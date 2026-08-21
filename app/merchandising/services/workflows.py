from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit
from merchandising.models import IncomingPlan, SalesProjection

from .calculations import (
    NO_INCOMING_CATEGORIES,
    NO_INCOMING_STATUSES,
    incoming_calculation,
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
