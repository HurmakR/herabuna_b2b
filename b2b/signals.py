# b2b/signals.py
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.core.mail import send_mail
from django.conf import settings
from django.contrib.auth import get_user_model

Dealer = get_user_model()

@receiver(pre_save, sender=Dealer)
def mark_prev_active(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = Dealer.objects.get(pk=instance.pk)
            instance._was_active = old.is_active
        except Dealer.DoesNotExist:
            instance._was_active = instance.is_active
    else:
        instance._was_active = instance.is_active

@receiver(post_save, sender=Dealer)
def notify_activation(sender, instance, created, **kwargs):
    # новостворених не чіпаємо; реагуємо тільки на зміну з False->True
    if created:
        return
    was = getattr(instance, "_was_active", instance.is_active)
    if not was and instance.is_active and instance.email:
        try:
            send_mail(
                subject="Ваш акаунт активовано — Herabuna B2B",
                message=(
                    f"Вітаємо, {instance.username}!\n\n"
                    "Ваш акаунт дилера активовано. Ви можете увійти на b2b.herabuna.com.ua "
                    "та оформлювати замовлення.\n\n"
                    "З повагою,\nКоманда Herabuna"
                ),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[instance.email],
                fail_silently=True,
            )
        except Exception:
            pass
