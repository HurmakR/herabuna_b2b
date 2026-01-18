from django import forms

from b2b.models import Product
from .models import InventoryLot, InboundReceiptLine


class ReceiveLotForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.order_by("sku"), label="Product")
    qty = forms.IntegerField(min_value=1, label="Qty")
    unit_cost = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, label="Unit cost")
    reference = forms.CharField(max_length=120, required=False, label="Reference")
    note = forms.CharField(max_length=200, required=False, label="Note")


class AdjustStockForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.order_by("sku"), label="Product")
    lot = forms.ModelChoiceField(
        queryset=InventoryLot.objects.select_related("product").order_by("product__sku", "-received_at", "-id"),
        required=False,
        label="Lot",
    )
    qty_delta = forms.IntegerField(label="Qty delta")
    note = forms.CharField(max_length=200, required=False, label="Note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].widget.attrs.update({"class": "form-select"})
        self.fields["lot"].widget.attrs.update({"class": "form-select"})
        self.fields["qty_delta"].widget.attrs.update({"class": "form-control", "placeholder": "Напр.: -3 або +5"})
        self.fields["note"].widget.attrs.update({"class": "form-control", "placeholder": "Причина/коментар"})

        # Make lot labels more informative (SKU + lot id + available)
        def _lot_label(obj: InventoryLot) -> str:
            return f"{obj.product.sku} · lot#{obj.id} · avail {obj.qty_available}/{obj.qty_in} · {obj.received_at:%Y-%m-%d}"

        self.fields["lot"].label_from_instance = _lot_label


class ReceiptHeaderForm(forms.Form):
    supplier = forms.CharField(max_length=120, required=False, label="Постачальник")
    external_ref = forms.CharField(max_length=120, required=False, label="Накладна / інвойс")
    currency = forms.ChoiceField(choices=(("UAH", "UAH"),), required=True, label="Валюта")
    received_date = forms.DateField(required=False, label="Дата надходження", widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(max_length=200, required=False, label="Нотатка")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].widget.attrs.update({"class": "form-control", "placeholder": "Напр. Old Ghost / Loonva / Huashi"})
        self.fields["external_ref"].widget.attrs.update({"class": "form-control", "placeholder": "№ або референс"})
        self.fields["currency"].widget.attrs.update({"class": "form-select"})
        self.fields["received_date"].widget.attrs.update({"class": "form-control"})
        self.fields["note"].widget.attrs.update({"class": "form-control", "placeholder": "Коментар до накладної"})


class ReceiptLineForm(forms.ModelForm):
    class Meta:
        model = InboundReceiptLine
        fields = ("product", "qty", "unit_cost")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.order_by("sku")
        self.fields["product"].widget.attrs.update({"class": "form-select form-select-sm"})
        self.fields["qty"].widget.attrs.update({"class": "form-control form-control-sm text-end", "min": "1"})
        self.fields["unit_cost"].widget.attrs.update({"class": "form-control form-control-sm text-end", "step": "0.01"})
