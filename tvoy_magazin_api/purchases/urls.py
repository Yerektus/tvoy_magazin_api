from django.urls import path

from .views import PlanningAccessView, PurchasePlanView

urlpatterns = [
    path('access/', PlanningAccessView.as_view(), name='planning-access'),
    path('plan/', PurchasePlanView.as_view(), name='purchase-plan'),
]
