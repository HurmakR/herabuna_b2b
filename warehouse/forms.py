from django import forms

from b2b.models import Product
from .models import InventoryLot


class ReceiveLotForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.all(), label="Product")
    qty = forms.IntegerField(min_value=1, label="Qty")
    unit_cost = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, label="Unit cost")
    reference = forms.CharField(max_length=120, required=False, label="Reference")
    note = forms.CharField(max_length=200, required=False, label="Note")


class AdjustStockForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.all(), label="Product")
    lot = forms.ModelChoiceField(
        queryset=InventoryLot.objects.select_related("product").all(),
        required=False,
        label="Lot",
    )
    qty_delta = forms.IntegerField(label="Qty delta")
    note = forms.CharField(max_length=200, required=False, label="Note")
