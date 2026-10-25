from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from audit.services import record_audit
from master_data.models import Supplier
from merchandising.models import IncomingPlan

from ..models import (
    PPICProductionMaterial,
    PPICProductionCost,
    PPICProductionPlan,
    PPICProductionPlanLine,
    PPICRequirement,
    PPICRequirementRevision,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderLine,
    PurchaseOrderNumberSequence,
)


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


def _plan_generation_errors(plan):
    errors = []
    if plan.production_method == PPICProductionPlan.ProductionMethod.FPP and not plan.supplier_id:
        errors.append("Vendor produksi wajib dipilih.")
    process_dates = {
        "control_material_purchase": (("Target Pembelian Material", plan.target_material_purchase_date),),
        "control_trial": (("Target Trial Production", plan.target_trial_date),),
        "control_cut": (
            ("Target Mulai Cut", plan.target_cut_start_date),
            ("Target Selesai Cut", plan.target_cut_end_date),
        ),
        "control_additional_process": (
            ("Target Mulai Additional Process", plan.target_additional_process_start_date),
            ("Target Selesai Additional Process", plan.target_additional_process_end_date),
        ),
        "control_make": (
            ("Target Mulai Make", plan.target_make_start_date),
            ("Target Selesai Make", plan.target_make_end_date),
        ),
        "control_trim": (
            ("Target Mulai Trim", plan.target_trim_start_date),
            ("Target Selesai Trim", plan.target_trim_end_date),
        ),
        "control_qc": (
            ("Target Mulai QC", plan.target_qc_start_date),
            ("Target Selesai QC", plan.target_qc_end_date),
        ),
        "control_inbound": (("Target Inbound", plan.target_inbound_date),),
    }
    controlled_processes = [name for name in process_dates if getattr(plan, name)]
    if not controlled_processes:
        errors.append("Pilih minimal satu proses yang akan dikontrol.")
    required_dates = [item for name in controlled_processes for item in process_dates[name]]
    if plan.production_method == PPICProductionPlan.ProductionMethod.CMT:
        if not plan.materials.exists():
            errors.append("CMT wajib memiliki minimal satu material.")
        if plan.materials.filter(supplier__isnull=True).exists():
            errors.append("Setiap material CMT wajib memiliki supplier.")
        if plan.process_costs.filter(cmt_unit_cost__gt=0, cmt_supplier__isnull=True).exists():
            errors.append("Setiap biaya CMT wajib memiliki vendor.")
        if plan.process_costs.filter(
            additional_process_unit_cost__gt=0,
            additional_process_supplier__isnull=True,
        ).exists():
            errors.append("Setiap biaya Additional Process wajib memiliki vendor.")
    elif any(line.unit_cogs <= 0 for line in plan.lines.all()):
        errors.append("COGS per quantity FPP wajib diisi pada setiap SKU.")
    errors.extend(f"{label} wajib diisi." for label, value in required_dates if not value)
    if not plan.lines.exists():
        errors.append("Production Plan belum memiliki product requirement.")
    if plan.planned_unit_cogs <= 0:
        errors.append("Planned COGS per unit wajib lebih besar dari nol.")
    return errors


