from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'email', 'name')


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
