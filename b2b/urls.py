from django.contrib.auth import views as auth_views
from django.urls import path
from . import views

app_name = "b2b"

urlpatterns = [
    path("signup/", views.signup, name="signup"),
    path("login/", auth_views.LoginView.as_view(template_name="b2b/login.html", redirect_authenticated_user=True), name="login"),
    path("logout/", views.logout_view, name="logout"),

    path("", views.dashboard, name="dashboard"),
    path("products/", views.product_list, name="product_list"),
    path("products/<int:product_id>/", views.product_detail, name="product_detail"),

    path("cart/", views.cart, name="cart"),
    path("add/<int:product_id>/", views.add_to_cart, name="add_to_cart"),
    path("add/<int:product_id>/with-attrs/", views.add_to_cart_with_attrs, name="add_to_cart_with_attrs"),
    path("cart/item/<int:item_id>/update/", views.cart_update_item, name="cart_update_item"),
    path("cart/item/<int:item_id>/remove/", views.cart_remove_item, name="cart_remove_item"),
    path("cart/clear/", views.cart_clear, name="cart_clear"),

    path("submit/", views.submit_order, name="submit_order"),
    path("orders/<int:order_id>/", views.order_detail, name="order_detail"),

    path("orders/<int:order_id>/invoice/", views.invoice_print, name="invoice_print"),
    path("orders/<int:order_id>/waybill/", views.waybill_print, name="waybill_print"),
    path("orders/<int:order_id>/invoice.pdf", views.invoice_pdf, name="invoice_pdf"),
    path("orders/<int:order_id>/waybill.pdf", views.waybill_pdf, name="waybill_pdf"),
    path("orders/<int:order_id>/admin/<str:action>/", views.order_admin_action, name="order_admin_action"),

    # staff management
    path("orders-admin/", views.orders_admin, name="orders_admin"),
    path("products/<int:product_id>/update/", views.product_update_inline, name="product_update_inline"),
    path("orders/<int:order_id>/set-status/<str:status>/", views.order_set_status, name="order_set_status"),
    path("orders/<int:order_id>/label.pdf", views.order_np_label, name="order_np_label"),
    path("profile/", views.profile_view, name="profile"),
    path("profile/addresses/", views.address_list, name="address_list"),
    path("profile/addresses/new/", views.address_create, name="address_create"),
    path("profile/addresses/<int:pk>/edit/", views.address_edit, name="address_edit"),
    path("profile/addresses/<int:pk>/delete/", views.address_delete, name="address_delete"),

    path("checkout/", views.order_checkout, name="order_checkout"),  # select address page
    path("checkout/confirm/", views.order_checkout_confirm, name="order_checkout_confirm"),

    path("np/cities/", views.np_cities, name="np_cities"),
    path("np/warehouses/", views.np_warehouses, name="np_warehouses"),
    path("orders/<int:order_id>/delete/", views.order_delete, name="order_delete"),
]
