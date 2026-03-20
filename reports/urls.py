from django.urls import path
from . import views
from . import service_views

app_name = "reports"

urlpatterns = [
    path("sales/", views.sales_report, name="sales_report"),
    path("stock/", views.stock_report, name="stock_report"),
    path("service/", service_views.service_dashboard, name="service_dashboard"),
    path("service/export/", service_views.export_backup, name="service_export"),
    path("service/import/", service_views.import_backup, name="service_import"),
    path("service/reset-warehouse/", service_views.reset_warehouse, name="service_reset_warehouse"),
    path("service/reset-orders/", service_views.reset_orders, name="service_reset_orders"),
    path("service/woo/", service_views.service_woo_import, name="service_woo_import"),
    path("service/woo/import/", service_views.service_woo_import_apply, name="service_woo_import_apply"),
    path("service/woo/stock/", service_views.service_woo_stock_sync, name="service_woo_stock_sync"),
    path("service/woo/stock/apply/", service_views.service_woo_stock_sync_apply, name="service_woo_stock_sync_apply"),
    path("service/marketplace/orders/", service_views.service_marketplace_orders, name="service_marketplace_orders"),
    path("service/marketplace/orders/sync/", service_views.service_marketplace_orders_sync, name="service_marketplace_orders_sync"),
    path("service/marketplace/orders/apply/", service_views.service_marketplace_orders_apply, name="service_marketplace_orders_apply"),
]
