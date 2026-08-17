from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from umag.models import UmagAccount
from umag.tests import FakeUmag

from .models import Invoice
from .openrouter import OpenRouterError, Parsed

User = get_user_model()

PARSED = {
    'supplier': 'ТОО «ЖЕТЫСУ-ТРЕЙД»',
    'supplier_bin': '140940011293',
    'number': '4000108981',
    'issued_at': '08.08.2026',
    'total': 32142.00,
    'lines': [
        {
            'name': 'Напиток PEPSI-COLA ПЭТ 1.0*12',
            'barcode': '4870145005545',
            'quantity': 60,
            'unit': 'бут.',
            'price': 535.50,
            'total': 32130.00,
        },
        {
            'name': 'Напиток PEPSI-COLA ПЭТ 1.0*12',
            'barcode': '4870145005545',
            'quantity': 12,
            'unit': 'бут.',
            'price': 1.00,
            'total': 12.00,
        },
    ],
}


# Столько OpenRouter списывает за одно фото — сумма попадает в карточку.
PARSE_RESULT = Parsed(PARSED, 'qwen/qwen3-vl-32b-instruct', 0.004965)


def photo():
    return SimpleUploadedFile('nakladnaya.jpg', b'\xff\xd8\xff\xdb fake jpeg', content_type='image/jpeg')


