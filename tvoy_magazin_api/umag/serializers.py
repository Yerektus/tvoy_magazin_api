import re

from rest_framework import serializers

from .models import UmagAccount


class UmagAccountSerializer(serializers.ModelSerializer):
    """Состояние подключения. Токен наружу не отдаём — он ключ от кабинета."""

    connected = serializers.SerializerMethodField()

    class Meta:
        model = UmagAccount
        fields = ('connected', 'phone', 'store_id', 'store_name', 'connected_at')

    def get_connected(self, account) -> bool:
        return account.ready


class UmagConnectSerializer(serializers.Serializer):
    """В UMAG заходят по номеру телефона. Пароль нужен один раз — за токеном."""

    phone = serializers.CharField(max_length=32)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    # Если на номер заведено несколько сотрудников — кем из них заходим.
    user_id = serializers.IntegerField(required=False, allow_null=True)

    def validate_phone(self, phone: str) -> str:
        # Номер диктуют по-разному: «+7 747 441-96-54» и «7474419654» — это
        # один и тот же вход, разделители убираем, остальное не трогаем.
        cleaned = re.sub(r'[\s()\-]', '', phone).strip()

        if not re.fullmatch(r'\+?\d{6,20}', cleaned):
            raise serializers.ValidationError('Похоже, это не номер телефона')

        return cleaned


class UmagStoreSerializer(serializers.Serializer):
    store_id = serializers.IntegerField()
