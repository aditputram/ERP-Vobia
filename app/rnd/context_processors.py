from accounts.access import module_level

from .models import RndNotification


def rnd_notifications(request):
    user = getattr(request, "user", None)
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "pk", None)
        or request.GET.get("embed") == "1"
        or (not user.is_superuser and module_level(user, "rnd") == "none")
    ):
        return {
            "show_rnd_notifications": False,
            "rnd_notification_items": (),
            "rnd_notification_unread_count": 0,
            "rnd_notification_badge": "",
        }

    notifications = RndNotification.objects.filter(recipient=user).select_related("actor")
    unread_count = notifications.filter(read_at__isnull=True).count()
    return {
        "show_rnd_notifications": True,
        "rnd_notification_items": notifications[:30],
        "rnd_notification_unread_count": unread_count,
        "rnd_notification_badge": "99+" if unread_count > 99 else str(unread_count),
    }
