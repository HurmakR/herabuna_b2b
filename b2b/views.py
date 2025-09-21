from django.contrib.auth.decorators import login_required
from django.contrib.auth import login
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from weasyprint import HTML
from .models import Product, Order, OrderItem
from .forms import DealerSignUpForm
from django.contrib.auth import logout

def signup(request):
    """Simple dealer sign up; set is_active=False if you want manual approval."""
    if request.method == 'POST':
        form = DealerSignUpForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = True  # flip to False for manual approval
            user.is_dealer = True
            user.save()
            login(request, user)
            return redirect('b2b:dashboard')
    else:
        form = DealerSignUpForm()
    return render(request, 'b2b/signup.html', {'form': form})

@login_required
def dashboard(request):
    orders = request.user.order_set.order_by('-created_at')[:10]
    return render(request, 'b2b/dashboard.html', {'orders': orders})

@login_required
def product_list(request):
    q = request.GET.get('q', '')
    products = Product.objects.filter(is_active=True)
    if q:
        products = products.filter(name__icontains=q) | products.filter(sku__icontains=q)
    return render(request, 'b2b/product_list.html', {'products': products, 'q': q})

@login_required
@transaction.atomic
def add_to_cart(request, product_id):
    product = get_object_or_404(Product, id=product_id, is_active=True)
    order, _ = Order.objects.get_or_create(dealer=request.user, status='draft')
    item, created = OrderItem.objects.get_or_create(
        order=order, product=product,
        defaults={'qty': 1, 'price': product.wholesale_price}
    )
    if not created:
        item.qty += 1
        item.save(update_fields=['qty'])
    order.recalc()
    return redirect('b2b:cart')

@login_required
def cart(request):
    order = Order.objects.filter(dealer=request.user, status='draft').first()
    return render(request, 'b2b/cart.html', {'order': order})


@login_required
@transaction.atomic
def submit_order(request):
    order = Order.objects.filter(dealer=request.user, status='draft').first()
    if not order or order.items.count() == 0:
        return redirect('b2b:product_list')
    # Reserve stock: reduce Product.stock_qty by ordered qty if available
    for item in order.items.select_related('product'):
        if item.product.stock_qty < item.qty:
            # If not enough stock, cap to available or reject; MVP: reject submission gracefully
            return render(request, 'b2b/cart.html', {'order': order, 'error': f"Недостатньо на складі для {item.product.sku}. Доступно: {item.product.stock_qty}"})
    for item in order.items.select_related('product'):
        item.product.stock_qty -= item.qty
        item.product.save(update_fields=['stock_qty'])

    order.status = 'submitted'
    order.recalc()
    order.save(update_fields=['status', 'subtotal', 'total'])
    return redirect('b2b:order_detail', order_id=order.id)
@login_required
def order_detail(request, order_id):
    order = get_object_or_404(Order, id=order_id, dealer=request.user)
    return render(request, 'b2b/order_detail.html', {'order': order})

@login_required
def invoice_print(request, order_id):
    order = get_object_or_404(Order, id=order_id, dealer=request.user)
    return render(request, 'b2b/invoice_print.html', {'order': order})

@login_required
def waybill_print(request, order_id):
    order = get_object_or_404(Order, id=order_id, dealer=request.user)
    return render(request, 'b2b/waybill_print.html', {'order': order})


def _render_pdf_from_template(request, template_name, context, filename_prefix):
    """Render a Django template to PDF and return as HttpResponse."""
    html_string = render(request, template_name, context).content.decode('utf-8')
    pdf = HTML(string=html_string, base_url=request.build_absolute_uri('/')).write_pdf()
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{filename_prefix}_{context.get("order").id}.pdf"'
    return response

@login_required
def invoice_pdf(request, order_id):
    order = get_object_or_404(Order, id=order_id, dealer=request.user)
    return _render_pdf_from_template(request, 'b2b/invoice_print.html', {'order': order}, 'invoice')

@login_required
def waybill_pdf(request, order_id):
    order = get_object_or_404(Order, id=order_id, dealer=request.user)
    return _render_pdf_from_template(request, 'b2b/waybill_print.html', {'order': order}, 'waybill')


@login_required
def logout_view(request):
    """Force logout and redirect to login page."""
    logout(request)
    return redirect('b2b:login')