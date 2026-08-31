from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from audit.services import record_audit
from merchandising.models import IncomingPlan, ProjectionScenario, SalesProjection

from .calculations import (
    NO_INCOMING_CATEGORIES,
    NO_INCOMING_STATUSES,
    incoming_calculation,
    planning_buffer_incoming,
)


@transaction.atomic
def approve_sales_projection(projection_id, final_qty, actor, reason=""):
    projection = SalesProjection.objects.select_for_update().select_related("sku").get(
        pk=projection_id
    )
    if projection.approval_status == SalesProjection.ApprovalStatus.APPROVED:
        raise ValidationError("Projection sudah approved.")
    final_qty = Decimal(final_qty)
    projection.final_approved_qty = final_qty
    projection.adit_adjustment = final_qty - projection.system_recommendation
    projection.approval_status = SalesProjection.ApprovalStatus.APPROVED
    projection.approved_by = actor
    projection.approved_at = timezone.now()
    projection.explanation = reason
    projection.full_clean()
    projection.save()
    record_audit(
        actor=actor,
        action="sales_projection_approved",
        entity_type="merchandising.salesprojection",
        entity_id=projection.id,
        reason=reason,
        after_values={
            "system_recommendation": str(projection.system_recommendation),
            "adit_adjustment": str(projection.adit_adjustment),
            "final_approved_qty": str(projection.final_approved_qty),
        },
    )
    return projection


@transaction.atomic
def create_incoming_plan(projection_id, prior_ending_qty, target_stock_ratio=None):
    projection = SalesProjection.objects.select_for_update().select_related(
        "sku__product_variant__product__status",
        "sku__product_variant__product__category",
    ).get(pk=projection_id)
    if projection.approval_status != SalesProjection.ApprovalStatus.APPROVED:
        raise ValidationError("Hanya Final Approved Projection yang boleh masuk Incoming Plan.")
    values = incoming_calculation(
        projection.final_approved_qty,
        prior_ending_qty,
        target_stock_ratio,
    )
    product = projection.sku.product_variant.product
    no_incoming = (
        product.status.name in NO_INCOMING_STATUSES
        or product.category.name in NO_INCOMING_CATEGORIES
    )
    if no_incoming and values["minimum"] > 0:
        raise ValidationError(
            "Projection melampaui stock tersedia untuk produk yang tidak boleh memiliki incoming baru."
        )
    recommended = Decimal("0") if no_incoming else values["recommended"]
    plan, _ = IncomingPlan.objects.update_or_create(
        scenario=projection.scenario,
        month=projection.month,
        sku=projection.sku,
        defaults={
            "sales_projection": projection,
            "prior_ending_qty": prior_ending_qty,
            "minimum_incoming": values["minimum"],
            "target_stock_ratio": target_stock_ratio,
            "recommended_incoming": recommended,
            "approval_status": IncomingPlan.ApprovalStatus.DRAFT,
            "final_approved_incoming": None,
            "approved_by": None,
            "approved_at": None,
        },
    )
    return plan


@transaction.atomic
def approve_incoming_plan(plan_id, final_incoming, actor, reason=""):
    plan = IncomingPlan.objects.select_for_update().get(pk=plan_id)
    if plan.approval_status == IncomingPlan.ApprovalStatus.APPROVED:
        raise ValidationError("Incoming Plan sudah approved.")
    final_incoming = Decimal(final_incoming)
    plan.final_approved_incoming = final_incoming
    plan.adit_adjustment = final_incoming - plan.recommended_incoming
    plan.approval_status = IncomingPlan.ApprovalStatus.APPROVED
    plan.approved_by = actor
    plan.approved_at = timezone.now()
    plan.full_clean()
    plan.save()
    record_audit(
        actor=actor,
        action="incoming_plan_approved",
        entity_type="merchandising.incomingplan",
        entity_id=plan.id,
        reason=reason,
        after_values={
            "minimum_incoming": str(plan.minimum_incoming),
            "recommended_incoming": str(plan.recommended_incoming),
            "adit_adjustment": str(plan.adit_adjustment),
            "final_approved_incoming": str(plan.final_approved_incoming),
        },
    )
    from purchasing.services.workflows import sync_ppic_requirement

    sync_ppic_requirement(plan.id, actor, reason or "Final Approved Incoming synced automatically")
    return plan


def _scenario_months(scenario):
    months = []
    month = scenario.start_month
    while month <= scenario.end_month:
        months.append(month)
        month = month.replace(
            year=month.year + (1 if month.month == 12 else 0),
            month=1 if month.month == 12 else month.month + 1,
        )
    return months


def _whole_nonnegative(value, label):
    value = Decimal(value)
    if value < 0 or value != value.to_integral_value():
        raise ValidationError(f"{label} harus bilangan bulat dan tidak boleh negatif.")
    return value


