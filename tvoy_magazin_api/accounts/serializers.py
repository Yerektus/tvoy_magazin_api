from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Organization, User


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ('id', 'name')


class UserSerializer(serializers.ModelSerializer):
    organization = OrganizationSerializer(read_only=True)
    # По ней фронт решает, показывать ли расширения: у менеджера их нет.
    manages_organization = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ('id', 'email', 'name', 'role', 'organization', 'manages_organization')


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get('request'),
            username=attrs['email'].strip(),
            password=attrs['password'],
        )

        if user is None:
            # Один и тот же текст для неизвестной почты и неверного пароля,
            # чтобы по ответу нельзя было перебирать существующие адреса.
            raise serializers.ValidationError('Неверная почта или пароль')

        if not user.is_active:
            raise serializers.ValidationError('Учётная запись отключена')

        if user.organization_id is None:
            # Заходят в организацию, а не «просто в кабинет»: без неё непонятно,
            # чьи накладные показывать и куда складывать новые. Так выглядит
            # суперпользователь, заведённый из консоли, — ему в админку Django.
            raise serializers.ValidationError('Учётная запись не привязана к организации')

        attrs['user'] = user
        return attrs

    def to_representation(self, instance):
        user = instance['user']
        # Refresh порождает access сам — так у пары один общий срок жизни и
        # один идентификатор в чёрном списке.
        refresh = RefreshToken.for_user(user)

        return {
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserSerializer(user).data,
        }


class LogoutSerializer(serializers.Serializer):
    """Гасит refresh-токен, чтобы выход из аккаунта был настоящим.

    Access живёт своим сроком и досрочно не отзывается — час он ещё поработает.
    Чтобы отзывался и он, пришлось бы ходить в базу на каждый запрос, а это как
    раз то, ради чего JWT и берут.
    """

    refresh = serializers.CharField()

    def validate_refresh(self, value):
        try:
            self.token = RefreshToken(value)
        except TokenError as error:
            raise serializers.ValidationError('Токен уже недействителен') from error

        return value

    def save(self, **kwargs):
        self.token.blacklist()
