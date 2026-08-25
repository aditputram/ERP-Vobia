from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from audit.services import record_audit
from master_data.models import Supplier
from merchandising.models import IncomingPlan

from ..models import PPICRequirement, PPICRequirementRevision, PurchaseOrder, PurchaseOrderLine, PurchaseOrderNumberSequence


@transaction.atomic
def sync_ppic_requirement(incoming_plan_id, actor, reason="Approved Incoming synced to PPIC"):
    plan = IncomingPlan.objects.select_for_update().select_related("sku").get(pk=incoming_plan_id)
    if plan.approval_status != IncomingPlan.ApprovalStatus.APPROVED:
        raise ValidationError("Hanya Final Approved Incoming yang boleh masuk PPIC.")
    qty = Decimal(plan.final_approved_incoming)
    if qty < 0:
        raise ValidationError("Final Approved Incoming tidak boleh negatif.")
    requirement = PPICRequirement.objects.select_for_update().filter(
        need_month=plan.month,
        sku=plan.sku,
    ).first()
    if qty == 0:
        if requirement is None:
            return None
        if requirement.po_lines.exists():
            raise ValidationError(
                "PPIC Requirement qty 0 tidak dapat dikeluarkan karena sudah memiliki histori PO line."
            )
        requirement_id = requirement.id
        previous_qty = requirement.approved_qty
        requirement.revisions.all().delete()
        requirement.delete()
        record_audit(
            actor=actor,
            action="ppic_requirement_removed_zero_incoming",
            entity_type="purchasing.ppicrequirement",
            entity_id=requirement_id,
            reason=reason,
            before_values={"approved_qty": str(previous_qty)},
            after_values={"approved_qty": "0", "removed_from_ppic": True},
        )
        return None
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


def _validated_cogs(raw_cogs, sku):
    if raw_cogs in (None, ""):
        raise ValidationError(f"PPIC COGS {sku.sku} wajib diisi saat Review PO.")
    try:
        cogs = Decimal(raw_cogs)
    except Exception as exc:
        raise ValidationError(f"PPIC COGS {sku.sku} harus berupa angka.") from exc
    if cogs <= 0 or cogs != cogs.to_integral_value():
        raise ValidationError(f"PPIC COGS {sku.sku} harus berupa Rupiah bulat positif.")
    return cogs


