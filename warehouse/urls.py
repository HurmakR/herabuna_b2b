from django.urls import path

from . import views

app_name = "warehouse"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("adjust/", views.adjust, name="adjust"),
    # Receipts are the only way to add inbound stock in UI.
    path("receipts/new/", views.receive_receipt_view, name="receipt_new"),
    path("receipts/<int:receipt_id>/", views.receipt_detail, name="receipt_detail"),

    # Backward-compatible URL (older bookmarks).
    path("receive-receipt/", views.receive_receipt_view, name="receive_receipt"),

    # Lots page kept for audit/debug (not linked from dashboard).
    path("product/<int:product_id>/lots/", views.product_lots, name="product_lots"),
]
