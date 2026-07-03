from django import forms

from b2b.models import Product, ProductVariant
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
        # Filled dynamically based on selected product.
        queryset=InventoryLot.objects.none(),
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

        # Filter lots by the selected product.
        product_obj = None
        if self.is_bound:
            raw = (self.data.get("product") or "").strip()
            if raw.isdigit():
                product_obj = Product.objects.filter(id=int(raw)).first()
        else:
            init = self.initial.get("product")
            if isinstance(init, Product):
                product_obj = init
            elif isinstance(init, int):
                product_obj = Product.objects.filter(id=init).first()
            elif isinstance(init, str) and init.isdigit():
                product_obj = Product.objects.filter(id=int(init)).first()

        if product_obj is not None:
            self.fields["lot"].queryset = (
                InventoryLot.objects.select_related("product")
                .filter(product=product_obj)
                .order_by("-received_at", "-id")
            )

        # Make lot labels more informative (SKU + lot id + available)
        def _lot_label(obj: InventoryLot) -> str:
            return f"{obj.product.sku} · lot#{obj.id} · avail {obj.qty_available}/{obj.qty_in} · {obj.received_at:%Y-%m-%d}"

        self.fields["lot"].label_from_instance = _lot_label

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        lot = cleaned.get("lot")
        if product and lot and lot.product_id != product.id:
            self.add_error("lot", "Обраний лот не належить до вибраного товару.")
        return cleaned


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
        fields = ("product", "variant", "qty", "unit_cost")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.order_by("sku")
        self.fields["product"].widget.attrs.update({
            "class": "form-select form-select-sm receipt-product-select",
        })
        self.fields["variant"].queryset = ProductVariant.objects.none()
        self.fields["variant"].required = False
        self.fields["variant"].empty_label = "— без варіації —"
        self.fields["variant"].widget.attrs.update({
            "class": "form-select form-select-sm receipt-variant-select",
        })
        self.fields["qty"].widget.attrs.update({"class": "form-control form-control-sm text-end", "min": "1"})
        self.fields["unit_cost"].widget.attrs.update({"class": "form-control form-control-sm text-end", "step": "0.01"})

        # If editing existing line — populate variant queryset
        product_obj = None
        if self.instance and self.instance.pk and self.instance.product_id:
            product_obj = self.instance.product
        elif self.is_bound:
            raw = (self.data.get(self.add_prefix("product")) or "").strip()
            if raw.isdigit():
                product_obj = Product.objects.filter(id=int(raw)).first()

        if product_obj:
            self.fields["variant"].queryset = (
                ProductVariant.objects.filter(product=product_obj, is_active=True).order_by("id")
            )

    def _variant_label(self, obj):
        attrs = obj.attributes or {}
        label = ", ".join(f"{k}: {v}" for k, v in attrs.items())
        return f"{obj.sku or label} (залишок: {obj.stock_qty})"

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        variant = cleaned.get("variant")
        if variant and product and variant.product_id != product.id:
            self.add_error("variant", "Варіація не належить до вибраного товару.")
        return cleaned
