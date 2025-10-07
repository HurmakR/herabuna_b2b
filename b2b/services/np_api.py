# b2b/services/np_api.py
# Nova Poshta integration: create InternetDocument (TTN) and fetch 100x100 label.
# Comments are in English only.

import re
import base64
import requests
from django.conf import settings

API_URL = "https://api.novaposhta.ua/v2.0/json/"


# ------------------------------- Low-level helpers -------------------------------

def _post(model: str, method: str, props: dict):
    """Low-level POST to Nova Poshta JSON API. Raises on API failure."""
    api_key = getattr(settings, "NOVA_POSHTA_API_KEY", None)
    if not api_key:
        raise RuntimeError("NOVA_POSHTA_API_KEY is not configured")

    payload = {
        "apiKey": api_key,
        "modelName": model,
        "calledMethod": method,
        "methodProperties": props or {},
    }
    r = requests.post(API_URL, json=payload, timeout=25)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        # errors/warnings are arrays; include them for easier debugging
        raise RuntimeError(f"NP error: {data.get('errors') or data}")
    return data.get("data") or []


def _normalize_phone(phone: str) -> str:
    """Normalize phone to +380XXXXXXXXX when possible."""
    if not phone:
        return ""
    digits = re.sub(r"\D+", "", phone)
    # +380XXXXXXXXX or 0XXXXXXXXX -> +380XXXXXXXXX
    if digits.startswith("380") and len(digits) == 12:
        return f"+{digits}"
    if digits.startswith("0") and len(digits) == 10:
        return f"+38{digits}"
    if len(digits) == 12 and digits.startswith("380"):
        return f"+{digits}"
    if phone.startswith("+") and len(phone) >= 10:
        return phone
    return f"+{digits}" if digits else ""


def _split_name(full_name: str):
    """
    Naive split into First/Middle/Last for NP.
    Keeps safe defaults if the full name is short.
    """
    full = (full_name or "").strip()
    if not full:
        return ("Одержувач", "", "")
    parts = full.split()
    if len(parts) == 1:
        return (parts[0], "", "")
    if len(parts) == 2:
        return (parts[0], "", parts[1])
    # 3 or more
    return (parts[0], " ".join(parts[1:-1]), parts[-1])


# ------------------------------- Weight calculation -------------------------------

def _compute_order_weight_kg(order) -> float:
    """
    Compute total order weight in kg using weight_g (grams) on product/variant.
    If some item has zero weight, it contributes 0. Min total weight is 0.1 kg.
    """
    total_g = 0
    # Select related to minimize queries
    for it in order.items.select_related("product", "variant"):
        per_g = 0
        if it.variant and getattr(it.variant, "weight_g", 0):
            per_g = int(it.variant.weight_g or 0)
        elif getattr(it.product, "weight_g", 0):
            per_g = int(it.product.weight_g or 0)
        total_g += per_g * int(it.qty or 0)

    kg = total_g / 1000.0
    if kg <= 0:
        kg = 0.1  # NP requires positive weight; use minimum 0.1 kg
    # Keep one decimal place
    kg = round(kg + 1e-9, 1)
    return kg


# ------------------------------- Recipient (Counterparty + Contact) -------------------------------

def _find_recipient_counterparty(search: str):
    """
    Try to find existing recipient counterparty by phone or name substring.
    Returns Ref or None.
    """
    if not search:
        return None
    try:
        rows = _post("Counterparty", "getCounterparties", {
            "CounterpartyProperty": "Recipient",
            "Page": "1",
            "FindByString": search,
        })
        return rows[0]["Ref"] if rows else None
    except Exception:
        return None


def _get_contacts(counterparty_ref: str):
    """Return list of contact persons for given counterparty."""
    try:
        return _post("ContactPerson", "getCounterpartyContactPersons", {"Ref": counterparty_ref})
    except Exception:
        return []


def _ensure_recipient_counterparty(first_name: str, middle_name: str, last_name: str, phone: str):
    """
    Find or create a recipient counterparty (PrivatePerson). Returns Ref.
    """
    # Prefer lookup by phone, then by name
    ref = _find_recipient_counterparty(phone) or _find_recipient_counterparty(f"{first_name} {last_name}".strip())
    if ref:
        return ref

    # Create new PrivatePerson recipient
    rows = _post("Counterparty", "save", {
        "CounterpartyProperty": "Recipient",
        "CounterpartyType": "PrivatePerson",
        "FirstName": first_name,
        "MiddleName": middle_name,
        "LastName": last_name,
        "Phone": phone,
        "Email": "",
    })
    return rows[0]["Ref"]


def _ensure_contact(counterparty_ref: str, first_name: str, middle_name: str, last_name: str, phone: str):
    """
    Find or create a contact person for the recipient counterparty. Returns Ref.
    """
    contacts = _get_contacts(counterparty_ref)
    norm_phone = re.sub(r"\D+", "", phone or "")
    for c in contacts:
        c_phone = re.sub(r"\D+", "", c.get("Phones") or "")
        fn = (c.get("FirstName") or "").strip()
        ln = (c.get("LastName") or "").strip()
        if (norm_phone and norm_phone == c_phone) or (fn == first_name and ln == last_name):
            return c["Ref"]

    rows = _post("ContactPerson", "save", {
        "CounterpartyRef": counterparty_ref,
        "FirstName": first_name,
        "MiddleName": middle_name,
        "LastName": last_name,
        "Phone": phone,
        "Email": "",
    })
    return rows[0]["Ref"]


