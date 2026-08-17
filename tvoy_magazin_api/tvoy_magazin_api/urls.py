from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('accounts.urls')),
    path('api/extensions/', include('extensions.urls')),
    path('api/invoices/', include('invoices.urls')),
    path('api/purchases/', include('purchases.urls')),
    path('api/umag/', include('umag.urls')),
]


def media(request, path):
    """Фотография накладной с примонтированного тома."""

    return serve(request, path, document_root=settings.MEDIA_ROOT)


def static_file(request, path):
    """Оформление админки, разложенное `collectstatic`."""

    return serve(request, path, document_root=settings.STATIC_ROOT)


# Файлы раздаёт сам Django — и в проде тоже. Обычно это работа nginx, но перед
# нами его нет: на хостинге стоит только маршрутизатор, а файлы лежат на
# примонтированном томе, куда он не заглядывает. Стандартный хелпер `static()`
# тут не годится — он молча отдаёт пустой список, когда отладка выключена, и
# фотографии пропадают из просмотрщика.
#
# Плата за это — занятый воркер на время отдачи файла, поэтому у gunicorn
# должны быть потоки: `--threads 4`.
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', media),
    re_path(r'^static/(?P<path>.*)$', static_file),
]
