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
    manages_organization = serializers.BooleanField(read_only=True)
    uses_purchases = serializers.BooleanField(read_only=True)
    uses_assistant = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = (
            'id',
            'email',
            'name',
            'role',
            'organization',
            'manages_organization',
            'uses_purchases',
            'uses_assistant',
        )


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
            raise serializers.ValidationError('Неверная почта или пароль')

        if not user.is_active:
            raise serializers.ValidationError('Учётная запись отключена')

        if user.organization_id is None:
            raise serializers.ValidationError('Учётная запись не привязана к организации')

        attrs['user'] = user
        return attrs

    def to_representation(self, instance):
        user = instance['user']
        refresh = RefreshToken.for_user(user)

        return {
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserSerializer(user).data,
        }


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()

    def validate_refresh(self, value):
        try:
            self.token = RefreshToken(value)
        except TokenError as error:
            raise serializers.ValidationError('Токен уже недействителен') from error

        return value

    def save(self, **kwargs):
        self.token.blacklist()
