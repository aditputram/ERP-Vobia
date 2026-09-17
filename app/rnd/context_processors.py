from accounts.access import module_level
from django.urls import reverse

from .models import Collection, DevelopmentProduct, RndNotification
from .services import can_approve_module


def _approval_items(user):
    if not can_approve_module(user, "rnd"):
        return []

    items = []
    if user.is_superuser:
        for product in DevelopmentProduct.objects.filter(
            document_status=DevelopmentProduct.DocumentStatus.SUBMITTED,
        ).select_related("collection"):
            items.append({
                "title": "Approve dokumen",
                "message": f"{product.name} · {product.collection.name}",
                "target_url": reverse("rnd:product_detail", args=[product.id]),
                "created_at": product.submitted_at or product.updated_at,
            })
    for product in DevelopmentProduct.objects.filter(
        collection__status=Collection.Status.DEVELOPMENT,
        development_stage=DevelopmentProduct.DevelopmentStage.PROTOTYPE,
    ).select_related("collection"):
        items.append({
            "title": f"Putuskan Prototype {product.prototype_number}",
            "message": f"{product.name} · {product.collection.name}",
            "target_url": reverse("rnd:development_product_detail", args=[product.id]),
            "created_at": product.updated_at,
        })
    return sorted(items, key=lambda item: item["created_at"], reverse=True)[:30]


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
            "rnd_show_approval_tab": False,
            "rnd_approval_items": (),
            "rnd_approval_count": 0,
        }

    notifications = RndNotification.objects.filter(recipient=user).select_related("actor")
    unread_count = notifications.filter(read_at__isnull=True).count()
    approval_items = _approval_items(user)
    return {
        "show_rnd_notifications": True,
        "rnd_notification_items": notifications[:30],
        "rnd_notification_unread_count": unread_count,
        "rnd_notification_badge": "99+" if unread_count > 99 else str(unread_count),
        "rnd_show_approval_tab": can_approve_module(user, "rnd"),
        "rnd_approval_items": approval_items,
        "rnd_approval_count": len(approval_items),
    }
