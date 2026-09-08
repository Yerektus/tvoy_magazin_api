from django.urls import path

from .views import ApprovedPurchaseListView, ApproveSupplierView, PlanningAccessView, PurchasePlanView

urlpatterns = [
    path('access/', PlanningAccessView.as_view(), name='planning-access'),
    path('plan/', PurchasePlanView.as_view(), name='purchase-plan'),
    path('plan/approve/', ApproveSupplierView.as_view(), name='purchase-approve'),
    path('approved/', ApprovedPurchaseListView.as_view(), name='purchase-approved'),
]
