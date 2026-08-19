from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import LoginSerializer, LogoutSerializer, UserSerializer


class LoginView(APIView):
    """POST /api/auth/login/ — почта и пароль в обмен на пару токенов."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class LogoutView(APIView):
    """POST /api/auth/logout/ — погасить refresh-токен.

    Аутентификации не требует намеренно: выходят обычно как раз тогда, когда
    access уже протух, и требовать живой access значило бы не дать выйти.
    Владение самим refresh-токеном тут и есть право его погасить.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """GET /api/auth/me/ — кто пришёл с этим токеном."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)
