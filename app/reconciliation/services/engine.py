from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from audit.services import record_audit
from inventory.models import FIFOAllocation, InventoryException, InventoryMovement
from inventory.services.aging import po_aging_snapshot
from inventory.services.fifo import inventory_balance
from master_data.models import SKU
from purchasing.models import PurchaseOrder, PurchaseOrderLine
from sales.models import SalesOrderLine

from ..models import ReconciliationIssue, ReconciliationRun


def _issue(run, *, code, entity_type, entity_key, message, expected="", actual="", difference="", severity="CRITICAL"):
    return ReconciliationIssue.objects.create(
        run=run,
        severity=severity,
        code=code,
        entity_type=entity_type,
        entity_key=str(entity_key),
        expected_value=str(expected),
        actual_value=str(actual),
        difference=str(difference),
        message=message,
    )


@transaction.atomic
def run_reconciliation(actor, as_of_date=None):
    as_of_date = as_of_date or timezone.localdate()
    run = ReconciliationRun.objects.create(as_of_date=as_of_date, initiated_by=actor)
    checks = {
        "sales_movement": 0,
        "inbound_movement": 0,
        "qc_po_limit": 0,
        "inbound_qc_limit": 0,
        "fifo_allocation": 0,
        "inventory_fifo_balance": 0,
        "po_close": 0,
    }
    sales_lines = SalesOrderLine.objects.filter(is_counted=True).select_related("order", "sku")
    for line in sales_lines:
        if not (line.order.shipped_datetime or line.order.is_final):
            continue
        checks["sales_movement"] += 1
        movements = line.inventory_movements.filter(movement_type=InventoryMovement.MovementType.SALES_OUT)
        if movements.count() != 1:
            _issue(
                run,
                code="SALES_MOVEMENT_COUNT",
                entity_type="sales.salesorderline",
                entity_key=line.business_key,
                expected=1,
                actual=movements.count(),
                message="Setiap shipped/final sales line harus memiliki tepat satu Sales Out movement.",
            )
        elif movements.get().quantity != line.quantity:
            movement = movements.get()
            _issue(
                run,
                code="SALES_MOVEMENT_QTY",
                entity_type="sales.salesorderline",
                entity_key=line.business_key,
                expected=line.quantity,
                actual=movement.quantity,
                difference=Decimal(movement.quantity) - Decimal(line.quantity),
                message="Sales Out Qty berbeda dari canonical Sales Qty.",
            )
    for movement in InventoryMovement.objects.filter(movement_type=InventoryMovement.MovementType.INCOMING).select_related("inbound_receipt"):
        checks["inbound_movement"] += 1
        if not movement.inbound_receipt_id or movement.quantity != movement.inbound_receipt.received_qty:
            _issue(
                run,
                code="INBOUND_MOVEMENT_QTY",
                entity_type="inventory.inventorymovement",
                entity_key=movement.movement_key,
                expected=movement.inbound_receipt.received_qty if movement.inbound_receipt_id else "Inbound receipt",
                actual=movement.quantity,
                message="Incoming movement harus cocok tepat dengan Inbound Receipt.",
            )
    for line in PurchaseOrderLine.objects.select_related("po", "sku"):
        inspected = line.qc_inspections.aggregate(total=Sum("qty_inspected"))["total"] or Decimal("0")
        passed = line.qc_inspections.aggregate(total=Sum("qty_passed"))["total"] or Decimal("0")
        received = line.inbound_receipts.aggregate(total=Sum("received_qty"))["total"] or Decimal("0")
        checks["qc_po_limit"] += 1
        checks["inbound_qc_limit"] += 1
        if inspected > line.ordered_qty:
            _issue(run, code="OVER_QC", entity_type="purchasing.purchaseorderline", entity_key=f"{line.po}|{line.sku.sku}", expected=line.ordered_qty, actual=inspected, difference=inspected-line.ordered_qty, message="Cumulative inspected melebihi PO Qty.")
        if received > passed:
            _issue(run, code="OVER_INBOUND", entity_type="purchasing.purchaseorderline", entity_key=f"{line.po}|{line.sku.sku}", expected=passed, actual=received, difference=received-passed, message="Cumulative received melebihi Qty QC Passed.")
    for movement in InventoryMovement.objects.filter(direction=InventoryMovement.Direction.OUT):
        checks["fifo_allocation"] += 1
        allocated = movement.fifo_allocations.aggregate(total=Sum("allocated_qty"))["total"] or Decimal("0")
        short = movement.exceptions.filter(code=InventoryException.Code.FIFO_SHORT, status=InventoryException.Status.OPEN).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        if allocated + short != movement.quantity:
            _issue(run, code="FIFO_ALLOCATION_COVERAGE", entity_type="inventory.inventorymovement", entity_key=movement.movement_key, expected=movement.quantity, actual=allocated + short, difference=allocated + short - movement.quantity, message="FIFO allocation + declared short harus menutup seluruh outbound Qty.")
    for sku in SKU.objects.filter(inventory_movements__isnull=False).distinct():
        checks["inventory_fifo_balance"] += 1
        balance = inventory_balance(sku)
        fifo_qty = sku.fifo_layers.aggregate(total=Sum("remaining_qty"))["total"] or Decimal("0")
        open_short = sku.inventory_exceptions.filter(code=InventoryException.Code.FIFO_SHORT, status=InventoryException.Status.OPEN).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        if fifo_qty - open_short != balance:
            _issue(run, code="INVENTORY_FIFO_DIFFERENCE", entity_type="master_data.sku", entity_key=sku.sku, expected=balance, actual=fifo_qty - open_short, difference=(fifo_qty - open_short)-balance, message="Movement balance harus sama dengan FIFO remaining dikurangi open short.")
    for po in PurchaseOrder.objects.filter(status=PurchaseOrder.Status.RELEASED).prefetch_related("lines"):
        checks["po_close"] += 1
        snapshot = po_aging_snapshot(po, as_of_date)
        if po.close_date and snapshot["po_remaining_qty"] != 0:
            _issue(run, code="INVALID_PO_CLOSE", entity_type="purchasing.purchaseorder", entity_key=po.po_number, expected=0, actual=snapshot["po_remaining_qty"], message="PO Closed masih memiliki outstanding inbound atau remaining FIFO layer.")
    critical_count = run.issues.filter(severity=ReconciliationIssue.Severity.CRITICAL).count()
    totals = {
        "sales_lines": sales_lines.count(),
        "movements": InventoryMovement.objects.count(),
        "movement_qty_in": str(InventoryMovement.objects.filter(direction="IN").aggregate(total=Sum("quantity"))["total"] or 0),
        "movement_qty_out": str(InventoryMovement.objects.filter(direction="OUT").aggregate(total=Sum("quantity"))["total"] or 0),
        "fifo_remaining_qty": str(SKU.objects.aggregate(total=Sum("fifo_layers__remaining_qty"))["total"] or 0),
        "open_inventory_exceptions": InventoryException.objects.filter(status=InventoryException.Status.OPEN).count(),
        "critical_reconciliation_issues": critical_count,
    }
    run.status = ReconciliationRun.Status.PASSED if critical_count == 0 else ReconciliationRun.Status.FAILED
    run.finished_at = timezone.now()
    run.totals = totals
    run.check_summary = checks
    run.save(update_fields=["status", "finished_at", "totals", "check_summary"])
    record_audit(actor=actor, action="reconciliation_run_completed", entity_type="reconciliation.reconciliationrun", entity_id=run.id, after_values={"status": run.status, **totals})
    return run
