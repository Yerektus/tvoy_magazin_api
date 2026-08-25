"""CORS своими руками — ставить django-cors-headers ради трёх заголовков не стали."""

from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpResponse

ALLOWED_HEADERS = 'Authorization, Content-Type'
ALLOWED_METHODS = 'GET, POST, PATCH, DELETE, OPTIONS'
MAX_AGE = '86400'

#: Своя машина. В отладке пускаем её с любого порта.
LOCAL_HOSTS = ('localhost', '127.0.0.1')


class CorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.allowed = set(origin.strip() for origin in settings.CORS_ALLOWED_ORIGINS if origin.strip())

    def _permitted(self, origin: str) -> bool:
        """Пускать ли этот источник.

        В списке — то, что разрешено всегда. Плюс при DEBUG пускаем свою же
        машину с любым портом: инструменты разработки их меняют сами (Expo
        занимает 8081, а если тот занят — следующий), и дописывать каждый в
        настройки значит ловить одну и ту же ошибку снова и снова.
        """

        if origin in self.allowed:
            return True

        return settings.DEBUG and urlsplit(origin).hostname in LOCAL_HOSTS

    def __call__(self, request):
        origin = request.headers.get('Origin')

        if request.method == 'OPTIONS' and origin:
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        if origin and self._permitted(origin):
            response['Access-Control-Allow-Origin'] = origin
            response['Access-Control-Allow-Headers'] = ALLOWED_HEADERS
            response['Access-Control-Allow-Methods'] = ALLOWED_METHODS
            response['Access-Control-Max-Age'] = MAX_AGE
            response['Vary'] = 'Origin'

        return response
