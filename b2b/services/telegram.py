import requests
from django.conf import settings

def send_message(chat_id: str, text: str) -> bool:
    """Send a Telegram text message. Returns True on HTTP 200."""
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.ok
    except Exception:
        return False

def notify_admins(text: str) -> bool:
    """Send to admin chat configured in settings."""
    chat_id = getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", None)
    return send_message(chat_id, text) if chat_id else False
