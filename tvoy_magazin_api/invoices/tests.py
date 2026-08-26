import io
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db.models import Sum
from django.test import SimpleTestCase, override_settings
from PIL import Image, ImageDraw
from rest_framework.test import APITestCase

from accounts.tests import make_organization, make_user
from umag.models import UmagAccount
from umag.tests import FakeUmag

from . import preview
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


def sideways_jpeg(size=(1600, 900)) -> bytes:
    """Снимок «лежащей на боку» накладной: широкий кадр с меткой в углу.

    Метка даёт понять, куда повернулась картинка, — иначе белый прямоугольник
    после поворота не отличить от исходного.
    """

    canvas = Image.new('RGB', size, 'white')
    ImageDraw.Draw(canvas).rectangle([(0, 0), (size[0] // 8, size[1] // 8)], fill='black')

    buffer = io.BytesIO()
    canvas.save(buffer, format='JPEG', quality=95)

    return buffer.getvalue()


# Сжатый снимок, который отдаёт обработка.
COMPRESSED = b'\xff\xd8\xff\xdb compressed'


class AuthTests(APITestCase):
    def setUp(self):
        make_user(email='shop@tvoymagazin.kz', password='tainy-parol-123')

    def login(self):
        return self.client.post(
            '/api/auth/login/',
            {'email': 'shop@tvoymagazin.kz', 'password': 'tainy-parol-123'},
            format='json',
        )

    def test_login_returns_both_tokens(self):
        response = self.login()

        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)
        self.assertEqual(response.data['user']['email'], 'shop@tvoymagazin.kz')

    def test_refresh_gives_a_working_access_token(self):
        """Access протух — за новым идут с refresh, а не с почтой и паролем."""

        refreshed = self.client.post(
            '/api/auth/refresh/',
            {'refresh': self.login().data['refresh']},
            format='json',
        )

        self.assertEqual(refreshed.status_code, 200)

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {refreshed.data["access"]}')
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 200)

    def test_used_refresh_token_stops_working(self):
        """Обновление выдаёт новый refresh, а прежний гасит: украденный не сработает."""

        stolen = self.login().data['refresh']
        self.client.post('/api/auth/refresh/', {'refresh': stolen}, format='json')

        again = self.client.post('/api/auth/refresh/', {'refresh': stolen}, format='json')
        self.assertEqual(again.status_code, 401)

    def test_rotation_hands_out_a_fresh_refresh_token(self):
        first = self.login().data['refresh']
        rotated = self.client.post('/api/auth/refresh/', {'refresh': first}, format='json')

        self.assertIn('refresh', rotated.data)
        self.assertNotEqual(rotated.data['refresh'], first)

    def test_logout_kills_the_refresh_token(self):
        """Выход настоящий: тем же refresh новый access больше не получить."""

        refresh = self.login().data['refresh']

        self.assertEqual(
            self.client.post('/api/auth/logout/', {'refresh': refresh}, format='json').status_code,
            204,
        )
        self.assertEqual(
            self.client.post('/api/auth/refresh/', {'refresh': refresh}, format='json').status_code,
            401,
        )

    def test_logout_needs_no_live_access_token(self):
        """Выходят обычно как раз тогда, когда access уже протух."""

        refresh = self.login().data['refresh']
        self.client.credentials(HTTP_AUTHORIZATION='Bearer protuh')

        response = self.client.post('/api/auth/logout/', {'refresh': refresh}, format='json')
        self.assertEqual(response.status_code, 204)

    def test_garbage_refresh_token_is_rejected(self):
        self.assertEqual(
            self.client.post('/api/auth/refresh/', {'refresh': 'ne-token'}, format='json').status_code,
            401,
        )
        self.assertEqual(
            self.client.post('/api/auth/logout/', {'refresh': 'ne-token'}, format='json').status_code,
            400,
        )

    def test_login_rejects_wrong_password(self):
        response = self.client.post(
            '/api/auth/login/',
            {'email': 'shop@tvoymagazin.kz', 'password': 'ne-tot'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)

    def test_me_requires_token(self):
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 401)

    def test_login_tells_which_organization_you_are_in(self):
        """Заходят в организацию — фронту нужно знать, в какую и кем."""

        user = self.login().data['user']

        self.assertEqual(user['organization']['name'], 'Магазин на углу')
        self.assertEqual(user['role'], User.Role.OWNER)
        self.assertTrue(user['manages_organization'])

    def test_account_without_organization_cannot_sign_in(self):
        """Суперпользователь из консоли — ему в админку Django, а не в кабинет."""

        User.objects.create_superuser(email='root@tvoymagazin.kz', password='tainy-parol-123')

        response = self.client.post(
            '/api/auth/login/',
            {'email': 'root@tvoymagazin.kz', 'password': 'tainy-parol-123'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)


class RoleTests(APITestCase):
    """Менеджер работает с накладными, но организацией не заведует."""

    def setUp(self):
        self.organization = make_organization()

    def sign_in(self, role):
        self.client.force_authenticate(
            make_user(email=f'{role}@tvoymagazin.kz', organization=self.organization, role=role)
        )

    def test_manager_does_not_get_the_extensions_catalogue(self):
        self.sign_in(User.Role.MANAGER)
        self.assertEqual(self.client.get('/api/extensions/').status_code, 403)

    def test_owner_and_admin_do(self):
        for role in (User.Role.OWNER, User.Role.ADMIN):
            with self.subTest(role=role):
                self.sign_in(role)
                self.assertEqual(self.client.get('/api/extensions/').status_code, 200)

    def test_manager_cannot_connect_an_extension(self):
        """Читать состояние можно — иначе страница закупов не нарисуется."""

        self.sign_in(User.Role.MANAGER)

        self.assertEqual(self.client.get('/api/purchases/access/').status_code, 200)
        self.assertEqual(self.client.post('/api/purchases/access/').status_code, 403)
        self.assertEqual(self.client.delete('/api/purchases/access/').status_code, 403)


class PreviewTests(SimpleTestCase):
    """Настоящая конвертация, без заглушек: на ней держится и просмотр, и разбор."""

    def heic(self, directory: str, size=(3000, 2000)) -> Path:
        path = Path(directory) / 'IMG_0033.HEIC'
        Image.new('RGB', size, 'white').save(path, format='HEIF')

        return path

    def sideways(self, directory: str) -> Path:
        path = Path(directory) / 'nakladnaya.jpg'
        path.write_bytes(sideways_jpeg())

        return path

    def test_heic_becomes_a_jpeg_the_browser_can_show(self):
        with tempfile.TemporaryDirectory() as directory:
            jpeg = preview.compress(self.heic(directory))

        self.assertIsNotNone(jpeg)

        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.format, 'JPEG')
            # Длинная сторона ужата: оригинал с телефона тяжелее вчетверо, а
            # за размер картинки мы платим ещё и токенами при разборе.
            self.assertEqual(max(image.size), preview.PREVIEW_SIZE)

    def test_photo_is_turned_the_way_the_model_read_it(self):
        """Поворот на 90 по часовой: широкий кадр встаёт вертикально, метка — вправо."""

        with tempfile.TemporaryDirectory() as directory:
            turned = preview.upright(self.sideways(directory), 90)

        self.assertIsNotNone(turned)

        with Image.open(io.BytesIO(turned)) as image:
            # Кадр был 1600x900 — после четверти оборота стал 900x1600.
            self.assertEqual(image.size, (900, 1600))
            # Метка была в левом верхнем углу; по часовой она уходит в правый
            # верхний, а левый становится чистым.
            self.assertLess(self.brightness(image.crop((800, 0, 900, 100))), 40)
            self.assertGreater(self.brightness(image.crop((0, 0, 100, 100))), 200)

    def brightness(self, image: Image.Image) -> float:
        return sum(image.convert('L').getdata()) / (image.width * image.height)

    def test_upright_photo_is_left_alone(self):
        """Угол нулевой — крутить нечего, файл не переписываем."""

        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(preview.upright(self.sideways(directory), 0))

    def test_top_edge_turns_the_frame_the_short_way(self):
        """Сторона, где шапка, — и угол, который ставит её наверх."""

        self.assertEqual(preview.UPRIGHT_TURNS['top'], 0)
        # Шапка справа — кадр идёт против часовой, то есть 270 по часовой.
        self.assertEqual(preview.UPRIGHT_TURNS['right'], 270)
        self.assertEqual(preview.UPRIGHT_TURNS['bottom'], 180)
        self.assertEqual(preview.UPRIGHT_TURNS['left'], 90)

    def test_ready_photo_is_not_compressed_twice(self):
        """«Распознать заново» не должно пережимать снимок по кругу."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nakladnaya.jpg'
            path.write_bytes(preview.compress(self.heic(directory)))

            self.assertIsNone(preview.compress(path))

    def test_broken_file_does_not_break_the_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / 'IMG_0034.HEIC'
            broken.write_bytes(b'\x00\x00\x00\x18ftypheic')

            self.assertIsNone(preview.compress(broken))

    def test_raster_formats_need_a_preview(self):
        # Любую картинку сжимаем — не только HEIC ради браузера, но и обычный
        # JPEG/PNG ради токенов модели.
        self.assertTrue(preview.needed_for('IMG_0033.HEIC'))
        self.assertTrue(preview.needed_for('foto.heif'))
        self.assertTrue(preview.needed_for('nakladnaya.jpg'))
        self.assertTrue(preview.needed_for('nakladnaya.png'))
        self.assertTrue(preview.needed_for('nakladnaya.webp'))
        # PDF Pillow не откроет, а сжимать его тут не наша забота.
        self.assertFalse(preview.needed_for('nakladnaya.pdf'))

    def test_jpeg_gets_compressed_too(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nakladnaya.jpg'
            Image.new('RGB', (3000, 4000), 'white').save(path, format='JPEG', quality=95)
            original_size = path.stat().st_size

            jpeg = preview.compress(path)

        self.assertIsNotNone(jpeg)
        self.assertLess(len(jpeg), original_size)

        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(max(image.size), preview.PREVIEW_SIZE)


@override_settings(INVOICE_PARSE_INLINE=True)
class InvoiceTests(APITestCase):
    def setUp(self):
        self.user = make_user(email='shop@tvoymagazin.kz', password='tainy-parol-123')
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

    def test_upload_leaves_one_photo_compressed_and_upright(self):
        """Настоящая обработка целиком: один файл, сжатый и повёрнутый как надо."""

        sideways = SimpleUploadedFile('nakladnaya.jpg', sideways_jpeg(), content_type='image/jpeg')
        # Шапка у левого края — значит кадр доворачивают на 90 по часовой.
        turned = Parsed({**PARSED, 'top_edge': 'left'}, 'qwen/qwen3-vl-32b-instruct', 0.004965)

        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            with patch('invoices.tasks.parse_invoice', return_value=turned):
                response = self.client.post('/api/invoices/', {'image': sideways}, format='multipart')

            invoice = Invoice.objects.get(pk=response.data['id'])

            # Кадр был широким, а лёг вертикально: снимок довёрнут.
            with Image.open(invoice.image) as stored:
                self.assertLess(stored.width, stored.height)

            # Сырого оригинала и старого отдельного превью нет: рядом со
            # снимком лежит только маленькая копия для списка.
            self.assertFalse(invoice.preview)
            self.assertTrue(invoice.thumbnail)

            saved = [path for path in Path(directory).rglob('*') if path.is_file()]
            self.assertEqual(len(saved), 2)

            # Копия и правда маленькая — и повёрнута вместе со снимком: в
            # списке накладная не должна лежать боком.
            with Image.open(invoice.thumbnail) as small:
                self.assertLessEqual(max(small.size), preview.THUMBNAIL_SIZE)
                self.assertLess(small.width, small.height)

            self.assertLess(invoice.thumbnail.size, invoice.image.size)

    def test_upright_photo_is_not_turned(self):
        """Шапка и так сверху — крутить нечего, кадр остаётся как был."""

        sideways = SimpleUploadedFile('nakladnaya.jpg', sideways_jpeg(), content_type='image/jpeg')
        straight = Parsed({**PARSED, 'top_edge': 'top'}, 'qwen/qwen3-vl-32b-instruct', 0.004965)

        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            with patch('invoices.tasks.parse_invoice', return_value=straight):
                response = self.client.post('/api/invoices/', {'image': sideways}, format='multipart')

            with Image.open(Invoice.objects.get(pk=response.data['id']).image) as stored:
                self.assertGreater(stored.width, stored.height)

    def test_old_photos_are_compressed_by_the_command(self):
        """Накладные, загруженные до появления обработки, догоняют потом."""

        heic = SimpleUploadedFile('IMG_0039.HEIC', b'\x00\x00\x00\x18ftypheic', content_type='image/heic')

        # Обработки не было: накладная разобралась, а снимок остался сырым.
        with (
            patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT),
            patch('invoices.preview.compress', return_value=None),
        ):
            response = self.client.post('/api/invoices/', {'image': heic}, format='multipart')

        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertTrue(invoice.image.name.endswith('.HEIC'))

        # Обработка появилась — команда проходит по таким накладным ещё раз.
        with patch('invoices.preview.compress', return_value=COMPRESSED):
            call_command('compress_photos')

        invoice.refresh_from_db()
        self.assertEqual(invoice.image.read(), COMPRESSED)

    def test_photo_is_served_with_debug_off(self):
        """В проде отладка выключена, а фотографию просмотрщик всё равно берёт."""

        self.assertFalse(settings.DEBUG)

        with tempfile.TemporaryDirectory() as directory:
            photo_path = Path(directory) / 'invoices'
            photo_path.mkdir()
            (photo_path / 'nakladnaya.jpg').write_bytes(b'\xff\xd8\xff\xdb fake jpeg')

            with override_settings(MEDIA_ROOT=directory):
                response = self.client.get('/media/invoices/nakladnaya.jpg')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'\xff\xd8\xff\xdb fake jpeg')

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

    def test_two_pages_go_to_the_model_as_one_document(self):
        """Накладная на двух листах — один документ, а не два.

        Оба листа уходят в один запрос: шапка есть только на первом, и разбери
        мы их порознь, у второго не было бы ни поставщика, ни номера.
        """

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT) as model:
            response = self.client.post(
                '/api/invoices/',
                {'image': photo(), 'pages': photo()},
                format='multipart',
            )

        self.assertEqual(response.status_code, 201)

        # Разбор был один, и в нём оба листа по порядку.
        self.assertEqual(model.call_count, 1)
        (pages,) = model.call_args.args
        self.assertEqual(len(pages), 2)

        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.pages.count(), 1)
        self.assertEqual(invoice.pages.first().position, 2)

    def test_card_shows_every_page(self):
        """В карточке — все листы по порядку, первым тот, что в накладной."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post(
                '/api/invoices/',
                {'image': photo(), 'pages': photo()},
                format='multipart',
            )

        card = self.client.get(f'/api/invoices/{created.data["id"]}/')
        self.assertEqual(len(card.data['images']), 2)
        # Первый лист тот же, что и одиночный снимок: старые клиенты берут его.
        self.assertTrue(card.data['images'][0].endswith(card.data['image'].split('/')[-1]))

    def test_single_page_invoice_still_has_one_image(self):
        """Без второго листа ничего не меняется: в списке один снимок."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        card = self.client.get(f'/api/invoices/{created.data["id"]}/')
        self.assertEqual(len(card.data['images']), 1)
        self.assertEqual(Invoice.objects.get(pk=created.data['id']).pages.count(), 0)

    def test_broken_extra_page_is_refused(self):
        """Второй лист проверяем так же, как первый."""

        broken = SimpleUploadedFile('notes.txt', b'not a photo', content_type='text/plain')

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            response = self.client.post(
                '/api/invoices/',
                {'image': photo(), 'pages': broken},
                format='multipart',
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_model_reads_the_compressed_photo(self):
        """Снимок с айфона отдаём моделью уже сжатым: HEIC читают не все."""

        heic = SimpleUploadedFile('IMG_0051.HEIC', b'\x00\x00\x00\x18ftypheic', content_type='image/heic')

        with (
            patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT) as model,
            patch('invoices.preview.compress', return_value=COMPRESSED),
        ):
            self.client.post('/api/invoices/', {'image': heic}, format='multipart')

        # Модель получает список листов; лист тут один.
        (pages,) = model.call_args.args
        self.assertEqual(pages, [(COMPRESSED, 'image/jpeg')])

    def test_heic_goes_as_is_when_it_cannot_be_processed(self):
        """Обработать снимок не вышло — отправляем оригинал, а не падаем."""

        heic = SimpleUploadedFile('IMG_0053.HEIC', b'\x00\x00\x00\x18ftypheic', content_type='image/heic')

        with (
            patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT) as model,
            patch('invoices.preview.compress', return_value=None),
        ):
            self.client.post('/api/invoices/', {'image': heic}, format='multipart')

        (pages,) = model.call_args.args
        self.assertEqual(pages[0][1], 'image/heic')

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
            organization=self.user.organization,
            created_by=self.user,
            image='invoices/moya.jpg',
            umag_store_id=17795,
        )
        # Завели до подключения UMAG: спрятать её некуда, видна в любом магазине.
        nobodys = Invoice.objects.create(organization=self.user.organization, created_by=self.user, image='invoices/nichya.jpg')
        Invoice.objects.create(
            organization=self.user.organization,
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

        Invoice.objects.create(organization=self.user.organization, created_by=self.user, image='invoices/odna.jpg', umag_store_id=17795)
        Invoice.objects.create(organization=self.user.organization, created_by=self.user, image='invoices/dve.jpg', umag_store_id=17796)

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
            organization=self.user.organization,
            created_by=self.user,
            image='invoices/chuzhaya.jpg',
            status=Invoice.Status.DONE,
            umag_store_id=17796,
        )

        self.assertEqual(self.client.get(f'/api/invoices/{other.pk}/').status_code, 200)
        self.assertEqual(self.client.post(f'/api/invoices/{other.pk}/check/').status_code, 200)

    def test_supplier_is_edited_by_hand(self):
        """Название и БИН поставщика правятся в карточке: печать модель читает плохо."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        response = self.client.patch(
            f'/api/invoices/{created.data["id"]}/',
            {'supplier': 'ТОО «КАРАВАН»', 'supplier_bin': '220340013017'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        invoice = Invoice.objects.get(pk=created.data['id'])
        self.assertEqual(invoice.supplier, 'ТОО «КАРАВАН»')
        self.assertEqual(invoice.supplier_bin, '220340013017')

    def test_hand_typed_bin_drops_the_guessed_mark(self):
        """БИН вписали руками — плашка «подставил ИИ» больше не про него."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice = Invoice.objects.get(pk=created.data['id'])
        invoice.supplier_bin_auto = True
        invoice.save(update_fields=('supplier_bin_auto',))

        self.client.patch(
            f'/api/invoices/{created.data["id"]}/',
            {'supplier_bin': '140940011293'},
            format='json',
        )

        invoice.refresh_from_db()
        self.assertFalse(invoice.supplier_bin_auto)

    def test_only_supplier_fields_can_be_edited(self):
        """Остальное прочитано с бумаги или ведём мы — PATCH его не трогает."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice = Invoice.objects.get(pk=created.data['id'])

        self.client.patch(
            f'/api/invoices/{created.data["id"]}/',
            {'number': 'подделка', 'total': '999999', 'status': Invoice.Status.CHECKED},
            format='json',
        )

        after = Invoice.objects.get(pk=invoice.pk)
        self.assertEqual(after.number, invoice.number)
        self.assertEqual(after.total, invoice.total)
        self.assertEqual(after.status, invoice.status)

    def test_deleted_invoice_cannot_be_edited(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        self.client.delete(f'/api/invoices/{created.data["id"]}/')
        response = self.client.patch(
            f'/api/invoices/{created.data["id"]}/',
            {'supplier': 'ТОО «КАРАВАН»'},
            format='json',
        )

        self.assertEqual(response.status_code, 409)

    def test_failed_parsing_is_reported(self):
        with patch('invoices.tasks.parse_invoice', side_effect=OpenRouterError('OpenRouter ответил 402')):
            response = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.status, Invoice.Status.FAILED)
        self.assertIn('402', invoice.error)

    def test_invoices_are_isolated_per_organization(self):
        """Чужая организация накладную не видит — ни в списке, ни по прямой ссылке."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        # У этого своя организация: `make_user` заводит её каждому.
        other = make_user(email='other@tvoymagazin.kz', password='tainy-parol-123')
        self.client.force_authenticate(other)

        self.assertEqual(self.client.get(f'/api/invoices/{created.data["id"]}/').status_code, 404)
        self.assertEqual(self.client.get('/api/invoices/').data['count'], 0)

    def test_colleague_sees_what_the_shift_uploaded(self):
        """Ради этого организация и заводилась: принял сменщик — видит хозяин."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        colleague = make_user(
            email='smena@tvoymagazin.kz',
            password='tainy-parol-123',
            organization=self.user.organization,
            role=User.Role.MANAGER,
        )
        self.client.force_authenticate(colleague)

        self.assertEqual(self.client.get(f'/api/invoices/{created.data["id"]}/').status_code, 200)
        self.assertEqual(self.client.get('/api/invoices/').data['count'], 1)

    def test_manager_edits_what_the_owner_uploaded(self):
        """Менеджера роль не ограничивает в работе с накладными — только в расширениях."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        self.client.force_authenticate(
            make_user(
                email='smena@tvoymagazin.kz',
                password='tainy-parol-123',
                organization=self.user.organization,
                role=User.Role.MANAGER,
            )
        )

        patched = self.client.patch(
            f'/api/invoices/{created.data["id"]}/',
            {'supplier': 'ТОО «КАРАВАН»'},
            format='json',
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(
            self.client.post(f'/api/invoices/{created.data["id"]}/check/').status_code,
            200,
        )

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
        # Строки выверены по бумаге — снимок годится в обучающую выборку.
        self.assertTrue(invoice.for_training)

    def test_unchecked_invoice_is_not_training_material(self):
        """Пока человек не сверил строки — это догадка модели, а не эталон."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        self.assertFalse(Invoice.objects.get(pk=created.data['id']).for_training)

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
        # Строки перечитаны заново — выверенными они быть перестали, и в
        # обучение такая пара больше не годится.
        self.assertFalse(invoice.for_training)
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

    def linked_umag(self):
        """Подключённый кабинет: без него сопоставлять не с чем."""

        return UmagAccount.objects.create(
            user=self.user,
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
            store_name='Каратал Ерентал',
        )

    def test_new_barcode_finds_product_again(self):
        """Штрихкод поправили — товар ищется в кабинете сразу, а не при отправке.

        Круг целиком: чужой штрихкод сбивает сопоставление, верный находит его
        заново. Раньше строка так и оставалась пустой до проверки накладной.
        """

        self.linked_umag()

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT), patch(
            'umag.client._request', new=FakeUmag()
        ):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        line = Invoice.objects.get(pk=invoice_id).lines.first()
        self.assertEqual(line.umag_product_id, 132421277)

        def edit(barcode):
            with patch('umag.client._request', new=FakeUmag()):
                self.client.patch(
                    f'/api/invoices/{invoice_id}/lines/{line.pk}/',
                    {'barcode': barcode},
                    format='json',
                )
            line.refresh_from_db()

        # Такого штрихкода в кабинете нет — товар отвязывается.
        edit('9999999999999')
        self.assertIsNone(line.umag_product_id)
        self.assertEqual(line.umag_product_name, '')

        # Вернули верный — товар находится снова, без всякой отправки.
        edit('4870145005545')
        self.assertEqual(line.umag_product_id, 132421277)
        self.assertEqual(line.umag_product_name, 'Напиток PEPSI-COLA ПЭТ 1.0')
        # Штрихкод с бумаги — сопоставление точное, без оценки модели.
        self.assertEqual(line.umag_confidence, 1.0)

    def test_name_edit_does_not_call_umag(self):
        """Правка названия в кабинет не ходит: по имени выбирает модель."""

        self.linked_umag()

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT), patch(
            'umag.client._request', new=FakeUmag()
        ):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        line = Invoice.objects.get(pk=invoice_id).lines.first()
        umag = FakeUmag()

        with patch('umag.client._request', new=umag):
            self.client.patch(
                f'/api/invoices/{invoice_id}/lines/{line.pk}/',
                {'name': 'Совсем другой товар'},
                format='json',
            )

        self.assertEqual(umag.calls, [])
        line.refresh_from_db()
        self.assertIsNone(line.umag_product_id)

    def test_broken_umag_does_not_break_barcode_edit(self):
        """Кабинет лёг — правка всё равно сохраняется, просто без товара."""

        self.linked_umag()

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT), patch(
            'umag.client._request', new=FakeUmag()
        ):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        line = Invoice.objects.get(pk=invoice_id).lines.first()

        with patch('umag.client._request', new=FakeUmag(fail_on='findProductByBarcode')):
            response = self.client.patch(
                f'/api/invoices/{invoice_id}/lines/{line.pk}/',
                {'barcode': '4870145005545'},
                format='json',
            )

        self.assertEqual(response.status_code, 200)
        line.refresh_from_db()
        self.assertEqual(line.barcode, '4870145005545')

    def test_edit_recounts_line_total(self):
        """Поправили количество — сумма строки и итог накладной идут следом.

        Без этого на экране остаётся сумма от прежних чисел: количество 10,
        цена 270, а рядом 18 900 от прошлых семидесяти.
        """

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        invoice = Invoice.objects.get(pk=invoice_id)
        line = invoice.lines.first()
        others = invoice.lines.exclude(pk=line.pk).aggregate(Sum('total'))['total__sum'] or 0

        self.client.patch(
            f'/api/invoices/{invoice_id}/lines/{line.pk}/',
            {'quantity': '10', 'price': '270'},
            format='json',
        )

        line.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(str(line.total), '2700.00')
        self.assertEqual(invoice.total, Decimal('2700.00') + others)

    def test_edit_of_price_alone_recounts_total(self):
        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        line = Invoice.objects.get(pk=invoice_id).lines.first()

        # Количество в первой строке — 60, цену ставим ровную.
        self.client.patch(
            f'/api/invoices/{invoice_id}/lines/{line.pk}/',
            {'price': '100'},
            format='json',
        )

        line.refresh_from_db()
        self.assertEqual(str(line.total), '6000.00')

    def test_hand_written_total_is_not_overwritten(self):
        """На бумаге сумма бывает не равна произведению — скидка, округление.

        Раз человек вписал её сам, наша арифметика к ней не относится.
        """

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        line = Invoice.objects.get(pk=invoice_id).lines.first()

        self.client.patch(
            f'/api/invoices/{invoice_id}/lines/{line.pk}/',
            {'quantity': '10', 'price': '270', 'total': '2500'},
            format='json',
        )

        line.refresh_from_db()
        self.assertEqual(str(line.total), '2500.00')

    def test_edit_without_quantity_leaves_total_alone(self):
        """Правка названия к арифметике отношения не имеет."""

        with patch('invoices.tasks.parse_invoice', return_value=PARSE_RESULT):
            created = self.client.post('/api/invoices/', {'image': photo()}, format='multipart')

        invoice_id = created.data['id']
        line = Invoice.objects.get(pk=invoice_id).lines.first()
        was = line.total

        self.client.patch(
            f'/api/invoices/{invoice_id}/lines/{line.pk}/',
            {'name': 'Другое название'},
            format='json',
        )

        line.refresh_from_db()
        self.assertEqual(line.total, was)

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
            organization=self.user.organization,
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

        other = make_user(email='other@tvoymagazin.kz', password='tainy-parol-123')
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
