# reports/admin_views.py
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Count, Max, Q
from django.utils import timezone
from datetime import timedelta

from b2b.models import Dealer


def _is_superuser(u):
    return u.is_active and u.is_superuser


@user_passes_test(_is_superuser)
def activity_monitor(request):
    """User activity monitor — login events + dealer list."""
    from audit.models import LoginEvent

    period = int(request.GET.get("period") or 30)
    search = (request.GET.get("q") or "").strip()
    since = timezone.now() - timedelta(days=period)

    # Recent login events
    events_qs = LoginEvent.objects.filter(created_at__gte=since).select_related("user")
    if search:
        events_qs = events_qs.filter(
            Q(username__icontains=search) | Q(ip__icontains=search)
        )
    events = list(events_qs[:200])

    # Per-user stats
    stats = (
        LoginEvent.objects
        .filter(created_at__gte=since)
        .values("username")
        .annotate(
            total=Count("id"),
            success=Count("id", filter=Q(status="success")),
            failed=Count("id", filter=Q(status="failed")),
            last_seen=Max("created_at"),
        )
        .order_by("-last_seen")[:50]
    )

    context = {
        "events": events,
        "stats": stats,
        "period": period,
        "search": search,
    }
    return render(request, "reports/admin_activity.html", context)


@user_passes_test(_is_superuser)
def activation_requests(request):
    """List of inactive dealers waiting for activation."""

    if request.method == "POST":
        dealer_id = request.POST.get("dealer_id")
        action = request.POST.get("action")
        dealer = get_object_or_404(Dealer, id=dealer_id, is_dealer=True)

        if action == "activate":
            dealer.is_active = True
            dealer.save(update_fields=["is_active"])
            # Send activation email
            try:
                from django.core.mail import send_mail
                from django.conf import settings
                send_mail(
                    subject="Ваш акаунт активовано — Herabuna B2B",
                    message=(
                        f"Вітаємо, {dealer.username}!\n\n"
                        "Ваш акаунт дилера активовано. "
                        "Тепер ви можете увійти на портал: https://b2b.herabuna.com.ua\n\n"
                        "З повагою, команда Herabuna"
                    ),
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    recipient_list=[dealer.email] if dealer.email else [],
                    fail_silently=True,
                )
            except Exception:
                pass
            messages.success(request, f"Акаунт {dealer.username} активовано, лист надіслано.")

        elif action == "reject":
            dealer.delete()
            messages.warning(request, f"Акаунт {dealer.username} видалено.")

        return redirect("reports:activation_requests")

    # GET — list pending dealers
    pending = Dealer.objects.filter(
        is_active=False, is_dealer=True
    ).order_by("-id")

    context = {"pending": pending}
    return render(request, "reports/admin_activation.html", context)
