from django.urls import path
from . import views

app_name = "reports"

urlpatterns = [
    path("sales/", views.sales_report, name="sales_report"),
    path("stock/", views.stock_report, name="stock_report"),
]