# ------------------------------- Public API -------------------------------

def create_ttn(order) -> tuple[str, str]:
    """
    Create a real TTN (InternetDocument.save) and return (IntDocNumber, Ref).
    Shipping fee is paid by the recipient in cash (PayerType=Recipient, PaymentMethod=Cash).
    Uses snapshot fields from the order for recipient delivery data.
    """
    # Validate required sender refs from settings
    sender_ref = getattr(settings, "NP_SENDER_REF", None)
    sender_contact_ref = getattr(settings, "NP_SENDER_CONTACT_REF", None)
    sender_wh_ref = getattr(settings, "NP_SENDER_WAREHOUSE_REF", None)
    sender_city_ref = getattr(settings, "NP_SENDER_CITY_REF", None)
    if not all([sender_ref, sender_contact_ref, sender_wh_ref, sender_city_ref]):
        raise RuntimeError("NP sender refs are not configured (check NP_SENDER_* vars).")

    # Snapshot recipient data from order
    rec_name = (order.shipping_recipient or "").strip()
    phone = _normalize_phone(order.shipping_phone or "")
    first, middle, last = _split_name(rec_name)

    # Ensure recipient Counterparty + Contact exist and get Refs
    recip_ref = _ensure_recipient_counterparty(first, middle, last, phone)
    contact_ref = _ensure_contact(recip_ref, first, middle, last, phone)

    # Weight and declared cost
    weight = _compute_order_weight_kg(order)
    cost = str(order.total)

    # Prepare InternetDocument.save properties
    props = {
        # Recipient pays delivery in cash:
        "PayerType": "Recipient",
        "PaymentMethod": "Cash",

        "CargoType": "Parcel",
        "Weight": str(weight),
        "ServiceType": "WarehouseWarehouse",
        "SeatsAmount": "1",
        "Description": f"Order #{order.id}",
        "Cost": cost,

        "CitySender": sender_city_ref,
        "Sender": sender_ref,
        "SenderAddress": sender_wh_ref,
        "ContactSender": sender_contact_ref,
        "SendersPhone": getattr(order.dealer, "phone", "") or "",

        "CityRecipient": order.shipping_city_ref,
        "RecipientAddress": order.shipping_warehouse_ref,

        # Explicit recipient references (required by your NP account rules)
        "Recipient": recip_ref,
        "ContactRecipient": contact_ref,

        # Keep PrivatePerson details for redundancy
        "RecipientType": "PrivatePerson",
        "RecipientsPhone": phone,
        "RecipientsFirstName": first,
        "RecipientsMiddleName": middle,
        "RecipientsLastName": last,
    }

    data = _post("InternetDocument", "save", props)
    doc = data[0] if data else {}
    ttn = doc.get("IntDocNumber")
    ref = doc.get("Ref")
    if not ttn:
        raise RuntimeError("No IntDocNumber returned from NP.")
    return ttn, (ref or "")


def get_label_100x100_pdf_by_ref(doc_ref: str, ttn_number: str = "") -> bytes:
    """
    Try JSON printMarking100x100 first. If NP returns 500 or no file,
    fallback to legacy HTTP endpoint on my.novaposhta.ua.
    Returns raw PDF bytes.
    """
    if not doc_ref and not ttn_number:
        raise RuntimeError("Document Ref or TTN number is required")

    # --- Attempt 1: JSON API ---
    try:
        if doc_ref:
            rows = _post("InternetDocument", "printMarking100x100", {"DocumentRefs": [doc_ref]})
            if rows:
                file_field = rows[0].get("file") or ""
                if file_field:
                    if file_field.startswith("http"):
                        r = requests.get(file_field, timeout=25)
                        r.raise_for_status()
                        return r.content
                    # base64 case
                    try:
                        return base64.b64decode(file_field)
                    except Exception:
                        pass
    except Exception:
        # fall through to HTTP fallback
        pass

    # --- Attempt 2: Legacy HTTP fallback ---
    api_key = getattr(settings, "NOVA_POSHTA_API_KEY", None)
    if not api_key:
        raise RuntimeError("NOVA_POSHTA_API_KEY is not configured")

    ref_or_num = doc_ref or ttn_number
    # Primary fallback (PDF, 100x100). Suffix '/zebra' часто потрібен для термодруку,
    # але для PDF теж працює; якщо ні — спробуємо без '/zebra'.
    urls = [
        f"https://my.novaposhta.ua/orders/printMarking100x100/orders[]/{ref_or_num}/type/pdf/apiKey/{api_key}/zebra",
        f"https://my.novaposhta.ua/orders/printMarking100x100/orders[]/{ref_or_num}/type/pdf/apiKey/{api_key}",
    ]
    last_err = None
    for url in urls:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            # Очікуємо PDF
            if r.headers.get("Content-Type", "").lower().startswith("application/pdf") or r.content.startswith(b"%PDF"):
                return r.content
            # Інколи повертає HTML із помилкою — продовжимо до наступної спроби
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"Failed to fetch NP 100x100 label via JSON and HTTP fallback: {last_err or 'unknown error'}")
