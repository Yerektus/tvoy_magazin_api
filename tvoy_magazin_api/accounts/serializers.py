from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.tokens import AccessToken

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
        return {
            'access': str(AccessToken.for_user(user)),
            'user': UserSerializer(user).data,
        }
