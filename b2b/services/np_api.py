# Simple Nova Poshta TTN stub to be replaced with real API integration.

from datetime import datetime

def create_ttn(order) -> str:
    """
    Pretend to create TTN for the given order and return TTN string.
    Replace this stub with actual Nova Poshta API call.
    """
    # Example: "NP" + YYYYMMDD + order id
    return f"NP{datetime.utcnow():%Y%m%d}{order.id:06d}"