@transaction.atomic
def save_scenario_draft(scenario_id, actor, sales_values=None, incoming_values=None, reason=""):
    """Save auditable SKU-level Sales and Incoming adjustments before scenario approval."""
    sales_values = sales_values or {}
    incoming_values = incoming_values or {}
    scenario = ProjectionScenario.objects.select_for_update().get(pk=scenario_id)
    if scenario.status != ProjectionScenario.Status.DRAFT:
        raise ValidationError("Hanya Scenario Draft yang dapat diedit.")
    projections = list(
        SalesProjection.objects.select_for_update().filter(scenario=scenario).select_related(
            "sku__product_variant__product__status",
            "sku__product_variant__product__category",
        )
    )
    if not projections:
        raise ValidationError("Scenario belum memiliki Draft Projection.")
    targeted_projection_ids = set(sales_values) | set(incoming_values)
    if targeted_projection_ids:
        projections = [
            projection
            for projection in projections
            if str(projection.id) in targeted_projection_ids
        ]
    plans_by_projection = {
        plan.sales_projection_id: plan
        for plan in IncomingPlan.objects.select_for_update().filter(scenario=scenario)
    }
    sales_total = Decimal("0")
    incoming_total = Decimal("0")
    for projection in projections:
        projection_key = str(projection.id)
        final_sales = _whole_nonnegative(
            sales_values.get(projection_key, projection.proposed_qty),
            f"{projection.sku.sku}: Sales Projection",
        )
        projection.adit_adjustment = final_sales - projection.system_recommendation
        projection.final_approved_qty = None
        projection.approval_status = SalesProjection.ApprovalStatus.DRAFT
        projection.approved_by = None
        projection.approved_at = None
        projection.full_clean()
        projection.save(update_fields=[
            "adit_adjustment", "final_approved_qty", "approval_status", "approved_by", "approved_at",
        ])
        sales_total += final_sales

        prior_ending = projection.beginning_qty or Decimal("0")
        existing_plan = plans_by_projection.get(projection.id)
        target_ratio = existing_plan.target_stock_ratio if existing_plan else None
        values = incoming_calculation(final_sales, prior_ending, target_ratio)
        product = projection.sku.product_variant.product
        no_incoming = (
            product.status.name in NO_INCOMING_STATUSES
            or product.category.name in NO_INCOMING_CATEGORIES
        )
        available_stock = max(prior_ending, Decimal("0"))
        if no_incoming and final_sales > available_stock:
            raise ValidationError(
                f"{projection.sku.sku}: Sales Projection melampaui stock untuk Product yang tidak boleh Incoming."
            )
        buffer_minimum = planning_buffer_incoming(
            final_sales,
            prior_ending,
            incoming_allowed=not no_incoming,
        )
        minimum_incoming = Decimal("0") if no_incoming else buffer_minimum
        recommended = (
            Decimal("0")
            if no_incoming
            else max(values["recommended"], buffer_minimum)
        )
        if no_incoming:
            chosen_incoming = Decimal("0")
        elif projection_key in incoming_values:
            chosen_incoming = _whole_nonnegative(
                incoming_values[projection_key],
                f"{projection.sku.sku}: Incoming Plan",
            )
        elif existing_plan:
            chosen_incoming = max(existing_plan.proposed_incoming, minimum_incoming)
        else:
            chosen_incoming = recommended
        if chosen_incoming < minimum_incoming:
            raise ValidationError(
                f"{projection.sku.sku}: Incoming Plan tidak boleh di bawah minimum {minimum_incoming:.0f}."
            )
        plan, _ = IncomingPlan.objects.update_or_create(
            scenario=scenario,
            month=projection.month,
            sku=projection.sku,
            defaults={
                "sales_projection": projection,
                "prior_ending_qty": prior_ending,
                "minimum_incoming": minimum_incoming,
                "target_stock_ratio": target_ratio,
                "recommended_incoming": recommended,
                "adit_adjustment": chosen_incoming - recommended or None,
                "final_approved_incoming": None,
                "approval_status": IncomingPlan.ApprovalStatus.DRAFT,
                "approved_by": None,
                "approved_at": None,
            },
        )
        plan.full_clean()
        incoming_total += chosen_incoming
    record_audit(
        actor=actor,
        action="projection_scenario_draft_updated",
        entity_type="merchandising.projectionscenario",
        entity_id=scenario.id,
        reason=reason,
        after_values={
            "projection_count": len(projections),
            "sales_projection_total": str(sales_total),
            "incoming_plan_total": str(incoming_total),
        },
    )
    return scenario


