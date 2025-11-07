from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class LoginEvent(models.Model):
    """Immutable audit record for auth-related events."""
    STATUS_CHOICES = [
        ("success", "Success"),
        ("failed", "Failed"),
        ("logout", "Logout"),
    ]

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="login_events")
    username = models.CharField(max_length=150, blank=True)  # username attempted
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="Success",)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    path = models.CharField(max_length=512, blank=True)
    session_key = models.CharField(max_length=40, blank=True)
    is_staff = models.BooleanField(default=False)  # snapshot at event time
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["username"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.username or '-'} [{self.status}]"
