import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"

def _api(method: str, **params):
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        return None
    url = API.format(token=token, method=method)
    try:
        r = requests.post(url, data=params, timeout=10)
        if not r.ok:
            logger.error("Telegram API %s failed: %s %s", method, r.status_code, r.text[:300])
        return r
    except Exception as e:
        logger.exception("Telegram API %s exception: %s", method, e)
        return None

def resolve_chat_id(chat: str | None) -> str | None:
    """Accept numeric id, -100..., or @username/@channelusername; return numeric id if possible."""
    if not chat:
        return None
    chat = str(chat).strip()
    # numeric id (user/group/supergroup)
    if chat.lstrip("-").isdigit():
        return chat
    # try @username/@channelusername
    resp = _api("getChat", chat_id=chat)
    if resp and resp.ok:
        data = resp.json()
        if data.get("ok") and data.get("result") and "id" in data["result"]:
            return str(data["result"]["id"])
    logger.warning("Cannot resolve chat id for '%s'", chat)
    return None

def send_message(chat: str | None, text: str) -> bool:
    chat_id = resolve_chat_id(chat)
    if not chat_id:
        logger.error("No valid chat_id to send message (chat=%r).", chat)
        return False
    resp = _api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
    ok = bool(resp and resp.ok)
    if not ok and resp is not None:
        logger.error("sendMessage failed: %s %s", resp.status_code, resp.text[:300])
    return ok

def notify_admins(text: str) -> bool:
    chat = getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", None)
    return send_message(chat, text) if chat else False

