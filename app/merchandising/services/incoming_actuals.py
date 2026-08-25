from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone

from audit.services import record_audit
from inventory.models import InboundReceipt
from inventory.services.reporting import inventory_summary_rows
from master_data.models import SKU
from merchandising.models import (
    IncomingCarryover,
    IncomingMonthClose,
    IncomingMonthlyActual,
    MerchandisingMonthlySnapshot,
    MerchandisingSnapshotBatch,
)
from purchasing.models import PurchaseOrder, PurchaseOrderLine


ZERO = Decimal("0")


def month_start(value):
    return date(value.year, value.month, 1)


def next_month(value):
    return date(value.year + (1 if value.month == 12 else 0), 1 if value.month == 12 else value.month + 1, 1)


def month_end(value):
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def live_incoming_actuals(month, sku_ids=None):
    month = month_start(month)
    gross_expression = ExpressionWrapper(
        F("received_qty") * F("retail_price_snapshot"),
        output_field=DecimalField(max_digits=24, decimal_places=4),
    )
    rows = InboundReceipt.objects.filter(inbound_date__year=month.year, inbound_date__month=month.month)
    if sku_ids is not None:
        rows = rows.filter(po_line__sku_id__in=list(sku_ids))
    return {
        row["po_line__sku_id"]: {
            "incoming_qty": row["actual_qty"] or ZERO,
            "incoming_cogs": row["actual_cogs"] or ZERO,
            "incoming_gross": row["actual_gross"] or ZERO,
        }
        for row in rows.values("po_line__sku_id").annotate(
            actual_qty=Sum("received_qty"),
            actual_cogs=Sum("movement__allocated_cost"),
            actual_gross=Sum(gross_expression),
        )
    }


def official_incoming_actuals(month, sku_ids=None):
    """Use frozen close rows after close; otherwise show live Warehouse receipts."""
    month = month_start(month)
    close = IncomingMonthClose.objects.filter(month=month).first()
    if not close:
        return live_incoming_actuals(month, sku_ids)
    rows = close.actual_rows.all()
    if sku_ids is not None:
        rows = rows.filter(sku_id__in=list(sku_ids))
    return {
        row.sku_id: {
            "incoming_qty": row.actual_qty,
            "incoming_cogs": row.actual_cogs,
            "incoming_gross": row.actual_gross,
        }
        for row in rows
    }


def projected_incoming(batch, month, sku_ids=None):
    rows = MerchandisingMonthlySnapshot.objects.filter(batch=batch, month=month_start(month))
    if sku_ids is not None:
        rows = rows.filter(sku_id__in=list(sku_ids))
    return {
        row["sku_id"]: {
            "incoming_qty": row["incoming_qty"] or ZERO,
            "incoming_cogs": row["incoming_cogs"] or ZERO,
            "incoming_gross": row["incoming_gross"] or ZERO,
        }
        for row in rows.values("sku_id", "incoming_qty", "incoming_cogs", "incoming_gross")
    }


def incoming_comparison(batch, month, sku_ids=None):
    projected = projected_incoming(batch, month, sku_ids)
    actual = official_incoming_actuals(month, sku_ids)
    ids = set(projected) | set(actual)
    return {
        sku_id: {
            "projection": projected.get(sku_id, {"incoming_qty": ZERO, "incoming_cogs": ZERO, "incoming_gross": ZERO}),
            "actual": actual.get(sku_id, {"incoming_qty": ZERO, "incoming_cogs": ZERO, "incoming_gross": ZERO}),
            "variance_qty": actual.get(sku_id, {}).get("incoming_qty", ZERO) - projected.get(sku_id, {}).get("incoming_qty", ZERO),
        }
        for sku_id in ids
    }


def closed_cost_actuals(year, sku_ids=None):
    """Frozen actual incoming and ending values used after a month is closed."""
    rows = IncomingMonthlyActual.objects.filter(month_close__month__year=year)
    if sku_ids is not None:
        rows = rows.filter(sku_id__in=list(sku_ids))
    return {
        (row.sku_id, row.month_close.month.month): {
            "incoming_qty": row.actual_qty,
            "incoming_cogs": row.actual_cogs,
            "incoming_gross": row.actual_gross,
            "ending_qty": row.actual_ending_qty,
            "ending_cogs": row.actual_ending_cogs,
        }
        for row in rows.select_related("month_close")
    }


