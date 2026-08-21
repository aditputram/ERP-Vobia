from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import LoginThrottle, User


@admin.register(User)
class VobiaUserAdmin(UserAdmin):
    readonly_fields = ("last_login", "date_joined")


@admin.register(LoginThrottle)
class LoginThrottleAdmin(admin.ModelAdmin):
    list_display = ("username", "ip_address", "failure_count", "locked_until", "updated_at")
    search_fields = ("username", "ip_address")
    readonly_fields = (
        "username",
        "ip_address",
        "failure_count",
        "locked_until",
        "last_failed_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

