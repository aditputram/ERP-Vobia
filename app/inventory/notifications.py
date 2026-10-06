from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse

from accounts.access import can_access_tab
from chat.push import send_web_push
from rnd.models import RndNotification

from .models import InventoryException


WAREHOUSE_TABS = ("inventory_summary", "outbound", "inventory_turnover", "inbound", "return_log")


def _target_url(user, exception):
    movement = exception.movement
    sku = exception.sku.sku
    movement_date = movement.movement_date.isoformat()
    if can_access_tab(user, "operation", "inventory_summary"):
        return f'{reverse("inventory:overview")}?{urlencode({"q": sku, "as_of_date": movement_date})}'
    if can_access_tab(user, "operation", "outbound"):
        query = {
            "q": movement.sales_line.order.order_number,
            "date_from": movement_date,
            "date_to": movement_date,
        }
        return f'{reverse("inventory:outbound")}?{urlencode(query)}'
    if can_access_tab(user, "operation", "inventory_turnover"):
        return f'{reverse("inventory:turnover")}?{urlencode({"q": sku, "date_to": movement_date})}'
    if can_access_tab(user, "operation", "inbound"):
        return reverse("inventory:inbound")
    return reverse("inventory:return_log")


def sync_fifo_short_notification(exception, *, actor=None):
    """Notify Warehouse once for a canonical open FIFO shortage."""
    if (
        exception.code != InventoryException.Code.FIFO_SHORT
        or exception.status != InventoryException.Status.OPEN
        or not exception.movement_id
        or not exception.movement.sales_line_id
    ):
        return 0

    users = get_user_model().objects.filter(is_active=True)
    recipients = [
        user
        for user in users
        if user.is_superuser or any(can_access_tab(user, "operation", tab) for tab in WAREHOUSE_TABS)
    ]
    source_key = f"inventory-exception:{exception.id}"
    movement = exception.movement
    order = movement.sales_line.order
    product_name = exception.sku.product_variant.product.name
    quantity = exception.quantity.quantize(1)
    sale_date = movement.movement_date.strftime("%d %b %Y")
    message = (
        f"{order.display_source} {order.order_number} · {product_name} ({exception.sku.sku}) terjual pada "
        f"{sale_date}, tetapi FIFO kurang {quantity} pcs. Audit dan catat movement in."
    )
    notifications = []
    for user in recipients:
        notification, created = RndNotification.objects.get_or_create(
            recipient=user,
            source_key=source_key,
            defaults={
                "actor": actor,
                "module": RndNotification.Module.OPERATION,
                "title": "Stok warehouse belum tercatat",
                "message": message,
                "target_url": _target_url(user, exception),
            },
        )
        if created:
            notifications.append(notification)
    if not notifications:
        return 0

    targets = {}
    for notification in notifications:
        targets.setdefault(notification.target_url, []).append(notification.recipient_id)
    for target_url, recipient_ids in targets.items():
        transaction.on_commit(
            lambda ids=recipient_ids, url=target_url: send_web_push(
                ids,
                title="Stok Warehouse perlu diaudit",
                body=f"{exception.sku.sku} · FIFO kurang {quantity} pcs.",
                url=url,
                tag=source_key,
            ),
            robust=True,
        )
    return len(notifications)


def resolve_fifo_short_notification(exception):
    RndNotification.objects.filter(
        source_key=f"inventory-exception:{exception.id}",
        read_at__isnull=True,
    ).update(read_at=exception.resolved_at)