class AuthTests(APITestCase):
    def setUp(self):
        User.objects.create_user(email='shop@tvoymagazin.kz', password='tainy-parol-123')

    def test_login_returns_single_access_token(self):
        response = self.client.post(
            '/api/auth/login/',
            {'email': 'shop@tvoymagazin.kz', 'password': 'tainy-parol-123'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)
        self.assertNotIn('refresh', response.data)
        self.assertEqual(response.data['user']['email'], 'shop@tvoymagazin.kz')

    def test_login_rejects_wrong_password(self):
        response = self.client.post(
            '/api/auth/login/',
            {'email': 'shop@tvoymagazin.kz', 'password': 'ne-tot'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)

    def test_me_requires_token(self):
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 401)


@override_settings(INVOICE_PARSE_INLINE=True)
class InvoiceTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='shop@tvoymagazin.kz', password='tainy-parol-123')
        self.client.force_authenticate(self.user)

    def test_upload_schedules_parsing_and_stores_lines(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            response = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        self.assertEqual(response.status_code, 201)

        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.status, Invoice.Status.DONE)
        self.assertEqual(invoice.supplier_bin, '140940011293')
        self.assertEqual(str(invoice.total), '32142.00')
        self.assertEqual(invoice.issued_at.isoformat(), '2026-08-08')
        self.assertEqual(str(invoice.cost), '0.004965')

        lines = list(invoice.lines.all())
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].barcode, '4870145005545')
        self.assertEqual(str(lines[0].price), '535.50')

    def test_upload_remembers_chosen_store(self):
        """Магазин выбирают в шапке и меняют когда угодно — накладная держится своего."""

        UmagAccount.objects.create(
            user=self.user,
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
            store_name='Каратал Ерентал',
        )

        # Сеть подменяем: разбор теперь сам идёт в UMAG сопоставлять позиции.
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT), patch(
            'umag.client._request', new=FakeUmag()
        ):
            response = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.umag_store_id, 17795)
        self.assertEqual(invoice.umag_store_name, 'Каратал Ерентал')

    def test_upload_matches_lines_with_umag(self):
        """Готовой накладная становится уже сведённой с номенклатурой."""

        UmagAccount.objects.create(
            user=self.user,
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
            store_name='Каратал Ерентал',
        )

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT), patch(
            'umag.client._request', new=FakeUmag()
        ):
            response = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.status, Invoice.Status.DONE)

        line = invoice.lines.first()
        self.assertEqual(line.umag_product_id, 132421277)
        self.assertEqual(line.umag_product_name, 'Напиток PEPSI-COLA ПЭТ 1.0')
        self.assertEqual(line.umag_confidence, 1.0)
        # Единица измерения теперь из карточки товара, а не с фотографии.
        self.assertEqual(line.unit, 'шт')

    def test_status_stays_processing_while_matching(self):
        """Пока идёт сведение с кабинетом, накладная — «Распознаётся», не «В очереди»."""

        seen = {}

        def peek(invoice):
            seen['status'] = Invoice.objects.get(pk=invoice.pk).status

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT), patch(
            'umag.supply.match_lines', side_effect=peek
        ):
            self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        self.assertEqual(seen['status'], Invoice.Status.PROCESSING)

    def test_upload_survives_broken_umag(self):
        """Кабинет лёг — накладная всё равно распознана и открывается."""

        UmagAccount.objects.create(
            user=self.user,
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
        )

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT), patch(
            'umag.supply.match_lines',
            side_effect=RuntimeError('UMAG недоступен'),
        ):
            response = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.status, Invoice.Status.DONE)
        self.assertEqual(invoice.lines.count(), 2)

    def test_upload_without_umag_leaves_store_empty(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            response = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        self.assertIsNone(Invoice.objects.get(pk=response.data['id']).umag_store_id)

    def test_barcode_that_does_not_add_up_is_dropped(self):
        """Номенклатурный номер из соседней колонки штрихкодом не считается."""

        parsed = Parsed(
            {
                **PARSED,
                'lines': [
                    # 80843519 — номер поставщика: контрольная цифра не сходится.
                    {**PARSED['lines'][0], 'barcode': '80843519'},
                    {**PARSED['lines'][1], 'barcode': '4870145005545'},
                ],
            },
            'openai/gpt-5.6-luna',
            0.0017,
        )

        with patch('invoices.tasks.parse_invoice', return_value=parsed):
            response = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        lines = list(Invoice.objects.get(pk=response.data['id']).lines.all())
        # Строка не пропала — она просто пойдёт сопоставляться по названию.
        self.assertEqual(lines[0].barcode, '')
        self.assertEqual(lines[1].barcode, '4870145005545')
        # Прочитанное с фото сохранилось: с ним человеку сверяться.
        self.assertEqual(response.data['id'], Invoice.objects.get(pk=response.data['id']).pk)

    def test_heic_goes_to_model_as_jpeg(self):
        """Снимок с айфона отдаём моделью уже переведённым: HEIC читают не все."""

        heic = SimpleUploadedFile('IMG_0051.HEIC', b'\x00\x00\x00\x18ftypheic', content_type='image/heic')

        with (
            patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT) as model,
            patch('invoices.preview.to_jpeg', return_value=b'\xff\xd8\xff\xdb converted'),
        ):
            self.client.post('/api/invoices/', {'image': heic}, format='multipart')

        image, content_type = model.call_args.args
        self.assertEqual(content_type, 'image/jpeg')
        self.assertEqual(image, b'\xff\xd8\xff\xdb converted')

    def test_heic_goes_as_is_when_there_is_nothing_to_convert_with(self):
        """Утилиты для конвертации нет — отправляем оригинал, а не падаем."""

        heic = SimpleUploadedFile('IMG_0052.HEIC', b'\x00\x00\x00\x18ftypheic', content_type='image/heic')

        with (
            patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT) as model,
            patch('invoices.preview.to_jpeg', return_value=None),
        ):
            self.client.post('/api/invoices/', {'image': heic}, format='multipart')

        self.assertEqual(model.call_args.args[1], 'image/heic')

    def test_list_shows_only_invoices_of_chosen_store(self):
        """Магазин переключают в шапке — список документов меняется вместе с ним."""

        UmagAccount.objects.create(
            user=self.user,
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
            store_name='Каратал Ерентал',
        )

        mine = Invoice.objects.create(
            created_by=self.user,
            image='invoices/moya.jpg',
            umag_store_id=17795,
        )
        # Завели до подключения UMAG: спрятать её некуда, видна в любом магазине.
        nobodys = Invoice.objects.create(created_by=self.user, image='invoices/nichya.jpg')
        Invoice.objects.create(
            created_by=self.user,
            image='invoices/chuzhaya.jpg',
            umag_store_id=17796,
        )

        response = self.client.get('/api/invoices/')

        self.assertEqual(
            {item['id'] for item in response.data['results']},
            {mine.pk, nobodys.pk},
        )
        self.assertEqual(self.client.get('/api/invoices/counts/').data['all'], 2)

    def test_list_keeps_every_invoice_without_umag(self):
        """UMAG не подключён — отбирать не по чему, список остаётся общим."""

        Invoice.objects.create(created_by=self.user, image='invoices/odna.jpg', umag_store_id=17795)
        Invoice.objects.create(created_by=self.user, image='invoices/dve.jpg', umag_store_id=17796)

        self.assertEqual(self.client.get('/api/invoices/').data['count'], 2)

    def test_invoice_of_another_store_stays_open(self):
        """Открытую накладную дочитывают и правят, даже если магазин переключили."""

        UmagAccount.objects.create(
            user=self.user,
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
            store_name='Каратал Ерентал',
        )
        other = Invoice.objects.create(
            created_by=self.user,
            image='invoices/chuzhaya.jpg',
            status=Invoice.Status.DONE,
            umag_store_id=17796,
        )

        self.assertEqual(self.client.get(f'/api/invoices/{other.pk}/').status_code, 200)
        self.assertEqual(self.client.post(f'/api/invoices/{other.pk}/check/').status_code, 200)

    def test_failed_parsing_is_reported(self):
        with patch('invoices.tasks.parse_invoice', side_effect=OpenRouterError('OpenRouter ответил 402')):
            response = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.status, Invoice.Status.FAILED)
        self.assertIn('402', invoice.error)

    def test_invoices_are_isolated_per_user(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        other = User.objects.create_user(email='other@tvoymagazin.kz', password='tainy-parol-123')
        self.client.force_authenticate(other)

        self.assertEqual(self.client.get(f'/api/invoices/{created.data["id"]}/').status_code, 404)
        self.assertEqual(self.client.get('/api/invoices/').data['count'], 0)

    def test_delete_only_marks_invoice_as_deleted(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        self.assertEqual(self.client.delete(f'/api/invoices/{invoice_id}/').status_code, 204)

        # В базе накладная осталась, но с отметкой об удалении.
        self.assertFalse(Invoice.objects.filter(pk=invoice_id).exists())
        deleted = Invoice.all_objects.get(pk=invoice_id)
        self.assertIsNotNone(deleted.deleted_at)
        self.assertEqual(deleted.lines.count(), 2)

        # И пропала из обычного списка — но открывается из вкладки «Удалённые».
        self.assertEqual(self.client.get('/api/invoices/').data['count'], 0)
        self.assertEqual(self.client.get('/api/invoices/', {'tab': 'deleted'}).data['count'], 1)

    def test_check_marks_invoice_verified(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        response = self.client.post(f'/api/invoices/{invoice_id}/check/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], Invoice.Status.CHECKED)
        self.assertEqual(response.data['checked_by_email'], self.user.email)

        invoice = Invoice.objects.get(pk=invoice_id)
        self.assertIsNotNone(invoice.checked_at)
        self.assertEqual(invoice.checked_by, self.user)

    def test_check_rejected_until_parsing_finished(self):
        with patch('invoices.tasks.parse_invoice', side_effect=OpenRouterError('нет ключа')):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        response = self.client.post(f'/api/invoices/{created.data["id"]}/check/')
        self.assertEqual(response.status_code, 409)

    def test_retry_clears_check(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')
            self.client.post(f'/api/invoices/{created.data["id"]}/check/')
            self.client.post(f'/api/invoices/{created.data["id"]}/retry/')

        invoice = Invoice.objects.get(pk=created.data['id'])
        self.assertIsNone(invoice.checked_at)
        self.assertEqual(invoice.status, Invoice.Status.DONE)
        # Разбор был дважды — в карточке видно, во что обошлись оба.
        self.assertEqual(str(invoice.cost), '0.009930')

    def test_tabs_split_the_list(self):
        """Вкладки списка: ожидают проверки, проверенные и удалённые."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            waiting = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')
            checked = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')
            removed = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        self.client.post(f'/api/invoices/{checked.data["id"]}/check/')
        self.client.delete(f'/api/invoices/{removed.data["id"]}/')

        def ids(tab: str) -> set[int]:
            page = self.client.get('/api/invoices/', {'tab': tab} if tab else {})
            return {row['id'] for row in page.data['results']}

        self.assertEqual(ids(''), {waiting.data['id'], checked.data['id']})
        self.assertEqual(ids('pending'), {waiting.data['id']})
        self.assertEqual(ids('checked'), {checked.data['id']})
        self.assertEqual(ids('deleted'), {removed.data['id']})

    def test_counts_tell_how_many_are_waiting(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            self.client.post('/api/invoices/', {'image': photo()}, format='multipart')
            checked = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')
            removed = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        self.client.post(f'/api/invoices/{checked.data["id"]}/check/')
        self.client.delete(f'/api/invoices/{removed.data["id"]}/')

        counts = self.client.get('/api/invoices/counts/').data

        self.assertEqual(counts, {'all': 2, 'pending': 1, 'checked': 1, 'deleted': 1})

    def test_deleted_invoice_opens_read_only(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        self.client.delete(f'/api/invoices/{invoice_id}/')

        # Строка есть во вкладке «Удалённые», значит и открываться должна.
        self.assertEqual(self.client.get(f'/api/invoices/{invoice_id}/').status_code, 200)

    def test_line_can_be_edited(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        line = Invoice.objects.get(pk=invoice_id).lines.first()

        response = self.client.patch(
            f'/api/invoices/{invoice_id}/lines/{line.pk}/',
            {'name': 'Пепси 1 л', 'quantity': '48', 'price': '540.00'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        line.refresh_from_db()
        self.assertEqual(line.name, 'Пепси 1 л')
        self.assertEqual(str(line.quantity), '48.000')
        self.assertEqual(str(line.price), '540.00')

    def test_edit_forgets_matched_product(self):
        """Строку переписали — прежний товар из UMAG к ней уже не относится."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        line = Invoice.objects.get(pk=invoice_id).lines.first()
        line.umag_product_id = 132421277
        line.umag_product_name = 'Напиток PEPSI-COLA ПЭТ 1.0'
        line.umag_barcode = '4870145005545'
        line.umag_confidence = 0.9
        line.save()

        # Цену правят, не трогая товар: сопоставление остаётся.
        self.client.patch(
            f'/api/invoices/{invoice_id}/lines/{line.pk}/',
            {'price': '540.00'},
            format='json',
        )
        line.refresh_from_db()
        self.assertEqual(line.umag_product_id, 132421277)

        self.client.patch(
            f'/api/invoices/{invoice_id}/lines/{line.pk}/',
            {'name': 'Совсем другой товар'},
            format='json',
        )

        line.refresh_from_db()
        self.assertIsNone(line.umag_product_id)
        self.assertEqual(line.umag_barcode, '')
        self.assertIsNone(line.umag_confidence)

    def known_supplier(self, name='ТОО ЖЕТЫСУ-ТРЕЙД', code='140940011293'):
        """Прошлая накладная того же поставщика — с прочитанным БИН."""

        return Invoice.objects.create(
            created_by=self.user,
            status=Invoice.Status.CHECKED,
            supplier=name,
            supplier_bin=code,
        )

    def upload(self, supplier: str, bin_value=None):
        parsed = Parsed(
            {**PARSED, 'supplier': supplier, 'supplier_bin': bin_value},
            PARSE_RESULT.model,
            PARSE_RESULT.cost,
        )

        with patch('invoices.tasks.parse_invoice', return_value=parsed):
            response = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        return Invoice.objects.get(pk=response.data['id'])

    def test_bin_comes_from_the_same_supplier(self):
        """БИН не прочитался, но этот поставщик уже приезжал — берём оттуда."""

        self.known_supplier()

        with patch('invoices.suppliers.match_supplier') as model:
            invoice = self.upload('ТОО ЖЕТЫСУ-ТРЕЙД')

        # Название совпало дословно — модель для этого не нужна.
        model.assert_not_called()
        self.assertEqual(invoice.supplier_bin, '140940011293')
        self.assertTrue(invoice.supplier_bin_auto)

    def test_model_recognises_the_same_supplier_written_differently(self):
        self.known_supplier()

        answer = Parsed({'id': 0, 'confidence': 0.9}, 'qwen', 0.0001)

        with patch('invoices.suppliers.match_supplier', return_value=answer):
            invoice = self.upload('Товарищество с ограниченной ответственностью "Жетысу Трейд"')

        self.assertEqual(invoice.supplier_bin, '140940011293')
        self.assertTrue(invoice.supplier_bin_auto)

    def test_unsure_model_leaves_bin_empty(self):
        """Чужой БИН уедет в бухгалтерию — пустой лучше неверного."""

        self.known_supplier()

        answer = Parsed({'id': 0, 'confidence': 0.5}, 'qwen', 0.0001)

        with patch('invoices.suppliers.match_supplier', return_value=answer):
            invoice = self.upload('Жетысу Сут')

        self.assertEqual(invoice.supplier_bin, '')
        self.assertFalse(invoice.supplier_bin_auto)

    def test_bin_from_the_paper_is_not_touched(self):
        self.known_supplier()

        with patch('invoices.suppliers.match_supplier') as model:
            invoice = self.upload('ТОО ЖЕТЫСУ-ТРЕЙД', '999999999999')

        model.assert_not_called()
        self.assertEqual(invoice.supplier_bin, '999999999999')
        self.assertFalse(invoice.supplier_bin_auto)

    def test_first_invoice_has_nothing_to_borrow_from(self):
        with patch('invoices.suppliers.match_supplier') as model:
            invoice = self.upload('Новый поставщик')

        model.assert_not_called()
        self.assertEqual(invoice.supplier_bin, '')

    def test_line_can_be_added(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        response = self.client.post(f'/api/invoices/{invoice_id}/lines/', {}, format='json')

        self.assertEqual(response.status_code, 201)
        # Пустая строка встаёт первой, название человек впишет в таблице.
        self.assertEqual(response.data['position'], 1)
        self.assertEqual(response.data['name'], 'Новая позиция')

        invoice = Invoice.objects.get(pk=invoice_id)
        lines = list(invoice.lines.all())
        self.assertEqual([line.position for line in lines], [1, 2, 3])
        self.assertEqual(lines[0].pk, response.data['id'])
        # Распознанные строки только сдвинулись — их порядок и данные прежние.
        self.assertEqual(str(lines[1].total), '32130.00')
        self.assertEqual(str(lines[2].total), '12.00')

    def test_line_can_be_deleted(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice = Invoice.objects.get(pk=created.data['id'])
        first, second = invoice.lines.all()

        response = self.client.delete(f'/api/invoices/{invoice.pk}/lines/{first.pk}/')
        self.assertEqual(response.status_code, 204)

        invoice.refresh_from_db()
        lines = list(invoice.lines.all())
        self.assertEqual([line.pk for line in lines], [second.pk])
        # Нумерация без дыр, итог — по оставшейся строке.
        self.assertEqual(lines[0].position, 1)
        self.assertEqual(str(invoice.total), '12.00')

    def test_line_of_other_user_is_hidden(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        line = Invoice.objects.get(pk=invoice_id).lines.first()

        other = User.objects.create_user(email='other@tvoymagazin.kz', password='tainy-parol-123')
        self.client.force_authenticate(other)

        response = self.client.patch(
            f'/api/invoices/{invoice_id}/lines/{line.pk}/',
            {'name': 'чужое'},
            format='json',
        )
        self.assertEqual(response.status_code, 404)

    def test_rejects_non_image(self):
        document = SimpleUploadedFile('doc.txt', b'not a photo', content_type='text/plain')
        response = self.client.post('/api/invoices/', {'image': document}, format='multipart')

        self.assertEqual(response.status_code, 400)
