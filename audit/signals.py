from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver
from django.utils import timezone
from django.contrib.auth import get_user_model
from .models import LoginEvent

User = get_user_model()

def _client_ip(request):
    """Best-effort client IP extraction."""
    # Honor common proxy header first; fall back to REMOTE_ADDR.
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")

@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    """Persist successful login event."""
    LoginEvent.objects.create(
        user=user,
        username=user.username,
        status="success",
        ip=_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        path=(request.path or ""),
        session_key=getattr(request, "session", None) and request.session.session_key or "",
        is_staff=bool(getattr(user, "is_staff", False)),
        created_at=timezone.now(),
    )

@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    """Persist logout event."""
    LoginEvent.objects.create(
        user=user if isinstance(user, User) else None,
        username=getattr(user, "username", "") if user else "",
        status="logout",
        ip=_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        path=(request.path or ""),
        session_key=getattr(request, "session", None) and request.session.session_key or "",
        is_staff=bool(getattr(user, "is_staff", False)) if user else False,
        created_at=timezone.now(),
    )

@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request, **kwargs):
    """Persist failed login attempt."""
    username = (credentials or {}).get("username", "")
    LoginEvent.objects.create(
        user=None,
        username=username or "",
        status="failed",
        ip=_client_ip(request) if request else None,
        user_agent=request.META.get("HTTP_USER_AGENT", "") if request else "",
        path=(request.path or "") if request else "",
        session_key="",
        is_staff=False,
        created_at=timezone.now(),
    )
