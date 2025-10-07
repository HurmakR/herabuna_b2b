from django.core.management.base import BaseCommand
from django.conf import settings
from b2b.services import telegram as tg

class Command(BaseCommand):
    help = "Send a test Telegram message to admin chat"

    def add_arguments(self, parser):
        parser.add_argument("--chat", help="Chat ID or @username; default=TELEGRAM_ADMIN_CHAT_ID")
        parser.add_argument("--text", help="Text to send", default="Herabuna B2B: Telegram test ✅")

    def handle(self, *args, **opts):
        chat = opts["chat"] or getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", None)
        if not chat:
            self.stderr.write("No chat configured. Set TELEGRAM_ADMIN_CHAT_ID or pass --chat")
            return
        ok = tg.send_message(chat, opts["text"])
        if ok:
            self.stdout.write(self.style.SUCCESS(f"Sent to {chat}"))
        else:
            self.stderr.write(self.style.ERROR("FAILED to send (see server logs)"))
