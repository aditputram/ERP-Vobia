from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from audit.services import record_audit
from merchandising.models import IncomingPlan

from ..models import PPICRequirement, PPICRequirementRevision, PurchaseOrder, PurchaseOrderLine, PurchaseOrderNumberSequence


@transaction.atomic
def sync_ppic_requirement(incoming_plan_id, actor, reason="Approved Incoming synced to PPIC"):
    plan = IncomingPlan.objects.select_for_update().select_related("sku").get(pk=incoming_plan_id)
    if plan.approval_status != IncomingPlan.ApprovalStatus.APPROVED:
        raise ValidationError("Hanya Final Approved Incoming yang boleh masuk PPIC.")
    qty = Decimal(plan.final_approved_incoming)
    requirement = PPICRequirement.objects.select_for_update().filter(
        need_month=plan.month,
        sku=plan.sku,
    ).first()
    if requirement is None:
        requirement = PPICRequirement(
            need_month=plan.month,
            sku=plan.sku,
            incoming_plan=plan,
            approved_qty=qty,
            created_by=actor,
        )
        requirement.full_clean()
        requirement.save()
        previous_qty = Decimal("0")
    else:
        previous_qty = requirement.approved_qty
        ordered = Decimal(requirement.ordered_qty)
        if qty < ordered:
            raise ValidationError(
                f"Approved Incoming {qty} tidak boleh di bawah qty yang sudah masuk PO ({ordered})."
            )
        requirement.approved_qty = qty
        requirement.incoming_plan = plan
        requirement.revision += 1
        requirement.full_clean()
        requirement.save()
    PPICRequirementRevision.objects.create(
        requirement=requirement,
        revision=requirement.revision,
        previous_qty=previous_qty,
        approved_qty=qty,
        reason=reason,
        changed_by=actor,
    )
    record_audit(
        actor=actor,
        action="ppic_requirement_synced",
        entity_type="purchasing.ppicrequirement",
        entity_id=requirement.id,
        reason=reason,
        before_values={"approved_qty": str(previous_qty)},
        after_values={"approved_qty": str(qty), "revision": requirement.revision},
    )
    return requirement


def review_po(*, supplier, need_month, required_arrival=None, requirement_quantities=None, manual_lines=None):
    requirement_quantities = requirement_quantities or {}
    manual_lines = manual_lines or []
    preview = []
    for requirement_id, raw_qty in requirement_quantities.items():
        requirement = PPICRequirement.objects.select_related("sku").get(pk=requirement_id)
        qty = Decimal(raw_qty)
        if requirement.need_month != need_month:
            raise ValidationError("Semua requirement dalam satu PO harus memiliki Need Month yang sama.")
        if qty <= 0 or qty != qty.to_integral_value() or qty > requirement.remaining_qty:
            raise ValidationError(f"PO Qty {requirement.sku.sku} tidak valid atau melebihi remaining requirement.")
        if requirement.sku.current_master_cogs is None:
            raise ValidationError(f"COGS master {requirement.sku.sku} belum tersedia.")
        preview.append(
            {
                "requirement": requirement,
                "sku": requirement.sku,
                "qty": qty,
                "cogs_snapshot": requirement.sku.current_master_cogs,
                "line_value": qty * requirement.sku.current_master_cogs,
            }
        )
    for sku, raw_qty in manual_lines:
        qty = Decimal(raw_qty)
        if qty <= 0 or qty != qty.to_integral_value():
            raise ValidationError(f"Manual PO Qty {sku.sku} harus bilangan bulat positif.")
        if sku.current_master_cogs is None:
            raise ValidationError(f"COGS master {sku.sku} belum tersedia.")
        preview.append(
            {
                "requirement": None,
                "sku": sku,
                "qty": qty,
                "cogs_snapshot": sku.current_master_cogs,
                "line_value": qty * sku.current_master_cogs,
            }
        )
    if not preview:
        raise ValidationError("PO harus memiliki minimal satu line.")
    return {
        "supplier": supplier,
        "need_month": need_month,
        "required_arrival": required_arrival,
        "lines": preview,
        "total_qty": sum((item["qty"] for item in preview), Decimal("0")),
        "total_cogs": sum((item["qty"] * item["cogs_snapshot"] for item in preview), Decimal("0")),
    }