@transaction.atomic
def approve_scenario(scenario_id, actor, sales_values=None, incoming_values=None, reason=""):
    """Atomically approve every Sales and Incoming row in a complete scenario."""
    scenario = ProjectionScenario.objects.select_for_update().get(pk=scenario_id)
    if scenario.status != ProjectionScenario.Status.DRAFT:
        raise ValidationError("Scenario sudah approved atau tidak lagi aktif.")
    projected_months = set(scenario.projections.values_list("month", flat=True))
    missing_months = [month for month in _scenario_months(scenario) if month not in projected_months]
    if missing_months:
        labels = ", ".join(month.strftime("%B %Y") for month in missing_months)
        raise ValidationError(f"Scenario belum lengkap. Projection belum dibuat untuk: {labels}.")
    save_scenario_draft(
        scenario.id,
        actor,
        sales_values=sales_values,
        incoming_values=incoming_values,
        reason=reason,
    )
    projections = list(SalesProjection.objects.filter(scenario=scenario).order_by("month", "sku__sku"))
    plans_by_projection = {
        plan.sales_projection_id: plan
        for plan in IncomingPlan.objects.filter(scenario=scenario)
    }
    if len(plans_by_projection) != len(projections):
        raise ValidationError("Incoming Plan belum lengkap untuk seluruh Draft Projection.")
    for projection in projections:
        approve_sales_projection(projection.id, projection.proposed_qty, actor, reason)
    for projection in projections:
        plan = plans_by_projection[projection.id]
        approve_incoming_plan(plan.id, plan.proposed_incoming, actor, reason)
    scenario.status = ProjectionScenario.Status.APPROVED
    scenario.approved_by = actor
    scenario.approved_at = timezone.now()
    scenario.full_clean()
    scenario.save(update_fields=["status", "approved_by", "approved_at"])
    record_audit(
        actor=actor,
        action="projection_scenario_approved",
        entity_type="merchandising.projectionscenario",
        entity_id=scenario.id,
        reason=reason,
        after_values={
            "status": scenario.status,
            "projection_count": len(projections),
            "incoming_plan_count": len(plans_by_projection),
        },
    )
    return scenario


@transaction.atomic
def delete_draft_scenario(scenario_id, actor, reason="Draft scenario dihapus oleh user"):
    """Delete a never-approved scenario and its draft-only planning rows, preserving an audit event."""
    scenario = ProjectionScenario.objects.select_for_update().get(pk=scenario_id)
    if scenario.status != ProjectionScenario.Status.DRAFT:
        raise ValidationError("Hanya Scenario yang masih Draft yang dapat dihapus.")
    if scenario.projections.filter(
        approval_status=SalesProjection.ApprovalStatus.APPROVED,
    ).exists() or scenario.incoming_plans.filter(
        approval_status=IncomingPlan.ApprovalStatus.APPROVED,
    ).exists():
        raise ValidationError(
            "Scenario memiliki baris yang sudah Approved dan tidak boleh dihapus."
        )
    if scenario.incoming_plans.filter(ppic_requirements__isnull=False).exists():
        raise ValidationError(
            "Scenario sudah memiliki PPIC Requirement dan tidak boleh dihapus."
        )
    if scenario.superseded_scenarios.exists():
        raise ValidationError(
            "Scenario sudah menjadi referensi scenario lain dan tidak boleh dihapus."
        )

    snapshot = {
        "name": scenario.name,
        "start_month": scenario.start_month.isoformat(),
        "end_month": scenario.end_month.isoformat(),
        "rule_count": scenario.rules.count(),
        "projection_count": scenario.projections.count(),
        "incoming_plan_count": scenario.incoming_plans.count(),
    }
    scenario_id_snapshot = scenario.id
    scenario.incoming_plans.all().delete()
    scenario.projections.all().delete()
    scenario.rules.all().delete()
    scenario.delete()
    record_audit(
        actor=actor,
        action="projection_scenario_draft_deleted",
        entity_type="merchandising.projectionscenario",
        entity_id=scenario_id_snapshot,
        reason=reason,
        before_values=snapshot,
        after_values={"deleted": True},
    )
    return snapshot