def review_po(
    *,
    supplier,
    need_month,
    required_arrival=None,
    requirement_quantities=None,
    requirement_cogs=None,
    manual_lines=None,
):
    requirement_quantities = requirement_quantities or {}
    requirement_cogs = requirement_cogs or {}
    manual_lines = manual_lines or []
    preview = []
    for requirement_id, raw_qty in requirement_quantities.items():
        requirement = PPICRequirement.objects.select_related("sku").get(pk=requirement_id)
        qty = Decimal(raw_qty)
        if requirement.need_month != need_month:
            raise ValidationError("Semua requirement dalam satu PO harus memiliki Need Month yang sama.")
        if qty <= 0 or qty != qty.to_integral_value() or qty > requirement.remaining_qty:
            raise ValidationError(f"PO Qty {requirement.sku.sku} tidak valid atau melebihi remaining requirement.")
        proposed_cogs = requirement_cogs.get(
            requirement.id,
            requirement_cogs.get(str(requirement.id), requirement.sku.current_master_cogs),
        )
        cogs_snapshot = _validated_cogs(proposed_cogs, requirement.sku)
        preview.append(
            {
                "requirement": requirement,
                "sku": requirement.sku,
                "qty": qty,
                "cogs_snapshot": cogs_snapshot,
                "line_value": qty * cogs_snapshot,
            }
        )
    for sku, raw_qty in manual_lines:
        qty = Decimal(raw_qty)
        if qty <= 0 or qty != qty.to_integral_value():
            raise ValidationError(f"Manual PO Qty {sku.sku} harus bilangan bulat positif.")
        cogs_snapshot = _validated_cogs(sku.current_master_cogs, sku)
        preview.append(
            {
                "requirement": None,
                "sku": sku,
                "qty": qty,
                "cogs_snapshot": cogs_snapshot,
                "line_value": qty * cogs_snapshot,
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
def create_draft_po(
    *,
    supplier,
    need_month,
    actor,
    required_arrival=None,
    requirement_quantities=None,
    requirement_cogs=None,
    manual_lines=None,
    notes="",
):
    preview = review_po(
        supplier=supplier,
        need_month=need_month,
        required_arrival=required_arrival,
        requirement_quantities=requirement_quantities,
        requirement_cogs=requirement_cogs,
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
            cogs_snapshot=item["cogs_snapshot"],
        )
        line.full_clean()
        line.save()
    record_audit(
        actor=actor,
        action="purchase_order_draft_created",
        entity_type="purchasing.purchaseorder",
        entity_id=po.id,
        after_values={
            "line_count": len(preview["lines"]),
            "total_qty": str(preview["total_qty"]),
            "total_cogs": str(preview["total_cogs"]),
            "cogs_source": "PPIC confirmed at Review PO",
        },
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
        if line.cogs_snapshot is None or line.cogs_snapshot <= 0:
            raise ValidationError(f"PPIC COGS snapshot {line.sku.sku} belum valid.")
        line.full_clean()
    released_at = timezone.now()
    issue_month = timezone.localdate(released_at).replace(day=1)
    sequence_row, _ = PurchaseOrderNumberSequence.objects.get_or_create(issue_month=issue_month)
    sequence_row = PurchaseOrderNumberSequence.objects.select_for_update().get(pk=sequence_row.pk)
    sequence_row.last_sequence += 1
    sequence_row.save(update_fields=["last_sequence", "updated_at"])
    po.sequence = sequence_row.last_sequence
    po.issue_month = issue_month
    po.po_number = f"PO-VOB-{issue_month:%m/%y}-{po.sequence:03d}"
    po.status = PurchaseOrder.Status.RELEASED
    po.released_by = actor
    po.released_at = released_at
    po.full_clean()
    po.save()
    from production.services import ensure_production_order

    ensure_production_order(po, actor=actor)
    record_audit(
        actor=actor,
        action="purchase_order_released",
        entity_type="purchasing.purchaseorder",
        entity_id=po.id,
        after_values={
            "po_number": po.po_number,
            "issue_month": issue_month.isoformat(),
            "need_month": po.need_month.isoformat(),
            "cogs_snapshot_preserved_from_draft": True,
        },
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


@transaction.atomic
def revise_legacy_wip_supplier(po_id, supplier, actor, reason):
    po = PurchaseOrder.objects.select_for_update().select_related("supplier").get(pk=po_id)
    if po.source != PurchaseOrder.Source.LEGACY_WIP:
        raise ValidationError("Revisi vendor ini hanya berlaku untuk PO WIP hasil migrasi.")
    if po.status != PurchaseOrder.Status.RELEASED:
        raise ValidationError("Vendor hanya dapat direvisi pada PO WIP berstatus Released.")
    if not supplier.is_active:
        raise ValidationError("Vendor tujuan harus berstatus aktif.")
    if po.supplier_id == supplier.id:
        raise ValidationError("Vendor tujuan sama dengan vendor PO saat ini.")
    reason = reason.strip()
    if len(reason) < 10:
        raise ValidationError("Alasan revisi vendor wajib diisi minimal 10 karakter.")

    previous_supplier = po.supplier
    po.supplier = supplier
    po.full_clean()
    po.save(update_fields=["supplier"])
    record_audit(
        actor=actor,
        action="legacy_wip_supplier_revised",
        entity_type="purchasing.purchaseorder",
        entity_id=po.id,
        reason=reason,
        before_values={
            "supplier_id": str(previous_supplier.id),
            "supplier_code": previous_supplier.code,
            "supplier_name": previous_supplier.name,
        },
        after_values={
            "supplier_id": str(supplier.id),
            "supplier_code": supplier.code,
            "supplier_name": supplier.name,
        },
        metadata={
            "po_number": po.po_number,
            "source": po.source,
            "migration_cutoff_date": str(po.migration_cutoff_date),
            "quantities_and_cost_snapshots_changed": False,
        },
    )
    return po


@transaction.atomic
def delete_unused_supplier(supplier_id, actor, reason):
    supplier = Supplier.objects.select_for_update().get(pk=supplier_id)
    if supplier.purchase_orders.exists():
        raise ValidationError("Vendor yang masih dipakai Purchase Order tidak boleh dihapus.")
    reason = reason.strip()
    if len(reason) < 10:
        raise ValidationError("Alasan penghapusan vendor wajib diisi minimal 10 karakter.")
    supplier_id_value = supplier.id
    before_values = {
        "code": supplier.code,
        "name": supplier.name,
        "contact_name": supplier.contact_name,
        "phone": supplier.phone,
        "is_active": supplier.is_active,
        "purchase_order_count": 0,
    }
    supplier.delete()
    record_audit(
        actor=actor,
        action="unused_supplier_deleted",
        entity_type="master_data.supplier",
        entity_id=supplier_id_value,
        reason=reason,
        before_values=before_values,
        after_values={"deleted": True},
        metadata={"protected_by_zero_purchase_order_gate": True},
    )
    return before_values


@transaction.atomic
def delete_draft_po(po_id, actor):
    po = PurchaseOrder.objects.select_for_update().prefetch_related("lines__requirement", "lines__sku").get(pk=po_id)
    if po.status != PurchaseOrder.Status.DRAFT:
        raise ValidationError("Hanya PO Draft yang boleh dihapus.")
    lines = list(po.lines.all())
    before_values = {
        "supplier": str(po.supplier_id),
        "need_month": po.need_month.isoformat(),
        "line_count": len(lines),
        "need_keys": [
            f"{line.requirement.need_month.isoformat()}|{line.sku.sku}"
            for line in lines
            if line.requirement_id
        ],
    }
    po_id_value = po.id
    po.lines.all().delete()
    po.delete()
    record_audit(
        actor=actor,
        action="purchase_order_draft_deleted",
        entity_type="purchasing.purchaseorder",
        entity_id=po_id_value,
        before_values=before_values,
        reason="Draft deleted before release; requirement eligibility restored",
    )
