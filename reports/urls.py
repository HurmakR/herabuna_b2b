from django.urls import path
from . import views, service_views, marketing_views, admin_views
from .service_price import service_price, service_price_export

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
    path("service/price/", service_price, name="service_price"),
    path("service/price/export/", service_price_export, name="service_price_export"),
    path("service/sync-variations/", service_views.service_sync_variations, name="service_sync_variations"),
    path("service/marketplace/queue/clear/", service_views.service_marketplace_clear_queue, name="service_marketplace_clear_queue"),
    path("marketing/mailing/", marketing_views.marketing_mailing, name="marketing_mailing"),
    path("marketing/validate-email/", marketing_views.validate_email_domain, name="marketing_validate_email"),
    path("admin/activity/", admin_views.activity_monitor, name="admin_activity"),
    path("admin/activation/", admin_views.activation_requests, name="activation_requests"),
]
