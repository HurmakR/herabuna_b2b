import requests
from django.conf import settings

API_URL = "https://api.novaposhta.ua/v2.0/json/"

def _post(model_name: str, called_method: str, method_props: dict):
    """Low-level NP API POST wrapper."""
    key = getattr(settings, "NOVA_POSHTA_API_KEY", None)
    if not key:
        return []
    payload = {
        "apiKey": key,
        "modelName": model_name,
        "calledMethod": called_method,
        "methodProperties": method_props or {},
    }
    r = requests.post(API_URL, json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        return []
    return data.get("data") or []

def search_cities(query: str, limit: int = 20):
    """Return [{'name': 'Київ', 'ref': '...'}, ...] filtered by query."""
    if not query:
        return []
    rows = _post("Address", "getCities", {"FindByString": query, "Limit": limit})
    out = []
    for row in rows:
        name = row.get("Description") or row.get("DescriptionUa") or ""
        ref = row.get("Ref")
        if name and ref:
            out.append({"name": name, "ref": ref})
    return out

def get_warehouses(city_ref: str, query: str = "", limit: int = 50):
    """Return warehouses for a city [{'name': 'Відділення №1 ...', 'ref': '...'}, ...]."""
    if not city_ref:
        return []
    props = {"CityRef": city_ref, "Limit": limit}
    if query:
        props["FindByString"] = query
    rows = _post("AddressGeneral", "getWarehouses", props)
    out = []
    for row in rows:
        name = row.get("Description") or row.get("DescriptionUa") or ""
        ref = row.get("Ref")
        if name and ref:
            out.append({"name": name, "ref": ref})
    return out
