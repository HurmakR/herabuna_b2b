from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

class Dealer(AbstractUser):
    company_name=models.CharField(max_length=255,blank=True)
    edrpou=models.CharField(max_length=32,blank=True)
    vat_number=models.CharField(max_length=64,blank=True)
    phone=models.CharField(max_length=64,blank=True)
    billing_address=models.TextField(blank=True)
    shipping_address=models.TextField(blank=True)
    is_dealer=models.BooleanField(default=True)

class Product(models.Model):
    sku=models.CharField(max_length=64,unique=True)
    name=models.CharField(max_length=255)
    wholesale_price=models.DecimalField(max_digits=10,decimal_places=2,default=0)
    retail_price=models.DecimalField(max_digits=10,decimal_places=2,default=0)
    stock_qty=models.IntegerField(default=0)
    is_active=models.BooleanField(default=True)
    woo_id=models.BigIntegerField(null=True,blank=True)
    def __str__(self): return f"{self.sku} — {self.name}"

class Order(models.Model):
    STATUS_CHOICES=[('draft','Draft'),('submitted','Submitted'),('confirmed','Confirmed'),('fulfilled','Fulfilled'),('cancelled','Cancelled')]
    dealer=models.ForeignKey(Dealer,on_delete=models.PROTECT)
    created_at=models.DateTimeField(default=timezone.now)
    status=models.CharField(max_length=16,choices=STATUS_CHOICES,default='draft')
    subtotal=models.DecimalField(max_digits=12,decimal_places=2,default=0)
    total=models.DecimalField(max_digits=12,decimal_places=2,default=0)
    note=models.TextField(blank=True)
    def recalc(self):
        subtotal=sum(i.qty*i.price for i in self.items.all())
        self.subtotal=subtotal; self.total=subtotal
        self.save(update_fields=['subtotal','total'])

class OrderItem(models.Model):
    order=models.ForeignKey(Order,related_name='items',on_delete=models.CASCADE)
    product=models.ForeignKey(Product,on_delete=models.PROTECT)
    qty=models.PositiveIntegerField(default=1)
    price=models.DecimalField(max_digits=10,decimal_places=2)
    @property
    def line_total(self): return self.qty*self.price
    class Meta: unique_together=('order','product')
