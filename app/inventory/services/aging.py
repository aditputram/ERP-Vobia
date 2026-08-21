from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from purchasing.models import PurchaseOrder


def po_aging_snapshot(po, as_of_date=None):
    as_of_date = as_of_date or date.today()
    total_po = po.lines.aggregate(total=Sum("ordered_qty"))["total"] or Decimal("0")
    total_received = sum(
        (
            line.received_before_cutover_qty
            + (line.inbound_receipts.aggregate(total=Sum("received_qty"))["total"] or Decimal("0"))
            for line in po.lines.all()
        ),
        Decimal("0"),
    )
    layer_remaining = po.lines.aggregate(total=Sum("fifo_layers__remaining_qty"))["total"] or Decimal("0")
    outstanding = max(total_po - total_received, Decimal("0"))
    remaining = outstanding + layer_remaining
    end_date = po.close_date or as_of_date
    age_days = max((end_date - po.created_at.date()).days, 0)
    has_exception = po.lines.filter(fifo_layers__allocations__outbound_movement__exceptions__status="OPEN").exists()
    if has_exception:
        status = "Exception"
    elif po.close_date:
        status = "Closed Late" if age_days > 90 else "Closed"
    elif age_days <= 60:
        status = "Open - On Target"
    elif age_days <= 90:
        status = "Open - Attention"
    else:
        status = "Open - Over Target"
    return {
        "total_po_qty": total_po,
        "received_qty": total_received,
        "outstanding_inbound": outstanding,
        "remaining_layer_qty": layer_remaining,
        "po_remaining_qty": remaining,
        "age_days": age_days,
        "status": status,
    }


@transaction.atomic
def refresh_po_close(po_id, as_of_date=None):
    po = PurchaseOrder.objects.select_for_update().prefetch_related("lines__inbound_receipts", "lines__fifo_layers").get(pk=po_id)
    snapshot = po_aging_snapshot(po, as_of_date)
    if snapshot["po_remaining_qty"] == 0 and snapshot["status"] != "Exception":
        if not po.close_date:
            po.close_date = as_of_date or date.today()
            po.save(update_fields=["close_date"])
    elif po.close_date:
        po.close_date = None
        po.save(update_fields=["close_date"])
    return po_aging_snapshot(po, as_of_date)
