from django.urls import path

from .views import UmagAccountView, UmagStoresView, UmagSupplyView

urlpatterns = [
    path('account/', UmagAccountView.as_view(), name='umag-account'),
    path('stores/', UmagStoresView.as_view(), name='umag-stores'),
    path('invoices/<int:pk>/', UmagSupplyView.as_view(), name='umag-supply'),
]
