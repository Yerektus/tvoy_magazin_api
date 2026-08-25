from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.tests import make_organization, make_user
from invoices.models import Invoice, InvoiceLine
from invoices.tests import sideways_jpeg
from purchases import planner
from purchases.tests import FakeReport, product as sold

from umag.client import UmagError
from umag.models import UmagAccount, UmagProduct
from umag.tests import FakeUmag

from . import agent, cabinet, tools
from .models import Conversation, Message


def photo():
    """Фото к вопросу. Настоящий JPEG: пустые байты сервер и не примет — он
    проверяет, что прислали картинку, а не что попало под видом неё."""

    return SimpleUploadedFile(
        'polka.jpg',
        sideways_jpeg((320, 240)),
        content_type='image/jpeg',
    )


def answer(text='Готово.', calls=None, cost=0.0001):
    """Ответ OpenRouter: либо словами, либо просьбой позвать функции."""

    message = {'role': 'assistant', 'content': None if calls else text}

    if calls:
        message['tool_calls'] = [
            {
                'id': f'call_{number}',
                'type': 'function',
                'function': {'name': name, 'arguments': arguments},
            }
            for number, (name, arguments) in enumerate(calls)
        ]

    return {'choices': [{'message': message}], 'usage': {'cost': cost}}


class ToolsTests(APITestCase):
    """Ручки видят только свою организацию — это главное, что тут проверяется."""

    def setUp(self):
        self.user = make_user()
        self.stranger = make_user(
            email='chужой@tvoymagazin.kz',
            organization=make_organization('Чужой магазин'),
        )

        self.mine = Invoice.objects.create(
            organization=self.user.organization,
            created_by=self.user,
            supplier='ТОО МОЙ ПОСТАВЩИК',
            total=Decimal('1000.00'),
            status=Invoice.Status.DONE,
        )
        InvoiceLine.objects.create(
            invoice=self.mine, position=1, name='Мой товар', total=Decimal('1000.00')
        )

        self.theirs = Invoice.objects.create(
            organization=self.stranger.organization,
            created_by=self.stranger,
            supplier='ТОО ЧУЖОЙ ПОСТАВЩИК',
            total=Decimal('999999.00'),
            status=Invoice.Status.DONE,
        )
        InvoiceLine.objects.create(
            invoice=self.theirs, position=1, name='Чужой товар', total=Decimal('999999.00')
        )

    def test_summary_counts_only_own_invoices(self):
        self.assertEqual(tools.summary(self.user)['накладных'], 1)
        self.assertEqual(tools.summary(self.user)['на_сумму'], 1000.0)

    def test_suppliers_hide_other_organizations(self):
        names = [row['поставщик'] for row in tools.suppliers(self.user)['поставщики']]

        self.assertIn('ТОО МОЙ ПОСТАВЩИК', names)
        self.assertNotIn('ТОО ЧУЖОЙ ПОСТАВЩИК', names)

    def test_products_hide_other_organizations(self):
        names = [row['товар'] for row in tools.products(self.user)['товары']]

        self.assertEqual(names, ['Мой товар'])

    def test_foreign_invoice_is_not_readable_by_id(self):
        """Даже зная номер чужой накладной, аналитик её не покажет."""

        self.assertEqual(
            tools.invoice(self.user, id=self.theirs.pk),
            {'ошибка': 'Такой накладной нет'},
        )

    def test_period_is_clamped_to_sane_bounds(self):
        """Чужие числа приводим к своим границам, а не верим им."""

        now = timezone.now()

        # Год — потолок: больше не смотрим, даже если попросят десять лет.
        self.assertGreater(tools.since(100000), now - timezone.timedelta(days=366))
        # Ноль и мусор не должны превращаться в «с начала времён».
        self.assertLess(tools.since(0), now)
        self.assertLess(tools.since('вчера'), now)