@transaction.atomic
def close_incoming_month(*, month, actor, evidence_reference, notes="", today=None, allow_open_month=False):
    month = month_start(month)
    cutoff = month_end(month)
    today = today or timezone.localdate()
    if today <= cutoff and not allow_open_month:
        raise ValidationError("Bulan berjalan belum boleh ditutup. Month close tersedia mulai tanggal 1 bulan berikutnya.")
    if IncomingMonthClose.objects.filter(month=month).exists():
        raise ValidationError("Incoming month ini sudah ditutup dan snapshot-nya immutable.")
    if not evidence_reference.strip():
        raise ValidationError("Referensi bukti month close wajib diisi.")
    batch = MerchandisingSnapshotBatch.objects.filter(is_active=True).first()
    if not batch:
        raise ValidationError("Baseline Merchandising aktif belum tersedia.")

    close = IncomingMonthClose(
        month=month,
        cutoff_date=cutoff,
        closed_by=actor,
        evidence_reference=evidence_reference.strip(),
        notes=notes,
    )
    close.full_clean()
    close.save()
    projected_values = projected_incoming(batch, month)
    live_values = live_incoming_actuals(month)
    projected_month_rows = {
        row.sku_id: row
        for row in MerchandisingMonthlySnapshot.objects.filter(batch=batch, month=month)
    }
    comparison = {
        sku_id: {
            "projection": projected_values.get(sku_id, {"incoming_qty": ZERO, "incoming_cogs": ZERO, "incoming_gross": ZERO}),
            "actual": live_values.get(sku_id, {"incoming_qty": ZERO, "incoming_cogs": ZERO, "incoming_gross": ZERO}),
        }
        for sku_id in set(projected_values) | set(live_values)
    }
    sku_ids = set(comparison)
    ending_actuals = {
        row["sku"].id: row
        for row in inventory_summary_rows(
            SKU.objects.filter(id__in=sku_ids).select_related(
                "product_variant__product__status",
                "product_variant__product__category",
                "product_variant__product__subcategory",
            ),
            as_of_date=cutoff,
        )
    }
    actual_rows = []
    shortage_by_sku = {}
    for sku_id, values in comparison.items():
        projection = values["projection"]
        actual = values["actual"]
        projected_month = projected_month_rows.get(sku_id)
        ending_actual = ending_actuals.get(sku_id, {})
        shortage = max(projection["incoming_qty"] - actual["incoming_qty"], ZERO)
        shortage_by_sku[sku_id] = shortage
        actual_rows.append(
            IncomingMonthlyActual(
                month_close=close,
                sku_id=sku_id,
                projected_qty=projection["incoming_qty"],
                projected_cogs=projection["incoming_cogs"],
                projected_ending_qty=(projected_month.ending_qty if projected_month else ZERO),
                projected_ending_cogs=(projected_month.ending_cogs if projected_month else ZERO),
                actual_qty=actual["incoming_qty"],
                actual_cogs=actual["incoming_cogs"],
                actual_gross=actual["incoming_gross"],
                actual_ending_qty=ending_actual.get("balance", ZERO),
                actual_ending_cogs=ending_actual.get("fifo_value", ZERO),
                variance_qty=actual["incoming_qty"] - projection["incoming_qty"],
            )
        )
    IncomingMonthlyActual.objects.bulk_create(actual_rows)

    receipts_to_cutoff = {
        row["po_line_id"]: row["qty"] or ZERO
        for row in InboundReceipt.objects.filter(inbound_date__lte=cutoff)
        .values("po_line_id")
        .annotate(qty=Sum("received_qty"))
    }
    candidates = PurchaseOrderLine.objects.filter(
        po__status=PurchaseOrder.Status.RELEASED,
        po__need_month__lte=month,
    ).select_related("po", "sku").order_by("po__required_arrival", "po__created_at", "sku__sku")
    remaining_shortage = defaultdict(lambda: ZERO, shortage_by_sku)
    carryovers = []
    for line in candidates:
        if remaining_shortage[line.sku_id] <= 0:
            continue
        received = line.received_before_cutover_qty + receipts_to_cutoff.get(line.id, ZERO)
        outstanding = max(line.ordered_qty - received, ZERO)
        carry_qty = min(outstanding, remaining_shortage[line.sku_id])
        if carry_qty <= 0:
            continue
        carryovers.append(
            IncomingCarryover(
                source_close=close,
                target_month=next_month(month),
                po_line=line,
                sku=line.sku,
                carryover_qty=carry_qty,
            )
        )
        remaining_shortage[line.sku_id] -= carry_qty
    IncomingCarryover.objects.bulk_create(carryovers)
    record_audit(
        actor=actor,
        action="incoming_month_closed",
        entity_type="merchandising.incomingmonthclose",
        entity_id=close.id,
        reason=notes,
        after_values={
            "month": str(month),
            "actual_rows": len(actual_rows),
            "carryover_rows": len(carryovers),
            "actual_incoming_cogs": str(sum((row.actual_cogs for row in actual_rows), ZERO)),
            "actual_ending_cogs": str(sum((row.actual_ending_cogs for row in actual_rows), ZERO)),
            "evidence_reference": evidence_reference,
        },
    )
    return close


def carryover_totals(month, sku_ids=None):
    rows = IncomingCarryover.objects.filter(target_month=month_start(month))
    if sku_ids is not None:
        rows = rows.filter(sku_id__in=list(sku_ids))
    return {
        row["sku_id"]: row["qty"] or ZERO
        for row in rows.values("sku_id").annotate(qty=Sum("carryover_qty"))
    }
