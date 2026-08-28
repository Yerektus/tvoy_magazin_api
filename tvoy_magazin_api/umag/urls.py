from django.urls import path

from .views import (
    UmagAccountView,
    UmagCategoriesView,
    UmagProductView,
    UmagStoresView,
    UmagSupplyView,
)

urlpatterns = [
    path('account/', UmagAccountView.as_view(), name='umag-account'),
    path('stores/', UmagStoresView.as_view(), name='umag-stores'),
    path('categories/', UmagCategoriesView.as_view(), name='umag-categories'),
    path('products/<str:barcode>/', UmagProductView.as_view(), name='umag-product'),
    path('invoices/<int:pk>/', UmagSupplyView.as_view(), name='umag-supply'),
]