class AgentTests(APITestCase):
    """Цикл вызова функций: что модель может, а чего не может."""

    def setUp(self):
        self.user = make_user()

    def test_unknown_function_is_refused_not_executed(self):
        """Выдуманное имя получает отказ, а не попытку что-то исполнить."""

        result = agent._run(self.user, {'function': {'name': 'drop_database', 'arguments': '{}'}})

        self.assertEqual(result, {'ошибка': 'Нет такой функции: drop_database'})

    def test_extra_arguments_are_dropped(self):
        """Модель не может подсунуть чужую организацию: лишние ключи выбрасываем."""

        result = agent._run(
            self.user,
            {
                'function': {
                    'name': 'summary',
                    'arguments': '{"days": 7, "organization": 999, "user": 1}',
                }
            },
        )

        self.assertEqual(result['период_дней'], 7)

    def test_broken_arguments_do_not_crash(self):
        result = agent._run(
            self.user, {'function': {'name': 'summary', 'arguments': 'не json'}}
        )

        self.assertIn('период_дней', result)

    def test_reply_calls_the_tool_and_answers(self):
        replies = [
            answer(calls=[('summary', '{"days": 30}')]),
            answer('За месяц пришло 0 накладных.'),
        ]

        with patch('assistant.agent._post', side_effect=replies) as post:
            text, cost = agent.reply(self.user, [{'role': 'user', 'content': 'Что по накладным?'}])

        self.assertEqual(text, 'За месяц пришло 0 накладных.')
        self.assertAlmostEqual(cost, 0.0002)

        # Во втором запросе модель уже видит результат функции.
        sent = post.call_args.args[0]['messages']
        self.assertEqual(sent[-1]['role'], 'tool')

    def test_loop_gives_up_instead_of_spinning(self):
        """Модель просит данные по кругу — не даём ей крутиться вечно."""

        with patch(
            'assistant.agent._post',
            return_value=answer(calls=[('summary', '{}')]),
        ) as post:
            text, _ = agent.reply(self.user, [{'role': 'user', 'content': 'Привет'}])

        self.assertEqual(post.call_count, agent.MAX_STEPS)
        self.assertIn('Спросите', text)


class ChatApiTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)

    def test_question_and_answer_are_saved(self):
        with patch('assistant.agent._post', return_value=answer('Всё хорошо.')):
            response = self.client.post('/api/assistant/chat/', {'text': 'Как дела?'}, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual([m['text'] for m in response.data['messages']], ['Как дела?', 'Всё хорошо.'])

        history = self.client.get('/api/assistant/chat/')
        self.assertEqual(len(history.data['messages']), 2)

    def test_unanswered_question_does_not_stay_in_the_chat(self):
        """Модель недоступна — вопрос не остаётся висеть без ответа.

        Иначе переписка копит немые реплики: вопрос есть, ответа нет и не
        будет, а выглядит это как молчание аналитика. Текст при этом не
        теряется: приложение возвращает его в поле.
        """

        with patch('assistant.agent._post', side_effect=agent.OpenRouterError('OpenRouter лёг')):
            response = self.client.post('/api/assistant/chat/', {'text': 'Что там?'}, format='json')

        self.assertEqual(response.status_code, 502)
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(self.client.get('/api/assistant/chat/').data['messages'], [])

    def test_chat_can_be_started_over(self):
        with patch('assistant.agent._post', return_value=answer()):
            self.client.post('/api/assistant/chat/', {'text': 'Привет'}, format='json')

        self.client.delete('/api/assistant/chat/')

        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(self.client.get('/api/assistant/chat/').data['messages'], [])

    def test_photo_goes_to_the_model_and_stays_in_the_chat(self):
        """Фото уходит прямо в запросе: media у нас за логином, снаружи его
        никто не откроет."""

        with patch('assistant.agent._post', return_value=answer('Это ценник.')) as asked:
            response = self.client.post(
                '/api/assistant/chat/',
                {'text': 'Что это?', 'image': photo()},
                format='multipart',
            )

        self.assertEqual(response.status_code, 201)

        sent = asked.call_args.args[0]['messages'][-1]['content']
        kinds = [part['type'] for part in sent]

        self.assertEqual(kinds, ['text', 'image_url'])
        self.assertTrue(sent[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))

        # В переписке фото остаётся: человек должен видеть, о чём спрашивал.
        self.assertTrue(Message.objects.get(role=Message.Role.USER).image)
        self.assertIn('assistant/', response.data['messages'][0]['image'])

    def test_photo_is_not_paid_for_twice(self):
        """Со следующим вопросом старое фото не пересылаем."""

        with patch('assistant.agent._post', return_value=answer('Ценник.')):
            self.client.post(
                '/api/assistant/chat/',
                {'text': 'Что это?', 'image': photo()},
                format='multipart',
            )

        with patch('assistant.agent._post', return_value=answer('Да.')) as asked:
            self.client.post('/api/assistant/chat/', {'text': 'А дорого?'}, format='json')

        sent = asked.call_args.args[0]['messages']

        self.assertTrue(all(isinstance(message['content'], str) for message in sent))

    def test_a_photo_is_a_question_by_itself(self):
        """Прислали одну фотографию без слов — это тоже вопрос."""

        with patch('assistant.agent._post', return_value=answer('Вижу накладную.')):
            response = self.client.post(
                '/api/assistant/chat/',
                {'image': photo()},
                format='multipart',
            )

        self.assertEqual(response.status_code, 201)

    def test_nothing_at_all_is_not_a_question(self):
        response = self.client.post('/api/assistant/chat/', {'text': '   '}, format='json')

        self.assertEqual(response.status_code, 400)

    def test_thinking_is_switched_on_by_the_person(self):
        """Думать дольше — просьба человека: это дороже и заметно медленнее."""

        with patch('assistant.agent._post', return_value=answer('Ответ.')) as asked:
            self.client.post('/api/assistant/chat/', {'text': 'Подумай'}, format='json')

        self.assertNotIn('reasoning', asked.call_args.args[0])

        with patch('assistant.agent._post', return_value=answer('Ответ.')) as asked:
            self.client.post(
                '/api/assistant/chat/',
                {'text': 'Подумай', 'think': True},
                format='json',
            )

        # Рассуждение нужно модели, а не переписке: в ответ его не просим.
        self.assertEqual(
            asked.call_args.args[0]['reasoning'],
            {'enabled': True, 'exclude': True},
        )

    def test_chat_needs_authentication(self):
        self.client.force_authenticate(None)

        self.assertEqual(self.client.get('/api/assistant/chat/').status_code, 401)

    def test_other_users_chat_is_not_visible(self):
        """Переписка своя у каждого: она про его вопросы, а не про магазин."""

        with patch('assistant.agent._post', return_value=answer('Мой ответ')):
            self.client.post('/api/assistant/chat/', {'text': 'Мой вопрос'}, format='json')

        self.client.force_authenticate(make_user(email='другой@tvoymagazin.kz'))

        self.assertEqual(self.client.get('/api/assistant/chat/').data['messages'], [])


class CabinetTests(APITestCase):
    """Граница с UMAG: смотреть можно, писать нельзя, и только своим токеном."""

    def setUp(self):
        # Отчёт кабинета живёт в кэше пару минут — между тестами он не должен
        # переезжать, иначе половина из них проверяет чужой ответ.
        cache.clear()
        self.user = make_user()
        self.account = UmagAccount.objects.create(
            user=self.user,
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
            store_name='Каратал Ерентал',
        )
        UmagProduct.objects.create(
            store_id=17795,
            barcode='4870145005545',
            name='Напиток PEPSI-COLA ПЭТ 1.0',
            measure='шт',
        )

    def test_read_only_client_has_no_way_to_write(self):
        """У обёртки нет методов записи — приёмку через неё не создать."""

        client = cabinet.ReadOnly(self.account)

        for method in ('post', 'post_form', 'delete', 'put', 'patch'):
            self.assertFalse(hasattr(client, method), method)

    def test_only_whitelisted_paths_are_readable(self):
        """Даже читать можно не всё: список адресов закрыт."""

        client = cabinet.ReadOnly(self.account)

        with patch('umag.client._request', new=FakeUmag()):
            with self.assertRaises(UmagError):
                client.get('org/agent/list')

            with self.assertRaises(UmagError):
                client.get('nom/product/report')

    def test_catalog_searches_the_local_copy_without_touching_umag(self):
        umag = FakeUmag()

        with patch('umag.client._request', new=umag):
            found = cabinet.catalog(self.user, query='pepsi')

        self.assertEqual(found['найдено'], 1)
        self.assertEqual(found['товары'][0]['штрихкод'], '4870145005545')
        # Поиск по своей копии — в кабинет не ходили вовсе.
        self.assertEqual(umag.calls, [])

    def test_product_reads_price_and_stock(self):
        with patch('umag.client._request', new=FakeUmag()):
            card = cabinet.product(self.user, barcode='4870145005545')

        self.assertEqual(card['товар'], 'Напиток PEPSI-COLA ПЭТ 1.0')
        self.assertEqual(card['остаток'], 165)
        self.assertEqual(card['цена_продажи'], 400)

    def test_unknown_barcode_answers_plainly(self):
        with patch('umag.client._request', new=FakeUmag()):
            card = cabinet.product(self.user, barcode='9999999999999')

        self.assertEqual(card, {'ошибка': 'Товара с таким штрихкодом в кабинете нет'})

    def test_barcode_must_be_digits(self):
        """Что угодно вместо штрихкода в кабинет не уходит."""

        umag = FakeUmag()

        with patch('umag.client._request', new=umag):
            card = cabinet.product(self.user, barcode='../../org/agent/list')

        self.assertIn('ошибка', card)
        self.assertEqual(umag.calls, [])

    def test_broken_cabinet_does_not_break_the_chat(self):
        with patch('umag.client._request', new=FakeUmag(fail_on='findProductByBarcode')):
            card = cabinet.product(self.user, barcode='4870145005545')

        self.assertEqual(card, {'ошибка': 'Кабинет UMAG сейчас не отвечает'})

    def test_without_umag_the_answer_is_clear(self):
        stranger = make_user(email='без-умага@tvoymagazin.kz')

        self.assertEqual(cabinet.product(stranger, barcode='4870145005545'),
                         {'ошибка': 'UMAG не подключён'})
        self.assertIn('ошибка', cabinet.catalog(stranger))

    def test_other_employees_cabinet_is_not_used(self):
        """Токен принадлежит сотруднику: чужим кабинетом не пользуемся."""

        colleague = make_user(
            email='коллега@tvoymagazin.kz',
            organization=self.user.organization,
        )
        umag = FakeUmag()

        with patch('umag.client._request', new=umag):
            card = cabinet.product(colleague, barcode='4870145005545')

        self.assertEqual(card, {'ошибка': 'UMAG не подключён'})
        self.assertEqual(umag.calls, [])

    def test_catalog_shows_only_the_chosen_store(self):
        UmagProduct.objects.create(
            store_id=17797,
            barcode='1111111111111',
            name='Чужой магазин PEPSI',
            measure='шт',
        )

        names = [row['товар'] for row in cabinet.catalog(self.user, query='pepsi')['товары']]

        self.assertNotIn('Чужой магазин PEPSI', names)

    def test_sales_read_what_the_shop_actually_sold(self):
        """Продажи есть только в кабинете: в накладных лежит закуп."""

        report = FakeReport(
            [
                sold(
                    productName='Пепси 1 л',
                    saleQuantity=60,
                    saleSellingAmount=24000,
                    marginAmount=6000,
                    stockQuantity=10,
                ),
            ]
        )

        with patch('umag.client._request', new=report):
            found = cabinet.sales(self.user)

        row = found['товары'][0]

        self.assertEqual(found['период_дней'], 30)
        self.assertEqual(row['товар'], 'Пепси 1 л')
        self.assertEqual(row['продано'], 60)
        self.assertEqual(row['выручка'], 24000.0)
        self.assertEqual(row['маржа'], 6000.0)
        self.assertEqual(row['остаток'], 10)
        # Две штуки в день, десять на полке — на пять дней.
        self.assertEqual(row['хватит_дней'], 5.0)

    def test_sales_start_with_the_biggest_money(self):
        """«Что продаётся» — вопрос про деньги, а не про штуки."""

        report = FakeReport(
            [
                sold(productName='Жвачка', saleQuantity=500, saleSellingAmount=15000),
                sold(productName='Молоко', saleQuantity=90, saleSellingAmount=42000),
            ]
        )

        with patch('umag.client._request', new=report):
            names = [row['товар'] for row in cabinet.sales(self.user)['товары']]

        self.assertEqual(names, ['Молоко', 'Жвачка'])

    def test_sales_can_be_asked_about_one_product(self):
        report = FakeReport(
            [
                sold(productName='Напиток PEPSI-COLA ПЭТ 1.0'),
                sold(productName='Молоко «Одари» 2.5%'),
            ]
        )

        with patch('umag.client._request', new=report):
            found = cabinet.sales(self.user, query='pepsi')

        self.assertEqual(found['найдено'], 1)
        self.assertEqual(found['товары'][0]['товар'], 'Напиток PEPSI-COLA ПЭТ 1.0')

    def test_running_out_puts_the_shortest_stock_first(self):
        report = FakeReport(
            [
                sold(productName='Хватит надолго', saleQuantity=30, stockQuantity=100),
                sold(productName='Кончается', saleQuantity=300, stockQuantity=2),
                # Не продавался: полка с ним не опустеет, планировать нечего.
                sold(productName='Лежит мёртвым грузом', saleQuantity=0, stockQuantity=50),
            ]
        )

        with patch('umag.client._request', new=report):
            names = [row['товар'] for row in cabinet.running_out(self.user)['товары']]

        self.assertEqual(names, ['Кончается', 'Хватит надолго'])

    def test_the_report_is_asked_once_for_the_same_period(self):
        """В одном ответе аналитик спрашивает и продажи, и что кончается."""

        report = FakeReport([sold()])

        with patch('umag.client._request', new=report):
            cabinet.sales(self.user)
            cabinet.running_out(self.user)

        self.assertEqual(report.calls.count(planner.REPORT), 1)

    def test_broken_report_does_not_break_the_chat(self):
        with patch('umag.client._request', new=FakeUmag(fail_on='list-product-report')):
            found = cabinet.sales(self.user)

        self.assertEqual(found, {'ошибка': 'Кабинет UMAG сейчас не отвечает'})

    def test_sales_need_a_connected_cabinet(self):
        stranger = make_user(email='без-умага-тоже@tvoymagazin.kz')

        self.assertEqual(cabinet.sales(stranger), {'ошибка': 'UMAG не подключён'})
        self.assertEqual(cabinet.running_out(stranger), {'ошибка': 'UMAG не подключён'})

    def test_period_is_ours_to_decide(self):
        """Сколько бы дней ни попросила модель, дальше года не смотрим."""

        report = FakeReport([sold()])

        with patch('umag.client._request', new=report):
            self.assertEqual(cabinet.sales(self.user, days=99999)['период_дней'], 365)
            self.assertEqual(cabinet.sales(self.user, days='за всё время')['период_дней'], 30)

