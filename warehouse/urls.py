from django.urls import path

from . import views

app_name = "warehouse"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("product/<int:product_id>/lots/", views.product_lots, name="product_lots"),
    path("receive/", views.receive, name="receive"),
    path("adjust/", views.adjust, name="adjust"),
]
