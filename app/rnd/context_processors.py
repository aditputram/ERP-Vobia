from urllib.parse import urlsplit

from accounts.access import allowed_tab_keys, module_level
from accounts.context_processors import current_business_module
from django.urls import Resolver404, resolve, reverse

from .models import Collection, DesignAsset, DevelopmentProduct, RndNotification
from .notifications import notification_category
from .services import can_approve_module


NOTIFICATION_MODULES = (
    (RndNotification.Module.SALES, "Sales"),
    (RndNotification.Module.OPERATION, "Operation"),
    (RndNotification.Module.RND, "R&D"),
    (RndNotification.Module.MARKETING, "Marketing"),
    (RndNotification.Module.FINANCE, "Finance"),
    (RndNotification.Module.HRGA, "HRGA"),
)


def available_notification_modules(user):
    explicit_access = getattr(user, "module_access", {}) or {}
    return [
        {"key": key, "label": label}
        for key, label in NOTIFICATION_MODULES
        if user.is_superuser
        or (
            key == RndNotification.Module.HRGA
            and explicit_access.get("hrga", "none") != "none"
        )
        or (
            key != RndNotification.Module.HRGA
            and module_level(user, key) != "none"
            and bool(allowed_tab_keys(user, key))
        )
    ]


def _product_thumbnail(product):
    if not product.product_cover:
        return ""
    return (
        f'{reverse("rnd:product_file", args=[product.id, "product-cover"])}'
        f'?size=card&v={int(product.updated_at.timestamp())}'
    )


def _attach_notification_thumbnails(notifications):
    targets = {}
    ids = {"design": set(), "product": set(), "collection": set()}
    route_kinds = {
        "rnd:design_detail": "design",
        "rnd:product_detail": "product",
        "rnd:development_product_detail": "product",
        "rnd:collection_detail": "collection",
        "rnd:development_detail": "collection",
    }
    for notification in notifications:
        notification.thumbnail_url = ""
        try:
            match = resolve(urlsplit(notification.target_url).path)
        except Resolver404:
            continue
        kind = route_kinds.get(match.view_name)
        object_id = match.kwargs.get(
            "design_id" if kind == "design" else "product_id" if kind == "product" else "collection_id"
        )
        if kind and object_id:
            targets[notification.id] = (kind, object_id)
            ids[kind].add(object_id)

    urls = {}
    for design in DesignAsset.objects.filter(id__in=ids["design"]).exclude(image=""):
        urls[("design", design.id)] = f'{reverse("rnd:design_file", args=[design.id])}?size=card'
    for product in DevelopmentProduct.objects.filter(id__in=ids["product"]).exclude(product_cover=""):
        urls[("product", product.id)] = _product_thumbnail(product)
    for product in (
        DevelopmentProduct.objects.filter(collection_id__in=ids["collection"])
        .exclude(product_cover="")
        .order_by("collection_id", "created_at")
    ):
        urls.setdefault(("collection", product.collection_id), _product_thumbnail(product))
    for notification in notifications:
        notification.thumbnail_url = urls.get(targets.get(notification.id), "")


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
                "thumbnail_url": _product_thumbnail(product),
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
            "thumbnail_url": _product_thumbnail(product),
        })
    return sorted(items, key=lambda item: item["created_at"], reverse=True)[:30]


def rnd_notifications(request):
    user = getattr(request, "user", None)
    module_options = available_notification_modules(user) if getattr(user, "is_authenticated", False) else []
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "pk", None)
        or request.GET.get("embed") == "1"
        or not module_options
    ):
        return {
            "show_rnd_notifications": False,
            "notification_modules": (),
            "rnd_notification_items": (),
            "rnd_notification_unread_count": 0,
            "rnd_notification_badge": "",
            "rnd_approval_items": (),
            "rnd_approval_count": 0,
        }

    module_keys = [row["key"] for row in module_options]
    active_module = current_business_module(request)
    if active_module not in module_keys:
        active_module = module_keys[0]
    for row in module_options:
        row["active"] = row["key"] == active_module

    notification_query = RndNotification.objects.filter(
        recipient=user,
        module__in=module_keys,
    ).select_related("actor")
    unread_count = notification_query.filter(read_at__isnull=True).count()
    notifications = list(notification_query[:30])
    for notification in notifications:
        notification.category = notification_category(notification)
    _attach_notification_thumbnails(notifications)
    approval_items = _approval_items(user) if RndNotification.Module.RND in module_keys else []
    approval_count = len(approval_items) + sum(
        notification.category == "approval" for notification in notifications
    )
    return {
        "show_rnd_notifications": True,
        "notification_modules": module_options,
        "rnd_notification_items": notifications,
        "rnd_notification_unread_count": unread_count,
        "rnd_notification_badge": "99+" if unread_count > 99 else str(unread_count),
        "rnd_approval_items": approval_items,
        "rnd_approval_count": approval_count,
    }
