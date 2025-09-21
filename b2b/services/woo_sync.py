import requests
from django.conf import settings

class WooClient:
    """Minimal WooCommerce REST client using basic key/secret auth."""
    def __init__(self):
        self.base_url = settings.WOO_BASE_URL.rstrip('/')
        self.ck = settings.WOO_CONSUMER_KEY
        self.cs = settings.WOO_CONSUMER_SECRET

    def _get(self, path, params=None):
        params = params or {}
        params.update({'consumer_key': self.ck, 'consumer_secret': self.cs, 'per_page': 100})
        r = requests.get(f"{self.base_url}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _put(self, path, data):
        params = {'consumer_key': self.ck, 'consumer_secret': self.cs}
        r = requests.put(f"{self.base_url}{path}", json=data, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def fetch_products(self):
        return self._get('/wp-json/wc/v3/products', params={'status': 'publish'})

    def update_stock(self, woo_id, stock_qty):
        data = {'stock_quantity': stock_qty, 'manage_stock': True}
        return self._put(f'/wp-json/wc/v3/products/{woo_id}', data)

    def update_price(self, woo_id, price):
        data = {'regular_price': str(price)}
        return self._put(f'/wp-json/wc/v3/products/{woo_id}', data)
