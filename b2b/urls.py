from django.urls import path, reverse_lazy
from django.contrib.auth import views as auth_views
from . import views
app_name='b2b'
urlpatterns=[
    path('signup/',views.signup,name='signup'),
    path('login/', auth_views.LoginView.as_view(
        template_name='b2b/login.html',
        redirect_authenticated_user=True
    ), name='login'),    path('logout/', views.logout_view, name='logout'),
    path('',views.dashboard,name='dashboard'),
    path('products/',views.product_list,name='product_list'),
    path('cart/',views.cart,name='cart'),
    path('add/<int:product_id>/',views.add_to_cart,name='add_to_cart'),
    path('submit/',views.submit_order,name='submit_order'),
    path('orders/<int:order_id>/',views.order_detail,name='order_detail'),
    path('orders/<int:order_id>/invoice/',views.invoice_print,name='invoice_print'),
    path('orders/<int:order_id>/waybill/',views.waybill_print,name='waybill_print'),
    path('orders/<int:order_id>/invoice.pdf',views.invoice_pdf,name='invoice_pdf'),
    path('orders/<int:order_id>/waybill.pdf',views.waybill_pdf,name='waybill_pdf'),
]
