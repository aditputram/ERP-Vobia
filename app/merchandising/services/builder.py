from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Sum

from inventory.services.fifo import inventory_balance
from master_data.models import SKU
from sales.models import SalesOrderLine

from ..models import IncomingPlan, ProjectionRule, SalesProjection
from .calculations import (
    apply_product_guardrail,
    current_month_projection,
    future_projection,
    select_effective_rule,
)


def previous_month(month):
    return date(month.year - (1 if month.month == 1 else 0), 12 if month.month == 1 else month.month - 1, 1)


def selected_skus(*, scope_type, product_status=None, category=None, product=None):
    queryset = SKU.objects.filter(is_active=True).select_related(
        "product_variant__product__status",
        "product_variant__product__category",
    )
    if scope_type == ProjectionRule.ScopeType.PRODUCT_STATUS and product_status:
        return queryset.filter(product_variant__product__status=product_status)
    if scope_type == ProjectionRule.ScopeType.CATEGORY and category:
        return queryset.filter(product_variant__product__category=category)
    if scope_type == ProjectionRule.ScopeType.PRODUCT and product:
        return queryset.filter(product_variant__product=product)
    raise ValidationError("Scope projection belum dipilih dengan benar.")


def projected_beginning(sku, target_month, today=None):
    today = today or date.today()
    current_month = today.replace(day=1)
    balance = Decimal(inventory_balance(sku, as_of_date=today if target_month <= current_month else None))
    if target_month <= current_month:
        actual = SalesOrderLine.objects.filter(
            sku=sku,
            is_counted=True,
            order__order_date__gte=target_month,
            order__order_date__lte=today,
        ).exclude(order__current_status="Retur").aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        # Ledger balance is after sales through cutoff; adding actual sales back
        # reconstructs Ending prior month + Incoming current month.
        return balance + actual
    incoming = IncomingPlan.objects.filter(
        sku=sku,
        approval_status=IncomingPlan.ApprovalStatus.APPROVED,
        month__gte=current_month,
        month__lte=target_month,
    ).aggregate(total=Sum("final_approved_incoming"))["total"] or Decimal("0")
    planned_sales = SalesProjection.objects.filter(
        sku=sku,
        approval_status=SalesProjection.ApprovalStatus.APPROVED,
        month__gte=current_month,
        month__lt=target_month,
    ).aggregate(total=Sum("final_approved_qty"))["total"] or Decimal("0")
    return balance + incoming - planned_sales


def recommendation_for(*, sku, target_month, method, parameter, today=None):
    today = today or date.today()
    current_month = today.replace(day=1)
    product = sku.product_variant.product
    beginning = projected_beginning(sku, target_month, today=today)
    baseline_month = None
    baseline_qty = None
    if target_month == current_month:
        current_lines = SalesOrderLine.objects.filter(
            is_counted=True,
            order__order_date__gte=current_month,
            order__order_date__lte=today,
        ).exclude(order__current_status="Retur")
        data_cutoff = current_lines.aggregate(value=Max("order__order_date"))["value"]
        actual = SalesOrderLine.objects.filter(
            sku=sku,
            is_counted=True,
            order__order_date__gte=current_month,
            order__order_date__lte=data_cutoff or today,
        ).exclude(order__current_status="Retur").aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        beginning = projected_beginning(sku, target_month, today=data_cutoff or today)
        recommendation = current_month_projection(
            actual,
            data_cutoff,
            beginning,
            run_date=today,
        ) if data_cutoff else Decimal("0")
        baseline_qty = actual
        baseline_month = data_cutoff or current_month
    elif target_month > current_month:
        baseline_month = previous_month(target_month)
        prior = SalesProjection.objects.filter(
            sku=sku,
            month=baseline_month,
            approval_status=SalesProjection.ApprovalStatus.APPROVED,
        ).first()
        if method in {ProjectionRule.Method.INCREASE_PERCENT, ProjectionRule.Method.DECREASE_PERCENT}:
            if prior is None:
                raise ValidationError(f"{sku.sku}: Final Approved Projection {baseline_month:%b %Y} belum tersedia.")
            baseline_qty = prior.final_approved_qty
        else:
            baseline_qty = prior.final_approved_qty if prior else Decimal("0")
        recommendation = future_projection(method, baseline_qty, parameter, beginning_qty=beginning)
    else:
        raise ValidationError("Projection historis tidak boleh dibangun ulang dari halaman ini.")
    recommendation = apply_product_guardrail(
        recommendation,
        product.status.name,
        product.category.name,
        available_stock=beginning,
    )
    return {
        "sku": sku,
        "baseline_month": baseline_month,
        "baseline_qty": baseline_qty,
        "beginning_qty": beginning,
        "recommendation": recommendation,
    }


def preview_rule(*, scenario, target_month, scope_type, method, parameter, product_status=None, category=None, product=None):
    if target_month < scenario.start_month or target_month > scenario.end_month:
        raise ValidationError("Target Month harus berada dalam periode Scenario.")
    rows = []
    errors = []
    for sku in selected_skus(
        scope_type=scope_type,
        product_status=product_status,
        category=category,
        product=product,
    ):
        try:
            rows.append(
                recommendation_for(
                    sku=sku,
                    target_month=target_month,
                    method=method,
                    parameter=parameter,
                )
            )
        except ValidationError as exc:
            errors.extend(exc.messages)
    return rows, errors


@transaction.atomic
def apply_rule(*, scenario, target_month, scope_type, method, parameter, actor, product_status=None, category=None, product=None, reason=""):
    rows, errors = preview_rule(
        scenario=scenario,
        target_month=target_month,
        scope_type=scope_type,
        method=method,
        parameter=parameter,
        product_status=product_status,
        category=category,
        product=product,
    )
    if errors:
        raise ValidationError(errors)
    rule = ProjectionRule(
        scenario=scenario,
        target_month=target_month,
        scope_type=scope_type,
        product_status=product_status,
        category=category,
        product=product,
        method=method,
        parameter=parameter,
        created_by=actor,
        reason=reason,
    )
    rule.full_clean()
    rule.save()
    all_rules = list(scenario.rules.filter(target_month=target_month))
    applied = 0
    overridden = 0
    for row in rows:
        effective, _ = select_effective_rule(all_rules, row["sku"])
        if effective and effective.id != rule.id:
            overridden += 1
            continue
        SalesProjection.objects.update_or_create(
            scenario=scenario,
            month=target_month,
            sku=row["sku"],
            defaults={
                "applied_rule": rule,
                "baseline_month": row["baseline_month"],
                "baseline_qty": row["baseline_qty"],
                "beginning_qty": row["beginning_qty"],
                "system_recommendation": row["recommendation"],
                "adit_adjustment": None,
                "final_approved_qty": None,
                "approval_status": SalesProjection.ApprovalStatus.DRAFT,
                "approved_by": None,
                "approved_at": None,
            },
        )
        applied += 1
    return rule, {"applied": applied, "overridden": overridden}
