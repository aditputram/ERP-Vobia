from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum

from merchandising.models import (
    IncomingCarryover,
    IncomingPlan,
    ProjectionScenario,
    SalesProjection,
)


ZERO = Decimal("0")
PLANNING_NET_RATE = Decimal("0.97")


def current_month_target_sales(*, sku_ids, planning_year, current_month_number):
    """Resolve Builder targets for the live month, with Approved taking priority over Draft."""
    sku_ids = list(sku_ids)
    target_month = date(planning_year, current_month_number, 1)
    approved = {}
    for row in SalesProjection.objects.filter(
        sku_id__in=sku_ids,
        month=target_month,
        scenario__status=ProjectionScenario.Status.APPROVED,
        approval_status=SalesProjection.ApprovalStatus.APPROVED,
    ).order_by("approved_at", "id"):
        approved[row.sku_id] = row.final_approved_qty or ZERO

    draft = {}
    draft_rows = SalesProjection.objects.filter(
        sku_id__in=sku_ids,
        month=target_month,
        scenario__status__in=[
            ProjectionScenario.Status.DRAFT,
            ProjectionScenario.Status.REVISION_DRAFT,
        ],
        approval_status=SalesProjection.ApprovalStatus.DRAFT,
    ).select_related("scenario").order_by("scenario__created_at", "id")
    for row in draft_rows:
        draft[row.sku_id] = row.proposed_qty

    draft_scenarios = list(
        ProjectionScenario.objects.filter(
            status__in=[
                ProjectionScenario.Status.DRAFT,
                ProjectionScenario.Status.REVISION_DRAFT,
            ],
            projections__sku_id__in=sku_ids,
            projections__month=target_month,
        )
        .distinct()
        .order_by("created_at")
        .values("id", "name")
    )
    return {**draft, **approved}, {
        "draft_scenarios": draft_scenarios,
        "draft_projection_count": draft_rows.count(),
    }


