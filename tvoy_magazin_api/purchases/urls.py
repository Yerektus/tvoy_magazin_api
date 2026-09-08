from django.urls import path

from .views import (
    ApprovedPurchaseListView,
    ApproveSupplierView,
    PlanningAccessView,
    PurchasePlanDetailView,
    PurchasePlanListView,
    PurchasePlanView,
    StoreProductsView,
)

urlpatterns = [
    path('access/', PlanningAccessView.as_view(), name='planning-access'),
    path('products/', StoreProductsView.as_view(), name='purchase-products'),
    path('plans/', PurchasePlanListView.as_view(), name='purchase-plans'),
    path('plans/<int:pk>/', PurchasePlanDetailView.as_view(), name='purchase-plan-detail'),
    path('plan/', PurchasePlanView.as_view(), name='purchase-plan'),
    path('plan/approve/', ApproveSupplierView.as_view(), name='purchase-approve'),
    path('approved/', ApprovedPurchaseListView.as_view(), name='purchase-approved'),
]
