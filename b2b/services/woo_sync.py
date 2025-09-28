import requests
from django.conf import settings


class WooClient:
    """WooCommerce REST client with explicit base and API root."""
    def __init__(self):
        root = settings.WOO_BASE_URL.rstrip("/")                      # e.g. https://herabuna.com.ua
        api_root = getattr(settings, "WOO_API_ROOT", "/wp-json/wc/v3").strip("/")
        self.api = f"{root}/{api_root}"                               # https://.../wp-json/wc/v3
        self.ck = settings.WOO_CONSUMER_KEY
        self.cs = settings.WOO_CONSUMER_SECRET

    def _get(self, path, params=None):
        url = f"{self.api}/{path.lstrip('/')}"
        params = params or {}
        params.update({"consumer_key": self.ck, "consumer_secret": self.cs, "per_page": 100})
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _put(self, path, data):
        url = f"{self.api}/{path.lstrip('/')}"
        params = {"consumer_key": self.ck, "consumer_secret": self.cs}
        r = requests.put(url, json=data, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    # Public API
    def fetch_products(self):
        return self._get("products", params={"status": "publish"})

    def fetch_variations(self, product_id: int):
        return self._get(f"products/{product_id}/variations")

    def update_stock(self, woo_id, stock_qty):
        return self._put(f"products/{woo_id}", {"stock_quantity": stock_qty, "manage_stock": True})

    def update_variation_stock(self, product_id, variation_id, stock_qty):
        return self._put(
            f"products/{product_id}/variations/{variation_id}",
            {"stock_quantity": stock_qty, "manage_stock": True},
        )

    def update_price(self, woo_id, price):
        return self._put(f"products/{woo_id}", {"regular_price": str(price)})
