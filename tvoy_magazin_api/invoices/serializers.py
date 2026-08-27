from rest_framework import serializers

from .models import Invoice, InvoiceLine

MAX_IMAGE_SIZE = 15 * 1024 * 1024
ALLOWED_TYPES = (
    'image/jpeg',
    'image/png',
    'image/webp',
    'image/heic',
    'image/heif',
    'application/pdf',
)
# Айфон снимает в HEIC. Safari присылает честный image/heic, а Chrome на маке —
# пустой тип или application/octet-stream, поэтому смотрим ещё и на имя файла.
ALLOWED_EXTENSIONS = ('.heic', '.heif')


class InvoiceLineSerializer(serializers.ModelSerializer):
    """Позиции правятся руками: модель ошибается, бумага — источник истины."""

    class Meta:
        model = InvoiceLine
        fields = (
            'id',
            'position',
            'name',
            'barcode',
            'barcode_auto',
            'quantity',
            'unit',
            'price',
            'total',
            # С каким товаром кабинета сведена строка. Уверенность меньше
            # единицы — штрихкод подставила модель, а не бумага.
            'umag_product_name',
            'umag_confidence',
            # Такого товара в кабинете нет — карточку заведём при отправке, а
            # что в ней написать, человек указывает в полях ниже.
            'umag_missing',
            'umag_new_name',
            'umag_new_measure',
            'umag_new_category_id',
            'umag_new_selling_price',
        )
        read_only_fields = (
            'id',
            'position',
            # Подобран он или прочитан — решает сервер: правка штрихкода руками
            # эту отметку снимает, а выставить её со стороны нельзя.
            'barcode_auto',
            'umag_product_name',
            'umag_confidence',
            # Есть товар в кабинете или нет — решает сопоставление, а не тот,
            # кто правит строку.
            'umag_missing',
        )
        # Строку добавляют пустой и заполняют прямо в таблице, поэтому название
        # при создании не требуем — вью подставит заглушку.
        extra_kwargs = {'name': {'required': False, 'allow_blank': True}}


class InvoiceListSerializer(serializers.ModelSerializer):
    lines_count = serializers.IntegerField(source='lines.count', read_only=True)
    checked_by_email = serializers.EmailField(source='checked_by.email', read_only=True, default=None)
    # Снимок нужен и списку: приложение показывает накладные карточками с
    # превью — по бумаге документ узнают быстрее, чем по номеру.
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = (
            'id',
            'thumbnail',
            'status',
            'error',
            'supplier',
            'supplier_bin',
            'supplier_bin_auto',
            'number',
            'issued_at',
            'total',
            'cost',
            'lines_count',
            'created_at',
            'processed_at',
            'checked_at',
            'checked_by_email',
            'umag_supply_id',
            'umag_pushed_at',
            'umag_store_id',
            'umag_store_name',
        )

    def get_thumbnail(self, invoice) -> str | None:
        """Первый лист накладной — маленькой копией.

        У накладных, снятых до того, как мы стали её делать, копии нет: отдаём
        полный снимок, чтобы карточка не осталась пустой.
        """

        image = invoice.thumbnail or invoice.preview or invoice.image

        if not image:
            return None

        request = self.context.get('request')

        return request.build_absolute_uri(image.url) if request else image.url


class InvoiceDetailSerializer(InvoiceListSerializer):
    """Карточка накладной. Правятся руками поставщик и дата документа.

    Дата — потому что кабинет её проверяет: приход раньше проведённой
    инвентаризации он не принимает, а модель нет-нет да и прочитает «2020»
    вместо «2026». Без правки такая накладная не уехала бы никогда.

    Остальное либо прочитано с бумаги и меняется через «распознать заново»,
    либо ведётся нами (статус, стоимость разбора, отметки UMAG), поэтому
    держим всё это только на чтение: PATCH с лишним полем ничего не испортит.
    """

    lines = InvoiceLineSerializer(many=True, read_only=True)
    image = serializers.FileField(read_only=True)
    # Выпрямленный снимок — его и показываем. Пусто, если лист на фото найти
    # не удалось: тогда просмотрщик откатывается на `image`.
    preview = serializers.FileField(read_only=True)
    images = serializers.SerializerMethodField()

    class Meta(InvoiceListSerializer.Meta):
        fields = InvoiceListSerializer.Meta.fields + (
            'image',
            'images',
            'preview',
            'model',
            'lines',
        )
        read_only_fields = tuple(set(fields) - {'supplier', 'supplier_bin', 'issued_at'})

    def get_images(self, invoice) -> list[str]:
        """Все листы по порядку, первым — тот, что в самой накладной.

        Одним списком, а не «image плюс отдельно остальные»: смотрящему всё
        равно, где какой лист хранится, ему нужно пролистать документ.
        """

        request = self.context.get('request')
        files = [invoice.preview or invoice.image, *(page.image for page in invoice.pages.all())]
        urls = [image.url for image in files if image]

        return [request.build_absolute_uri(url) for url in urls] if request else urls

    def update(self, invoice, validated_data):
        # БИН вписали руками — значит он из бумаги, а не подставлен моделью по
        # прошлым накладным. Иначе в карточке осталась бы висеть плашка «подставил ИИ».
        if 'supplier_bin' in validated_data:
            invoice.supplier_bin_auto = False

        return super().update(invoice, validated_data)


class InvoiceCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = ('id', 'image', 'status')
        read_only_fields = ('id', 'status')

    def validate_image(self, image):
        return check_photo(image)


def check_photo(image):
    """Годится ли файл в снимок накладной.

    Отдельной функцией, а не только методом сериализатора: те же проверки нужны
    для второго и следующих листов, которые приходят рядом с первым.
    """

    if image.size > MAX_IMAGE_SIZE:
        raise serializers.ValidationError('Файл больше 15 МБ')

    known_type = getattr(image, 'content_type', '') in ALLOWED_TYPES
    known_name = image.name.lower().endswith(ALLOWED_EXTENSIONS)

    if not known_type and not known_name:
        raise serializers.ValidationError('Нужен файл JPEG, PNG, WEBP, HEIC или PDF')

    return image
