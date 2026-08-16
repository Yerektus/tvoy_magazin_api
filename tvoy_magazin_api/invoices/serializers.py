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
            'quantity',
            'unit',
            'price',
            'total',
            # С каким товаром кабинета сведена строка. Уверенность меньше
            # единицы — штрихкод подставила модель, а не бумага.
            'umag_product_name',
            'umag_confidence',
        )
        read_only_fields = ('id', 'position', 'umag_product_name', 'umag_confidence')
        # Строку добавляют пустой и заполняют прямо в таблице, поэтому название
        # при создании не требуем — вью подставит заглушку.
        extra_kwargs = {'name': {'required': False, 'allow_blank': True}}


class InvoiceListSerializer(serializers.ModelSerializer):
    lines_count = serializers.IntegerField(source='lines.count', read_only=True)
    checked_by_email = serializers.EmailField(source='checked_by.email', read_only=True, default=None)

    class Meta:
        model = Invoice
        fields = (
            'id',
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


class InvoiceDetailSerializer(InvoiceListSerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)
    image = serializers.FileField(read_only=True)
    # Пусто у обычных JPEG — там браузеру хватает оригинала.
    preview = serializers.FileField(read_only=True)

    class Meta(InvoiceListSerializer.Meta):
        fields = InvoiceListSerializer.Meta.fields + ('image', 'preview', 'model', 'lines')


class InvoiceCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = ('id', 'image', 'status')
        read_only_fields = ('id', 'status')

    def validate_image(self, image):
        if image.size > MAX_IMAGE_SIZE:
            raise serializers.ValidationError('Файл больше 15 МБ')

        known_type = image.content_type in ALLOWED_TYPES
        known_name = image.name.lower().endswith(ALLOWED_EXTENSIONS)

        if not known_type and not known_name:
            raise serializers.ValidationError('Нужен файл JPEG, PNG, WEBP, HEIC или PDF')

        return image
