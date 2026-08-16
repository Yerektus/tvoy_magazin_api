from django.urls import path

from .views import (
    InvoiceCheckView,
    InvoiceCountsView,
    InvoiceDetailView,
    InvoiceLineCreateView,
    InvoiceLineView,
    InvoiceListCreateView,
    InvoiceRetryView,
)

urlpatterns = [
    path('', InvoiceListCreateView.as_view(), name='invoice-list'),
    path('counts/', InvoiceCountsView.as_view(), name='invoice-counts'),
    path('<int:pk>/', InvoiceDetailView.as_view(), name='invoice-detail'),
    path('<int:pk>/check/', InvoiceCheckView.as_view(), name='invoice-check'),
    path('<int:pk>/lines/', InvoiceLineCreateView.as_view(), name='invoice-line-create'),
    path('<int:pk>/lines/<int:line_id>/', InvoiceLineView.as_view(), name='invoice-line'),
    path('<int:pk>/retry/', InvoiceRetryView.as_view(), name='invoice-retry'),
]
