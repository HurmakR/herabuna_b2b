from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class Dealer(AbstractUser):
    company_name = models.CharField(max_length=255, blank=True)
    edrpou = models.CharField(max_length=32, blank=True)
    vat_number = models.CharField(max_length=64, blank=True)
    phone = models.CharField(max_length=64, blank=True)
    billing_address = models.TextField(blank=True)
    shipping_address = models.TextField(blank=True)
    is_dealer = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Dealer"
        verbose_name_plural = "Dealers"


class Category(models.Model):
    """Product category mapped from Woo; can be hierarchical."""
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, blank=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE)
    woo_id = models.BigIntegerField(null=True, blank=True, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Category"
        verbose_name_plural = "Categories"

    def __str__(self) -> str:
        return self.name


class Brand(models.Model):
    """Brand as a dedicated entity for filtering and display."""
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, blank=True)
    woo_id = models.BigIntegerField(null=True, blank=True, unique=True)
    logo_url = models.URLField(blank=True)

    def __str__(self) -> str:
        return self.name


class Facet(models.Model):
    """Generic filterable descriptor (not an order option)."""
    TYPE_CHOICES = [
        ("ingredient", "Ingredient"),
        ("effect", "EffectiveFor"),
        ("season", "Season"),
    ]
    type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, blank=True)
    woo_term_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        unique_together = ("type", "name")

    def __str__(self) -> str:
        return f"{self.type}: {self.name}"


class Product(models.Model):
    """
    Product mirror for B2B. Wholesale price is edited manually in admin.
    Contains descriptive fields (brand, facets, media) and may have variants.
    """
    sku = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    wholesale_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    retail_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    stock_qty = models.IntegerField(default=0)  # for simple products or aggregated for variable
    is_active = models.BooleanField(default=True)
    woo_id = models.BigIntegerField(null=True, blank=True)

    # Descriptive fields
    short_description = models.TextField(blank=True)
    description = models.TextField(blank=True)
    weight_g = models.PositiveIntegerField(default=0)  # stored in grams
    main_image_url = models.URLField(blank=True)
    gallery = models.JSONField(default=list, blank=True)        # list of image URLs
    attributes = models.JSONField(default=dict, blank=True)     # informational attributes (not options)
    categories = models.ManyToManyField("Category", through="ProductCategory", related_name="products")
    brand = models.ForeignKey(Brand, null=True, blank=True, on_delete=models.SET_NULL, related_name="products")
    facets = models.ManyToManyField(Facet, blank=True, related_name="products")

    def __str__(self) -> str:
        return f"{self.sku} — {self.name}"

    @staticmethod
    def _format_weight(weight_g: int) -> str:
        """Return a localized human-readable weight label."""
        if not weight_g:
            return ""
        if weight_g >= 1000:
            kg = weight_g / 1000.0
            # Trim trailing zeros (e.g., 2.0 -> 2)
            kg_str = f"{kg:.2f}".rstrip("0").rstrip(".")
            return f"{kg_str} кг"
        return f"{weight_g} г"

    @property
    def name_with_weight(self) -> str:
        """Return name with weight suffix appended after comma if weight exists."""
        if self.weight_g:
            return f"{self.name}, {self._format_weight(self.weight_g)}"
        return self.name


class ProductCategory(models.Model):
    """Through model for Product <-> Category relation."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)

    class Meta:
        unique_together = ("product", "category")


class ProductImage(models.Model):
    """Optional per-product images for admin UX."""
    product = models.ForeignKey(Product, related_name="images", on_delete=models.CASCADE)
    url = models.URLField()
    position = models.PositiveIntegerField(default=0)
    alt = models.CharField(max_length=255, blank=True)
    is_main = models.BooleanField(default=False)

    class Meta:
        ordering = ["position"]


class ProductVariant(models.Model):
    """Concrete purchasable option (e.g., length/line/connector)."""
    product = models.ForeignKey(Product, related_name="variants", on_delete=models.CASCADE)
    woo_variation_id = models.BigIntegerField(unique=True)
    sku = models.CharField(max_length=128, blank=True)
    attributes = models.JSONField(default=dict, blank=True)   # {"Length":"5.4","Line":"2.0","Connector":"Ring"}
    retail_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    wholesale_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    stock_qty = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    image_url = models.URLField(blank=True)
    weight_g = models.PositiveIntegerField(default=0)  # variant-specific weight if provided by Woo

    def __str__(self) -> str:
        return f"{self.product.sku} / {self.sku or self.woo_variation_id}"

    @property
    def name_with_weight(self) -> str:
        """Return product name, optionally suffixed with variant weight."""
        weight = self.weight_g or 0
        if weight:
            return f"{self.product.name}, {Product._format_weight(weight)}"
        # fallback to product-level weight if variant has none
        return self.product.name_with_weight



class Order(models.Model):
    """Dealer order with a simple lifecycle."""
    STATUS_CHOICES = [
        ("draft", "Чернетка"),
        ("submitted", "Очікує підтвердження"),
        ("pending_payment", "Очікує оплату"),
        ("shipped", "Відправлено"),
        ("cancelled", "Скасовано"),
    ]

    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")

    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.TextField(blank=True)

    # Shipping info (filled on shipment)
    shipping_provider = models.CharField(max_length=64, blank=True, default="Nova Poshta")
    shipping_ttn = models.CharField(max_length=64, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)

    def recalc(self):
        """Recalculate totals based on items."""
        subtotal = sum(i.qty * i.price for i in self.items.all())
        self.subtotal = subtotal
        self.total = subtotal
        self.save(update_fields=["subtotal", "total"])


class OrderItem(models.Model):
    """Line item for either simple product or specific variant."""
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    variant = models.ForeignKey(ProductVariant, null=True, blank=True, on_delete=models.PROTECT)
    qty = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    variant_attrs = models.JSONField(default=dict, blank=True)  # snapshot of selected options

    class Meta:
        unique_together = ("order", "product", "variant")

    @property
    def line_total(self):
        """Compute total for this line item."""
        return self.qty * self.price
