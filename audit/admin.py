from django.contrib import admin
from .models import LoginEvent

@admin.register(LoginEvent)
class LoginEventAdmin(admin.ModelAdmin):
    """Read-only audit admin with useful filters and search."""
    list_display = ("created_at", "status", "username", "user", "ip", "is_staff", "path")
    list_filter = ("status", "is_staff", "created_at")
    search_fields = ("username", "user__username", "ip", "user_agent", "path", "session_key")
    date_hierarchy = "created_at"
    readonly_fields = ("user", "username", "status", "ip", "user_agent", "path",
                       "session_key", "is_staff", "created_at")

    def has_add_permission(self, request):  # pragma: no cover
        # Prevent manual creation in admin.
        return False

    def has_change_permission(self, request, obj=None):  # pragma: no cover
        # Make entries immutable.
        return False