def production_plan_po_groups(plan):
    """Return the exact child-PO split used by both preview and generation."""
    product_quantities = {}
    for line in plan.lines.select_related("requirement__sku__product_variant__product"):
        product = line.requirement.sku.product_variant.product
        product_quantities[product.id] = product_quantities.get(product.id, Decimal("0")) + line.quantity

    grouped = {}

    def add_item(order_type, supplier, product, description, quantity, unit, unit_price, required_arrival):
        key = (order_type, supplier.id if supplier else None)
        group = grouped.setdefault(
            key,
            {
                "order_type": order_type,
                "supplier": supplier,
                "required_arrival": required_arrival,
                "items": [],
            },
        )
        if order_type in {
            PurchaseOrder.OrderType.MAIN_MATERIAL,
            PurchaseOrder.OrderType.SUPPORTING_MATERIAL,
        }:
            material_key = (description.strip().casefold(), unit.strip().casefold(), unit_price)
            for item in group["items"]:
                item_key = (
                    item["description"].strip().casefold(),
                    item["unit"].strip().casefold(),
                    item["unit_price"],
                )
                if item_key == material_key:
                    item["quantity"] += quantity
                    if item["product"] != product:
                        item["product"] = None
                    return
        group["items"].append(
            {
                "product": product,
                "description": description,
                "quantity": quantity,
                "unit": unit,
                "unit_price": unit_price,
            }
        )

    for material in plan.materials.select_related("product", "supplier"):
        order_type = (
            PurchaseOrder.OrderType.MAIN_MATERIAL
            if material.material_type == PPICProductionMaterial.MaterialType.MAIN
            else PurchaseOrder.OrderType.SUPPORTING_MATERIAL
        )
        add_item(
            order_type,
            material.supplier,
            material.product,
            material.material_name,
            material.quantity,
            material.unit,
            material.unit_price,
            plan.target_material_purchase_date,
        )

    for cost in plan.process_costs.select_related("product", "cmt_supplier", "additional_process_supplier"):
        quantity = product_quantities.get(cost.product_id, Decimal("0"))
        if quantity > 0 and cost.cmt_unit_cost > 0:
            add_item(
                PurchaseOrder.OrderType.CMT,
                cost.cmt_supplier,
                cost.product,
                f"CMT · {cost.product.name}",
                quantity,
                "pcs",
                cost.cmt_unit_cost,
                plan.target_inbound_date,
            )
        if quantity > 0 and cost.additional_process_unit_cost > 0:
            process_name = cost.additional_process_name or plan.additional_process_name or "Additional Process"
            add_item(
                PurchaseOrder.OrderType.ADDITIONAL_PROCESS,
                cost.additional_process_supplier,
                cost.product,
                f"{process_name} · {cost.product.name}",
                quantity,
                "pcs",
                cost.additional_process_unit_cost,
                plan.target_additional_process_end_date,
            )

    type_order = {
        PurchaseOrder.OrderType.MAIN_MATERIAL: 0,
        PurchaseOrder.OrderType.CMT: 1,
        PurchaseOrder.OrderType.ADDITIONAL_PROCESS: 2,
        PurchaseOrder.OrderType.SUPPORTING_MATERIAL: 3,
    }
    groups = sorted(
        grouped.values(),
        key=lambda row: (type_order[row["order_type"]], row["supplier"].name if row["supplier"] else ""),
    )
    if len(groups) > 26:
        raise ValidationError("Satu PPO maksimal memiliki 26 PO.")
    for index, group in enumerate(groups):
        group["child_code"] = chr(65 + index)
        group["order_type_label"] = PurchaseOrder.OrderType(group["order_type"]).label
        group["total_cost"] = sum(
            (item["quantity"] * item["unit_price"] for item in group["items"]),
            Decimal("0"),
        )
    return groups


