from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from inventory.models import InventoryMovement
from sales.models import SalesOrderLine

from ..models import (
    IncomingPlan,
    MerchandisingMonthlySnapshot,
    MerchandisingSnapshotBatch,
    SalesProjection,
)
from .official_projection import official_current_month_values, official_planning_state


ZERO = Decimal("0")


def previous_calendar_month(value):
    return date(
        value.year - (1 if value.month == 1 else 0),
        12 if value.month == 1 else value.month - 1,
        1,
    )


def next_calendar_month(value):
    return date(
        value.year + (1 if value.month == 12 else 0),
        1 if value.month == 12 else value.month + 1,
        1,
    )


def planning_activity_snapshot(as_of_date=None, target_month=None):
    """Return 30-day activity and Ending month M-1 for target planning month M."""
    as_of_date = as_of_date or timezone.localdate()
    window_start = as_of_date - timedelta(days=29)
    indicator_month = previous_calendar_month(target_month) if target_month else previous_calendar_month(as_of_date)

    sales_by_sku = {
        row["sku_id"]: row["qty"] or ZERO
        for row in SalesOrderLine.objects.filter(
            is_counted=True,
            sku_id__isnull=False,
            order__order_date__gte=window_start,
            order__order_date__lte=as_of_date,
        )
        .exclude(order__current_status="Retur")
        .values("sku_id")
        .annotate(qty=Sum("quantity"))
    }
    inbound_by_sku = {
        row["sku_id"]: row["qty"] or ZERO
        for row in InventoryMovement.objects.filter(
            movement_type=InventoryMovement.MovementType.INCOMING,
            movement_date__gte=window_start,
            movement_date__lte=as_of_date,
        )
        .values("sku_id")
        .annotate(qty=Sum("quantity"))
    }

    ending_by_sku = {}
    product_by_sku = {}
    batch = MerchandisingSnapshotBatch.objects.filter(is_active=True).first()
    if batch:
        state = official_planning_state(batch, run_date=as_of_date)
        official_month = (
            date(state["year"], state["current_month_number"], 1) if state else None
        )
        if official_month and indicator_month >= official_month:
            sku_rows = list(
                MerchandisingMonthlySnapshot.objects.filter(batch=batch)
                .order_by()
                .values("sku_id", "sku__product_variant__product_id")
                .distinct()
            )
            sku_ids = [row["sku_id"] for row in sku_rows]
            product_by_sku.update({
                row["sku_id"]: row["sku__product_variant__product_id"] for row in sku_rows
            })
            official_values = official_current_month_values(batch, sku_ids, state)
            ending_by_sku = {
                sku_id: values["ending_qty"] for sku_id, values in official_values.items()
            }
            if indicator_month > official_month:
                approved_incoming = {
                    (row["sku_id"], row["month"]): row["qty"] or ZERO
                    for row in IncomingPlan.objects.filter(
                        sku_id__in=sku_ids,
                        month__gt=official_month,
                        month__lte=indicator_month,
                        approval_status=IncomingPlan.ApprovalStatus.APPROVED,
                    )
                    .values("sku_id", "month")
                    .annotate(qty=Sum("final_approved_incoming"))
                }
                approved_sales = {
                    (row["sku_id"], row["month"]): row["qty"] or ZERO
                    for row in SalesProjection.objects.filter(
                        sku_id__in=sku_ids,
                        month__gt=official_month,
                        month__lte=indicator_month,
                        approval_status=SalesProjection.ApprovalStatus.APPROVED,
                    )
                    .values("sku_id", "month")
                    .annotate(qty=Sum("final_approved_qty"))
                }
                for sku_id in sku_ids:
                    ending = ending_by_sku.get(sku_id, ZERO)
                    month = next_calendar_month(official_month)
                    while month <= indicator_month:
                        ending += approved_incoming.get((sku_id, month), ZERO)
                        ending -= approved_sales.get((sku_id, month), ZERO)
                        month = next_calendar_month(month)
                    ending_by_sku[sku_id] = ending
        else:
            for row in MerchandisingMonthlySnapshot.objects.filter(
                batch=batch,
                month=indicator_month,
            ).values("sku_id", "sku__product_variant__product_id", "ending_qty"):
                ending_by_sku[row["sku_id"]] = row["ending_qty"] or ZERO
                product_by_sku[row["sku_id"]] = row["sku__product_variant__product_id"]

    active_sku_ids = {
        sku_id
        for sku_id in set(sales_by_sku) | set(inbound_by_sku) | set(ending_by_sku)
        if sales_by_sku.get(sku_id, ZERO) != ZERO
        or inbound_by_sku.get(sku_id, ZERO) != ZERO
        or ending_by_sku.get(sku_id, ZERO) != ZERO
    }

    # Sales/Inbound may exist for a SKU not present in the imported MD snapshot.
    missing_product_skus = active_sku_ids - set(product_by_sku)
    if missing_product_skus:
        from master_data.models import SKU

        product_by_sku.update(dict(
            SKU.objects.filter(id__in=missing_product_skus).values_list(
                "id", "product_variant__product_id"
            )
        ))

    active_product_ids = {
        product_by_sku[sku_id]
        for sku_id in active_sku_ids
        if sku_id in product_by_sku
    }
    return {
        "as_of_date": as_of_date,
        "window_start": window_start,
        "prior_month": indicator_month,
        "target_month": target_month,
        "sales_by_sku": sales_by_sku,
        "inbound_by_sku": inbound_by_sku,
        "ending_by_sku": ending_by_sku,
        "active_sku_ids": active_sku_ids,
        "active_product_ids": active_product_ids,
    }


def filter_products_by_planning_activity(queryset, mode, snapshot=None):
    snapshot = snapshot or planning_activity_snapshot()
    if mode == "ALL":
        return queryset
    if mode == "INACTIVE":
        return queryset.exclude(id__in=snapshot["active_product_ids"])
    return queryset.filter(id__in=snapshot["active_product_ids"])