def future_planning_values(
    *,
    sku_ids,
    planning_year,
    current_month_number,
    prior_ending_by_sku,
    price_by_sku,
):
    """Build one future planning preview, with Approved rows taking precedence over Draft."""
    sku_ids = list(sku_ids)
    current_month_date = date(planning_year, current_month_number, 1)
    future_filter = {
        "sku_id__in": sku_ids,
        "month__year": planning_year,
        "month__gt": current_month_date,
    }
    approved_sales = {}
    for row in SalesProjection.objects.filter(
        **future_filter,
        scenario__status=ProjectionScenario.Status.APPROVED,
        approval_status=SalesProjection.ApprovalStatus.APPROVED,
    ).order_by("approved_at", "id"):
        approved_sales[(row.sku_id, row.month.month)] = row

    draft_sales = {}
    draft_rows = SalesProjection.objects.filter(
        **future_filter,
        scenario__status__in=[
            ProjectionScenario.Status.DRAFT,
            ProjectionScenario.Status.REVISION_DRAFT,
        ],
    ).select_related("scenario").order_by("scenario__created_at", "id")
    for row in draft_rows:
        draft_sales[(row.sku_id, row.month.month)] = row

    approved_incoming = {}
    for row in IncomingPlan.objects.filter(
        **future_filter,
        scenario__status=ProjectionScenario.Status.APPROVED,
        approval_status=IncomingPlan.ApprovalStatus.APPROVED,
    ).order_by("approved_at", "id"):
        approved_incoming[(row.sku_id, row.month.month)] = (
            row.final_approved_incoming or ZERO
        )

    draft_incoming = {}
    for row in IncomingPlan.objects.filter(
        **future_filter,
        scenario__status__in=[
            ProjectionScenario.Status.DRAFT,
            ProjectionScenario.Status.REVISION_DRAFT,
        ],
    ).order_by("scenario__created_at", "id"):
        draft_incoming[(row.sku_id, row.month.month)] = row.proposed_incoming

    carryover_map = {
        (row["sku_id"], row["target_month"].month): row["qty"] or ZERO
        for row in IncomingCarryover.objects.filter(
            target_month__year=planning_year,
            target_month__gt=current_month_date,
            sku_id__in=sku_ids,
        )
        .order_by()
        .values("sku_id", "target_month")
        .annotate(qty=Sum("carryover_qty"))
    }

    sales_map = {**draft_sales, **approved_sales}
    incoming_map = {**draft_incoming, **approved_incoming}
    values = {}
    for sku_id in sku_ids:
        prior_ending = prior_ending_by_sku.get(sku_id)
        prior_ending = ZERO if prior_ending is None else prior_ending
        price = price_by_sku.get(sku_id, {})
        cogs = price.get("cogs") or ZERO
        retail = price.get("retail") or ZERO
        for month_number in range(current_month_number + 1, 13):
            key = (sku_id, month_number)
            sales_plan = sales_map.get(key)
            planned_sales = (
                (sales_plan.final_approved_qty or ZERO)
                if sales_plan and sales_plan.approval_status == SalesProjection.ApprovalStatus.APPROVED
                else sales_plan.proposed_qty
                if sales_plan
                else None
            )
            planned_incoming = incoming_map.get(key)
            carryover = carryover_map.get(key, ZERO)
            has_sales_plan = sales_plan is not None
            has_incoming_plan = key in incoming_map
            if planned_incoming is None and planned_sales is not None:
                planned_incoming = max(planned_sales - prior_ending, ZERO)
            if sales_plan and (
                sales_plan.cogs_snapshot != ZERO
                or sales_plan.retail_price_snapshot != ZERO
            ):
                cogs = sales_plan.cogs_snapshot
                retail = sales_plan.retail_price_snapshot
                net_rate = sales_plan.net_rate_snapshot
            else:
                cogs = price.get("cogs") or ZERO
                retail = price.get("retail") or ZERO
                net_rate = PLANNING_NET_RATE
            incoming_qty = (planned_incoming or ZERO) + carryover
            beginning_qty = prior_ending + incoming_qty
            sales_qty = planned_sales or ZERO
            ending_qty = beginning_qty - sales_qty
            sales_gross = sales_qty * retail
            sales_net = (sales_gross * net_rate).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            values[key] = {
                "incoming_qty": incoming_qty,
                "incoming_cogs": incoming_qty * cogs,
                "incoming_gross": incoming_qty * retail,
                "beginning_qty": beginning_qty,
                "beginning_cogs": beginning_qty * cogs,
                "beginning_gross": beginning_qty * retail,
                "sales_qty": sales_qty,
                "sales_cogs": sales_qty * cogs,
                "sales_gross": sales_gross,
                "sales_net": sales_net,
                "ending_qty": ending_qty,
                "ending_cogs": ending_qty * cogs,
                "ending_gross": ending_qty * retail,
                "ratio": (beginning_qty * retail / sales_gross) if sales_gross else None,
                "mos": (ending_qty / sales_qty) if sales_qty else None,
                "planning_status": (
                    "APPROVED"
                    if key in approved_sales or key in approved_incoming
                    else "DRAFT"
                    if has_sales_plan or has_incoming_plan
                    else "UNPLANNED"
                ),
            }
            prior_ending = ending_qty

    draft_scenarios = list(
        ProjectionScenario.objects.filter(
            status__in=[
                ProjectionScenario.Status.DRAFT,
                ProjectionScenario.Status.REVISION_DRAFT,
            ],
            projections__sku_id__in=sku_ids,
            projections__month__year=planning_year,
            projections__month__gt=current_month_date,
        )
        .distinct()
        .order_by("created_at")
        .values("id", "name")
    )
    return values, {
        "draft_scenario_count": len(draft_scenarios),
        "draft_scenarios": draft_scenarios,
        "draft_projection_count": draft_rows.count(),
    }