@transaction.atomic
def create_draft_po(*, supplier, need_month, actor, required_arrival=None, requirement_quantities=None, manual_lines=None, notes=""):
    preview = review_po(
        supplier=supplier,
        need_month=need_month,
        required_arrival=required_arrival,
        requirement_quantities=requirement_quantities,
        manual_lines=manual_lines,
    )
    source = PurchaseOrder.Source.MANUAL_NEW_PRODUCT if manual_lines else PurchaseOrder.Source.INCOMING_PLAN
    po = PurchaseOrder.objects.create(
        supplier=supplier,
        need_month=need_month,
        required_arrival=required_arrival,
        source=source,
        status=PurchaseOrder.Status.DRAFT,
        notes=notes,
        created_by=actor,
    )
    for item in preview["lines"]:
        line = PurchaseOrderLine(
            po=po,
            requirement=item["requirement"],
            sku=item["sku"],
            ordered_qty=item["qty"],
        )
        line.full_clean()
        line.save()
    record_audit(
        actor=actor,
        action="purchase_order_draft_created",
        entity_type="purchasing.purchaseorder",
        entity_id=po.id,
        after_values={"line_count": len(preview["lines"]), "total_qty": str(preview["total_qty"])},
    )
    return po


@transaction.atomic
def release_po(po_id, actor):
    po = PurchaseOrder.objects.select_for_update().prefetch_related("lines__sku", "lines__requirement").get(pk=po_id)
    if po.status != PurchaseOrder.Status.DRAFT:
        raise ValidationError("Hanya PO Draft yang boleh dirilis.")
    lines = list(po.lines.all())
    if not lines:
        raise ValidationError("PO tidak memiliki line.")
    for line in lines:
        if line.requirement_id:
            allocated = line.requirement.po_lines.exclude(po=po).exclude(po__status=PurchaseOrder.Status.CANCELLED).aggregate(
                total=Sum("ordered_qty")
            )["total"] or Decimal("0")
            if allocated + line.ordered_qty > line.requirement.approved_qty:
                raise ValidationError(f"Qty {line.sku.sku} melebihi remaining PPIC Requirement.")
        if line.sku.current_master_cogs is None:
            raise ValidationError(f"COGS master {line.sku.sku} belum tersedia.")
        line.cogs_snapshot = line.sku.current_master_cogs
        line.full_clean()
        line.save(update_fields=["cogs_snapshot"])
    sequence_row, _ = PurchaseOrderNumberSequence.objects.get_or_create(need_month=po.need_month)
    sequence_row = PurchaseOrderNumberSequence.objects.select_for_update().get(pk=sequence_row.pk)
    sequence_row.last_sequence += 1
    sequence_row.save(update_fields=["last_sequence", "updated_at"])
    po.sequence = sequence_row.last_sequence
    po.po_number = f"PO-VOB-{po.need_month:%m/%y}-{po.sequence:03d}"
    po.status = PurchaseOrder.Status.RELEASED
    po.released_by = actor
    po.released_at = timezone.now()
    po.full_clean()
    po.save()
    record_audit(
        actor=actor,
        action="purchase_order_released",
        entity_type="purchasing.purchaseorder",
        entity_id=po.id,
        after_values={"po_number": po.po_number, "cogs_snapshotted": True},
    )
    return po


@transaction.atomic
def cancel_po(po_id, actor, reason):
    po = PurchaseOrder.objects.select_for_update().get(pk=po_id)
    if po.status != PurchaseOrder.Status.RELEASED:
        raise ValidationError("Hanya PO Released yang boleh dibatalkan.")
    if not reason.strip():
        raise ValidationError("Alasan pembatalan wajib diisi.")
    if po.lines.filter(qc_inspections__isnull=False).exists() or po.lines.filter(inbound_receipts__isnull=False).exists():
        raise ValidationError("PO yang sudah memiliki QC/Inbound tidak dapat dibatalkan.")
    po.status = PurchaseOrder.Status.CANCELLED
    po.cancellation_reason = reason
    po.cancelled_by = actor
    po.cancelled_at = timezone.now()
    po.full_clean()
    po.save()
    record_audit(
        actor=actor,
        action="purchase_order_cancelled",
        entity_type="purchasing.purchaseorder",
        entity_id=po.id,
        reason=reason,
    )
    return po
