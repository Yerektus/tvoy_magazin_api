from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('accounts.urls')),
    path('api/extensions/', include('extensions.urls')),
    path('api/invoices/', include('invoices.urls')),
    path('api/purchases/', include('purchases.urls')),
    path('api/umag/', include('umag.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