@transaction.atomic
def save_ppic_production_plan(
    *,
    actor,
    requirements,
    quantities,
    values,
    materials,
    process_costs=None,
    unit_cogs=None,
    plan=None,
):
    unit_cogs = unit_cogs or {}
    process_costs = process_costs or []
    requirements = list(
        PPICRequirement.objects.select_for_update()
        .select_related("sku__product_variant__product")
        .filter(pk__in=[row.pk for row in requirements])
    )
    if not requirements:
        raise ValidationError("Pilih minimal satu requirement untuk Production Plan.")
    need_months = {row.need_month for row in requirements}
    if len(need_months) != 1:
        raise ValidationError("Satu Production Plan hanya boleh memakai satu Need Month.")
    if plan is not None:
        plan = PPICProductionPlan.objects.select_for_update().get(pk=plan.pk)
        if plan.status != PPICProductionPlan.Status.DRAFT:
            raise ValidationError("Production Plan yang sudah menghasilkan PO tidak dapat diedit.")
    else:
        plan = PPICProductionPlan(need_month=next(iter(need_months)), created_by=actor)

    for name, value in values.items():
        setattr(plan, name, value)
    control_fields = {
        "control_material_purchase",
        "control_trial",
        "control_cut",
        "control_additional_process",
        "control_make",
        "control_trim",
        "control_qc",
        "control_inbound",
    }
    if not control_fields.intersection(values):
        plan.control_material_purchase = plan.production_method == PPICProductionPlan.ProductionMethod.CMT
        plan.control_trial = False
        plan.control_cut = plan.production_method == PPICProductionPlan.ProductionMethod.CMT
        plan.control_additional_process = False
        plan.control_make = plan.control_trim = plan.control_qc = plan.control_inbound = True
    plan.full_clean()
    plan.save()

    for requirement in requirements:
        raw_qty = quantities.get(str(requirement.id), quantities.get(requirement.id))
        try:
            quantity = Decimal(raw_qty)
        except Exception as exc:
            raise ValidationError(f"Qty {requirement.sku.sku} wajib berupa angka.") from exc
        other_planned = (
            PPICProductionPlanLine.objects.filter(
                requirement=requirement,
                plan__purchase_order__isnull=True,
            )
            .exclude(plan=plan)
            .aggregate(total=Sum("quantity"))["total"]
            or Decimal("0")
        )
        available = Decimal(requirement.remaining_qty) - other_planned
        if quantity <= 0 or quantity != quantity.to_integral_value() or quantity > available:
            raise ValidationError(
                f"Production Plan Qty {requirement.sku.sku} tidak valid atau melebihi sisa {available:.0f}."
            )

    plan.lines.all().delete()
    for requirement in requirements:
        raw_cogs = str(unit_cogs.get(str(requirement.id), unit_cogs.get(requirement.id, 0))).replace(",", "")
        try:
            line_cogs = Decimal(raw_cogs or 0)
        except Exception as exc:
            raise ValidationError(f"COGS {requirement.sku.sku} wajib berupa angka.") from exc
        if plan.production_method == PPICProductionPlan.ProductionMethod.FPP and (
            line_cogs < 0 or line_cogs != line_cogs.to_integral_value()
        ):
            raise ValidationError(f"COGS {requirement.sku.sku} wajib berupa Rupiah bulat dan tidak boleh negatif.")
        line = PPICProductionPlanLine(
            plan=plan,
            requirement=requirement,
            quantity=Decimal(quantities.get(str(requirement.id), quantities.get(requirement.id))),
            unit_cogs=line_cogs if plan.production_method == PPICProductionPlan.ProductionMethod.FPP else 0,
        )
        line.full_clean()
        line.save()

    plan.materials.all().delete()
    plan.process_costs.all().delete()
    if plan.production_method == PPICProductionPlan.ProductionMethod.CMT:
        for material in materials:
            row = PPICProductionMaterial(plan=plan, **material)
            row.full_clean()
            row.save()
        for process_cost in process_costs:
            if not plan.control_additional_process:
                process_cost = {
                    **process_cost,
                    "additional_process_name": "",
                    "additional_process_supplier": None,
                    "additional_process_unit_cost": Decimal("0"),
                }
            row = PPICProductionCost(plan=plan, **process_cost)
            row.full_clean()
            row.save()
        lines = list(plan.lines.select_related("requirement__sku__product_variant"))
        product_quantities = {}
        for line in lines:
            product_id = line.requirement.sku.product_variant.product_id
            product_quantities[product_id] = product_quantities.get(product_id, Decimal("0")) + line.quantity
        material_costs = {}
        for material in plan.materials.all():
            material_costs[material.product_id] = material_costs.get(material.product_id, Decimal("0")) + material.total_cost
        process_unit_costs = {
            row.product_id: row.cmt_unit_cost + row.additional_process_unit_cost
            for row in plan.process_costs.all()
        }
        for line in lines:
            product_id = line.requirement.sku.product_variant.product_id
            line.unit_cogs = (
                process_unit_costs.get(product_id, Decimal("0"))
                + material_costs.get(product_id, Decimal("0")) / product_quantities[product_id]
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        PPICProductionPlanLine.objects.bulk_update(lines, ["unit_cogs"])
    record_audit(
        actor=actor,
        action="ppic_production_plan_saved",
        entity_type="purchasing.ppicproductionplan",
        entity_id=plan.id,
        after_values={
            "production_method": plan.production_method,
            "requirement_count": plan.lines.count(),
            "total_qty": str(plan.total_qty),
            "planned_unit_cogs": str(plan.planned_unit_cogs),
        },
    )
    return plan


@transaction.atomic
def generate_po_from_production_plan(plan_id, actor):
    plan = (
        PPICProductionPlan.objects.select_for_update()
        .select_related("supplier")
        .prefetch_related("lines__requirement__sku", "materials", "process_costs")
        .get(pk=plan_id)
    )
    if plan.status != PPICProductionPlan.Status.DRAFT or plan.purchase_order_id:
        raise ValidationError("Production Plan ini sudah menghasilkan PO.")
    errors = _plan_generation_errors(plan)
    if errors:
        raise ValidationError(errors)
    quantities = {line.requirement_id: line.quantity for line in plan.lines.all()}
    cogs = {line.requirement_id: line.unit_cogs for line in plan.lines.all()}
    if plan.production_method == PPICProductionPlan.ProductionMethod.FPP:
        po = create_draft_po(
            supplier=plan.supplier,
            need_month=plan.need_month,
            required_arrival=plan.target_inbound_date,
            actor=actor,
            requirement_quantities=quantities,
            requirement_cogs=cogs,
            notes=f"Production Plan {plan.id} · {plan.get_production_method_display()}",
        )
        po.parent_plan = plan
        po.order_type = PurchaseOrder.OrderType.FPP
        po.full_clean()
        po.save(update_fields=["parent_plan", "order_type"])
    else:
        groups = production_plan_po_groups(plan)
        if not groups:
            raise ValidationError("CMT belum menghasilkan kelompok PO.")
        if any(group["supplier"] is None for group in groups):
            raise ValidationError("Setiap PO CMT wajib memiliki vendor.")
        anchor_group = next(
            (group for group in groups if group["order_type"] == PurchaseOrder.OrderType.CMT),
            groups[0],
        )
        po = None
        for group in groups:
            if group is anchor_group:
                child = create_draft_po(
                    supplier=group["supplier"],
                    need_month=plan.need_month,
                    required_arrival=group["required_arrival"],
                    actor=actor,
                    requirement_quantities=quantities,
                    requirement_cogs=cogs,
                    notes=f"PPO Production Plan {plan.id} · {group['order_type_label']}",
                )
                po = child
            else:
                child = PurchaseOrder(
                    supplier=group["supplier"],
                    need_month=plan.need_month,
                    required_arrival=group["required_arrival"],
                    created_by=actor,
                    notes=f"PPO Production Plan {plan.id} · {group['order_type_label']}",
                )
            child.parent_plan = plan
            child.child_code = group["child_code"]
            child.order_type = group["order_type"]
            child.full_clean()
            child.save()
            PurchaseOrderItem.objects.bulk_create(
                [PurchaseOrderItem(po=child, **item) for item in group["items"]]
            )
    plan.purchase_order = po
    plan.status = PPICProductionPlan.Status.GENERATED
    plan.full_clean()
    plan.save(update_fields=["purchase_order", "status", "updated_at"])
    record_audit(
        actor=actor,
        action="ppic_production_plan_po_generated",
        entity_type="purchasing.ppicproductionplan",
        entity_id=plan.id,
        after_values={
            "purchase_order_id": str(po.id),
            "child_purchase_order_ids": [str(child.id) for child in plan.child_purchase_orders.all()],
            "production_method": plan.production_method,
            "planned_unit_cogs": str(plan.planned_unit_cogs),
        },
    )
    return po


@transaction.atomic
def release_po(po_id, actor):
    po = (
        PurchaseOrder.objects.select_for_update()
        .select_related("parent_plan")
        .prefetch_related("lines__sku", "lines__requirement")
        .get(pk=po_id)
    )
    if po.status != PurchaseOrder.Status.DRAFT:
        raise ValidationError("Hanya PO Draft yang boleh dirilis.")

    anchor = po.parent_plan.purchase_order if po.parent_plan_id else po
    lines = list(anchor.lines.select_related("sku", "requirement").all())
    if not lines:
        raise ValidationError("PO kontrol produksi tidak memiliki line SKU.")
    for line in lines:
        if line.requirement_id:
            allocated = line.requirement.po_lines.exclude(po=anchor).exclude(po__status=PurchaseOrder.Status.CANCELLED).aggregate(
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
    annual_last_po_sequence = PurchaseOrder.objects.filter(
        issue_month__year=issue_month.year,
        sequence__isnull=False,
    ).aggregate(last=Max("sequence"))["last"] or 0
    annual_last_ppo_sequence = PPICProductionPlan.objects.filter(
        ppo_issue_month__year=issue_month.year,
        ppo_sequence__isnull=False,
    ).aggregate(last=Max("ppo_sequence"))["last"] or 0
    sequence_row.last_sequence = max(
        sequence_row.last_sequence,
        annual_last_po_sequence,
        annual_last_ppo_sequence,
    ) + 1
    sequence_row.save(update_fields=["last_sequence", "updated_at"])

    if po.parent_plan_id:
        plan = PPICProductionPlan.objects.select_for_update().get(pk=po.parent_plan_id)
        children = list(
            PurchaseOrder.objects.select_for_update()
            .filter(parent_plan=plan)
            .order_by("child_code")
        )
        if not children or any(child.status != PurchaseOrder.Status.DRAFT for child in children):
            raise ValidationError("Seluruh PO dalam PPO harus masih Draft sebelum dirilis bersama.")
        plan.ppo_sequence = sequence_row.last_sequence
        plan.ppo_issue_month = issue_month
        plan.ppo_number = f"PPO-VOB-{issue_month:%m/%y}-{plan.ppo_sequence:03d}"
        plan.save(update_fields=["ppo_sequence", "ppo_issue_month", "ppo_number", "updated_at"])
        for child in children:
            child.sequence = plan.ppo_sequence
            child.issue_month = issue_month
            child.po_number = f"PO-VOB-{issue_month:%m/%y}-{plan.ppo_sequence:03d}{child.child_code}"
            child.status = PurchaseOrder.Status.RELEASED
            child.released_by = actor
            child.released_at = released_at
            child.full_clean()
            child.save()
        po = next(child for child in children if child.id == po.id)
        anchor = next(child for child in children if child.id == plan.purchase_order_id)
    else:
        po.sequence = sequence_row.last_sequence
        po.issue_month = issue_month
        po.po_number = f"PO-VOB-{issue_month:%m/%y}-{po.sequence:03d}"
        po.status = PurchaseOrder.Status.RELEASED
        po.released_by = actor
        po.released_at = released_at
        po.full_clean()
        po.save()
    from production.services import ensure_production_order

    ensure_production_order(anchor, actor=actor)
    record_audit(
        actor=actor,
        action="purchase_order_released",
        entity_type="purchasing.purchaseorder",
        entity_id=po.id,
        after_values={
            "po_number": po.po_number,
            "ppo_number": po.ppo_number,
            "issue_month": issue_month.isoformat(),
            "need_month": po.need_month.isoformat(),
            "cogs_snapshot_preserved_from_draft": True,
        },
    )
    return po


@transaction.atomic
def cancel_po(po_id, actor, reason):
    po = PurchaseOrder.objects.select_for_update().select_related("parent_plan").get(pk=po_id)
    if po.status != PurchaseOrder.Status.RELEASED:
        raise ValidationError("Hanya PO Released yang boleh dibatalkan.")
    if not reason.strip():
        raise ValidationError("Alasan pembatalan wajib diisi.")
    group = list(
        PurchaseOrder.objects.select_for_update().filter(parent_plan=po.parent_plan).order_by("child_code")
        if po.parent_plan_id else [po]
    )
    if any(child.status != PurchaseOrder.Status.RELEASED for child in group):
        raise ValidationError("Seluruh PO dalam PPO harus berstatus Released sebelum dibatalkan bersama.")
    if any(
        child.lines.filter(qc_inspections__isnull=False).exists()
        or child.lines.filter(inbound_receipts__isnull=False).exists()
        for child in group
    ):
        raise ValidationError("PO yang sudah memiliki QC/Inbound tidak dapat dibatalkan.")
    cancelled_at = timezone.now()
    for child in group:
        child.status = PurchaseOrder.Status.CANCELLED
        child.cancellation_reason = reason
        child.cancelled_by = actor
        child.cancelled_at = cancelled_at
        child.full_clean()
        child.save()
    record_audit(
        actor=actor,
        action="purchase_order_cancelled",
        entity_type="purchasing.purchaseorder",
        entity_id=po.id,
        reason=reason,
        metadata={
            "ppo_number": po.ppo_number,
            "cancelled_child_po_ids": [str(child.id) for child in group],
        },
    )
    return next(child for child in group if child.id == po.id)


@transaction.atomic
def revise_legacy_wip_supplier(po_id, supplier, actor, reason):
    po = PurchaseOrder.objects.select_for_update().select_related("supplier").get(pk=po_id)
    if po.source not in {
        PurchaseOrder.Source.LEGACY_WIP,
        PurchaseOrder.Source.SUPPLEMENTAL_WIP,
    }:
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
    po = (
        PurchaseOrder.objects.select_for_update()
        .select_related("ppic_production_plan", "parent_plan")
        .prefetch_related("lines__requirement", "lines__sku", "items")
        .get(pk=po_id)
    )
    if po.status != PurchaseOrder.Status.DRAFT:
        raise ValidationError("Hanya PO Draft yang boleh dihapus.")
    production_plan = po.parent_plan or getattr(po, "ppic_production_plan", None)
    child_pos = list(
        PurchaseOrder.objects.select_for_update().filter(parent_plan=production_plan)
        if po.parent_plan_id else [po]
    )
    if any(child.status != PurchaseOrder.Status.DRAFT for child in child_pos):
        raise ValidationError("PPO hanya dapat dihapus selama seluruh PO masih Draft.")
    lines = [line for child in child_pos for line in child.lines.all()]
    before_values = {
        "supplier": str(po.supplier_id),
        "need_month": po.need_month.isoformat(),
        "line_count": len(lines),
        "child_po_count": len(child_pos),
        "need_keys": [
            f"{line.requirement.need_month.isoformat()}|{line.sku.sku}"
            for line in lines
            if line.requirement_id
        ],
    }
    if production_plan is not None:
        production_plan.purchase_order = None
        production_plan.status = PPICProductionPlan.Status.DRAFT
        production_plan.ppo_number = None
        production_plan.ppo_sequence = None
        production_plan.ppo_issue_month = None
        production_plan.save(update_fields=[
            "purchase_order",
            "status",
            "ppo_number",
            "ppo_sequence",
            "ppo_issue_month",
            "updated_at",
        ])
    po_id_value = po.id
    for child in child_pos:
        child.lines.all().delete()
        child.items.all().delete()
        child.delete()
    record_audit(
        actor=actor,
        action="purchase_order_draft_deleted",
        entity_type="purchasing.purchaseorder",
        entity_id=po_id_value,
        before_values=before_values,
        reason="Draft deleted before release; requirement eligibility restored",
    )
