"""CORS своими руками — ставить django-cors-headers ради трёх заголовков не стали."""

from django.conf import settings
from django.http import HttpResponse

ALLOWED_HEADERS = 'Authorization, Content-Type'
ALLOWED_METHODS = 'GET, POST, PATCH, DELETE, OPTIONS'
MAX_AGE = '86400'


class CorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.allowed = set(origin.strip() for origin in settings.CORS_ALLOWED_ORIGINS if origin.strip())

    def __call__(self, request):
        origin = request.headers.get('Origin')

        if request.method == 'OPTIONS' and origin:
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        if origin in self.allowed:
            response['Access-Control-Allow-Origin'] = origin
            response['Access-Control-Allow-Headers'] = ALLOWED_HEADERS
            response['Access-Control-Allow-Methods'] = ALLOWED_METHODS
            response['Access-Control-Max-Age'] = MAX_AGE
            response['Vary'] = 'Origin'

        return response