@transaction.atomic
def delete_draft_scenario_items(
    scenario_id,
    actor,
    *,
    grain,
    identifiers,
    reason="Baris dihapus dari Scenario Draft oleh user",
):
    """Delete selected SKU/Parent SKU across every month in one Draft scenario."""
    scenario = ProjectionScenario.objects.select_for_update().get(pk=scenario_id)
    if scenario.status != ProjectionScenario.Status.DRAFT:
        raise ValidationError("Hanya baris dari Scenario yang masih Draft yang dapat dihapus.")
    if grain not in {"sku", "parent_sku"}:
        raise ValidationError("Tipe pilihan baris tidak dikenal.")

    identifiers = list(dict.fromkeys(str(value).strip() for value in identifiers if str(value).strip()))
    if not identifiers:
        raise ValidationError("Pilih minimal satu SKU atau Parent SKU yang akan dihapus.")

    projections = scenario.projections.select_for_update().select_related(
        "sku__product_variant__product"
    )
    if grain == "sku":
        projections = projections.filter(sku_id__in=identifiers)
    else:
        projections = projections.filter(
            Q(sku__product_variant__product__parent_sku__in=identifiers)
            | (
                Q(sku__product_variant__product__parent_sku="")
                & Q(sku__product_variant__product__code__in=identifiers)
            )
        )

    projection_rows = list(projections)
    if not projection_rows:
        raise ValidationError("Baris terpilih tidak ditemukan pada Scenario Draft ini.")
    projection_ids = [projection.id for projection in projection_rows]
    if any(
        projection.approval_status == SalesProjection.ApprovalStatus.APPROVED
        for projection in projection_rows
    ):
        raise ValidationError("Pilihan memiliki Sales Projection Approved dan tidak boleh dihapus.")

    incoming_plans = scenario.incoming_plans.select_for_update().filter(
        sales_projection_id__in=projection_ids
    )
    if incoming_plans.filter(
        approval_status=IncomingPlan.ApprovalStatus.APPROVED
    ).exists():
        raise ValidationError("Pilihan memiliki Incoming Plan Approved dan tidak boleh dihapus.")
    if incoming_plans.filter(ppic_requirements__isnull=False).exists():
        raise ValidationError("Pilihan sudah memiliki PPIC Requirement dan tidak boleh dihapus.")

    affected_rule_ids = {
        projection.applied_rule_id
        for projection in projection_rows
        if projection.applied_rule_id
    }
    snapshot = {
        "scenario": scenario.name,
        "grain": grain,
        "identifiers": identifiers,
        "sku_codes": sorted({projection.sku.sku for projection in projection_rows}),
        "months": sorted({projection.month.isoformat() for projection in projection_rows}),
        "projection_count": len(projection_rows),
        "incoming_plan_count": incoming_plans.count(),
    }
    incoming_plan_count = snapshot["incoming_plan_count"]
    incoming_plans.delete()
    projections.delete()
    deleted_rule_count = scenario.rules.filter(
        id__in=affected_rule_ids,
        projection_results__isnull=True,
    ).delete()[0]

    record_audit(
        actor=actor,
        action="projection_scenario_draft_items_deleted",
        entity_type="merchandising.projectionscenario",
        entity_id=scenario.id,
        reason=reason,
        before_values=snapshot,
        after_values={
            "projection_count": 0,
            "incoming_plan_count": 0,
            "deleted_rule_count": deleted_rule_count,
        },
    )
    return {
        **snapshot,
        "deleted_projection_count": len(projection_rows),
        "deleted_incoming_plan_count": incoming_plan_count,
        "deleted_rule_count": deleted_rule_count,
    }


@transaction.atomic
def update_draft_scenario(
    scenario_id,
    actor,
    *,
    name,
    start_month,
    end_month,
    reason="Scenario Draft diperbarui",
):
    """Edit a Draft scenario while keeping every existing child month inside its new range."""
    scenario = ProjectionScenario.objects.select_for_update().get(pk=scenario_id)
    if scenario.status != ProjectionScenario.Status.DRAFT:
        raise ValidationError("Hanya Scenario yang masih Draft yang dapat diubah.")
    linked_months = set(scenario.rules.values_list("target_month", flat=True))
    linked_months.update(scenario.projections.values_list("month", flat=True))
    linked_months.update(scenario.incoming_plans.values_list("month", flat=True))
    excluded_months = sorted(
        month for month in linked_months if month < start_month or month > end_month
    )
    if excluded_months:
        labels = ", ".join(month.strftime("%B %Y") for month in excluded_months)
        raise ValidationError(
            f"Periode baru tidak boleh mengeluarkan bulan yang sudah memiliki data draft: {labels}."
        )
    if ProjectionScenario.objects.filter(
        name__iexact=name.strip(),
        start_month=start_month,
        end_month=end_month,
    ).exclude(pk=scenario.id).exists():
        raise ValidationError("Scenario dengan nama dan periode yang sama sudah ada.")
    before_values = {
        "name": scenario.name,
        "start_month": scenario.start_month.isoformat(),
        "end_month": scenario.end_month.isoformat(),
    }
    scenario.name = name.strip()
    scenario.start_month = start_month
    scenario.end_month = end_month
    scenario.full_clean()
    scenario.save(update_fields=["name", "start_month", "end_month"])
    record_audit(
        actor=actor,
        action="projection_scenario_draft_updated",
        entity_type="merchandising.projectionscenario",
        entity_id=scenario.id,
        reason=reason,
        before_values=before_values,
        after_values={
            "name": scenario.name,
            "start_month": scenario.start_month.isoformat(),
            "end_month": scenario.end_month.isoformat(),
        },
    )
    return scenario
