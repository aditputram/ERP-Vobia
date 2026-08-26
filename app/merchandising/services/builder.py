from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Sum

from inventory.services.fifo import inventory_balance
from master_data.models import SKU
from sales.models import SalesOrderLine

from ..models import (
    IncomingPlan,
    MerchandisingMonthlySnapshot,
    MerchandisingSnapshotBatch,
    ProjectionRule,
    SalesProjection,
)
from .calculations import (
    NO_INCOMING_CATEGORIES,
    NO_INCOMING_STATUSES,
    apply_product_guardrail,
    current_month_projection,
    future_projection,
    planning_buffer_incoming,
    select_effective_rule,
)
from .official_projection import official_current_month_values, official_planning_state


def previous_month(month):
    return date(month.year - (1 if month.month == 1 else 0), 12 if month.month == 1 else month.month - 1, 1)


def next_month(month):
    return date(month.year + (1 if month.month == 12 else 0), 1 if month.month == 12 else month.month + 1, 1)


def preceding_months(target_month, count=3):
    """Return the calendar months before a target, oldest first."""
    months = []
    cursor = target_month
    for _ in range(count):
        cursor = previous_month(cursor)
        months.append(cursor)
    return list(reversed(months))


def historical_sales_qty_for_skus(
    skus,
    target_month,
    *,
    scenario=None,
    today=None,
    baseline_by_sku=None,
):
    """Resolve three prior Sales QTY layers for Planning Builder display.

    Closed months use the immutable MD snapshot, the running month uses the
    official current-month projection, and future prior months use the approved
    (or same-scenario Draft) Sales Projection.  The immediate prior month can be
    overridden by the recommendation row's stored baseline so the displayed
    history and the calculation baseline can never disagree.
    """
    skus = list(skus)
    months = preceding_months(target_month)
    values = {
        sku.id: {month: Decimal("0") for month in months}
        for sku in skus
    }
    if not skus:
        return months, values

    sku_ids = [sku.id for sku in skus]
    today = today or date.today()
    current_month = today.replace(day=1)
    closed_months = [month for month in months if month < current_month]
    batch = MerchandisingSnapshotBatch.objects.filter(is_active=True).first()
    snapshot_keys = set()
    if batch and closed_months:
        for row in MerchandisingMonthlySnapshot.objects.filter(
            batch=batch,
            sku_id__in=sku_ids,
            month__in=closed_months,
        ).values("sku_id", "month", "sales_qty"):
            values[row["sku_id"]][row["month"]] = Decimal(row["sales_qty"] or 0)
            snapshot_keys.add((row["sku_id"], row["month"]))

    # A defensive canonical fallback keeps newly added SKUs visible even if an
    # older MD snapshot row was absent. Returns follow the Operation convention.
    missing_closed = {
        (sku_id, month)
        for sku_id in sku_ids
        for month in closed_months
        if (sku_id, month) not in snapshot_keys
    }
    if missing_closed:
        fallback = (
            SalesOrderLine.objects.filter(
                is_counted=True,
                sku_id__in=sku_ids,
                order__order_date__gte=min(closed_months),
                order__order_date__lt=next_month(max(closed_months)),
            )
            .exclude(order__current_status="Retur")
            .values("sku_id", "order__order_date__year", "order__order_date__month")
            .annotate(total=Sum("quantity"))
        )
        for row in fallback:
            month = date(
                row["order__order_date__year"],
                row["order__order_date__month"],
                1,
            )
            key = (row["sku_id"], month)
            if key in missing_closed:
                values[row["sku_id"]][month] = Decimal(row["total"] or 0)

    if current_month in months:
        official = official_values_for_skus(skus, today)
        for sku_id, row in official.items():
            values[sku_id][current_month] = Decimal(row["sales_qty"] or 0)

    future_months = [month for month in months if month > current_month]
    if future_months:
        approved_keys = set()
        for projection in SalesProjection.objects.filter(
            sku_id__in=sku_ids,
            month__in=future_months,
            approval_status=SalesProjection.ApprovalStatus.APPROVED,
        ).order_by("month", "sku_id", "approved_at"):
            values[projection.sku_id][projection.month] = Decimal(
                projection.final_approved_qty or 0
            )
            approved_keys.add((projection.sku_id, projection.month))
        if scenario is not None:
            for projection in SalesProjection.objects.filter(
                scenario=scenario,
                sku_id__in=sku_ids,
                month__in=future_months,
                approval_status=SalesProjection.ApprovalStatus.DRAFT,
            ):
                if (projection.sku_id, projection.month) not in approved_keys:
                    values[projection.sku_id][projection.month] = Decimal(
                        projection.proposed_qty or 0
                    )

    immediate_prior = previous_month(target_month)
    for sku_id, baseline in (baseline_by_sku or {}).items():
        if sku_id in values and immediate_prior in values[sku_id] and baseline is not None:
            values[sku_id][immediate_prior] = Decimal(baseline)
    return months, values


def drafted_product_ids(target_month):
    """Products already claimed by a saved projection for the target month."""
    if not target_month:
        return SalesProjection.objects.none().values_list(
            "sku__product_variant__product_id", flat=True
        )
    return SalesProjection.objects.filter(month=target_month).values_list(
        "sku__product_variant__product_id", flat=True
    ).distinct()


def aggregate_draft_by_parent(projections):
    """Aggregate saved SKU projections by target month and Parent SKU."""
    grouped = {}
    for projection in projections:
        sku = projection.sku
        product = sku.product_variant.product
        parent_sku = product.parent_sku or product.code or sku.sku
        key = (projection.month, parent_sku)
        group = grouped.setdefault(
            key,
            {
                "month": projection.month,
                "parent_sku": parent_sku,
                "product_names": set(),
                "sku_count": 0,
                "baseline_qty": Decimal("0"),
                "beginning_qty": Decimal("0"),
                "proposed_qty": Decimal("0"),
                "baseline_complete": True,
                "beginning_complete": True,
                "approval_statuses": set(),
                "approval_labels": set(),
            },
        )
        group["product_names"].add(product.name)
        group["sku_count"] += 1
        if projection.baseline_qty is None:
            group["baseline_complete"] = False
        else:
            group["baseline_qty"] += projection.baseline_qty
        if projection.beginning_qty is None:
            group["beginning_complete"] = False
        else:
            group["beginning_qty"] += projection.beginning_qty
        group["proposed_qty"] += projection.proposed_qty
        group["approval_statuses"].add(projection.approval_status)
        group["approval_labels"].add(projection.get_approval_status_display())

    rows = []
    for group in grouped.values():
        group["product_name"] = " / ".join(sorted(group.pop("product_names")))
        if not group.pop("baseline_complete"):
            group["baseline_qty"] = None
        if not group.pop("beginning_complete"):
            group["beginning_qty"] = None
        statuses = group.pop("approval_statuses")
        labels = group.pop("approval_labels")
        group["approval_status"] = next(iter(statuses)) if len(statuses) == 1 else "MIXED"
        group["approval_label"] = next(iter(labels)) if len(labels) == 1 else "Mixed"
        rows.append(group)
    return sorted(rows, key=lambda row: (row["month"], row["product_name"], row["parent_sku"]))


def summarize_draft(projections, parent_rows):
    """Build auditable additive totals for both draft display grains."""
    projections = list(projections)
    baseline_complete = all(row.baseline_qty is not None for row in projections)
    beginning_complete = all(row.beginning_qty is not None for row in projections)
    return {
        "sku_row_count": len(projections),
        "parent_row_count": len(parent_rows),
        "sku_count": len({row.sku_id for row in projections}),
        "parent_count": len({row["parent_sku"] for row in parent_rows}),
        "product_count": len({row.sku.product_variant.product_id for row in projections}),
        "baseline_qty": (
            sum((row.baseline_qty for row in projections), Decimal("0"))
            if baseline_complete else None
        ),
        "beginning_qty": (
            sum((row.beginning_qty for row in projections), Decimal("0"))
            if beginning_complete else None
        ),
        "proposed_qty": sum((row.proposed_qty for row in projections), Decimal("0")),
    }


def build_draft_matrix(
    projections,
    selected_months,
    selected_metrics,
    grain="sku",
    incoming_plans=None,
    selected_submetrics=("qty",),
    history_months=(),
    history_by_sku=None,
    sales_target_by_sku_month=None,
):
    """Pivot saved SKU-month projections into a horizontal planning matrix."""
    if grain not in {"sku", "parent_sku"}:
        raise ValueError("Draft grain tidak dikenal.")
    metric_labels = {
        "baseline": "Baseline",
        "beginning": "Beginning",
        "sales": "Sales Projection",
        "ending": "Ending",
        "stock_ratio": "Stock Ratio",
        "incoming_recommendation": "Incoming Plan",
    }
    submetric_meta = {
        "qty": ("QTY", "number"),
        "cogs": ("COGS", "money"),
        "gross": ("Gross", "money"),
        "net": ("Net", "money"),
    }
    invalid_submetrics = set(selected_submetrics) - set(submetric_meta)
    if invalid_submetrics:
        raise ValueError("Draft sub metric tidak dikenal.")
    projections = list(projections)
    history_months = list(history_months or [])
    history_by_sku = history_by_sku or {}
    include_sales_target = sales_target_by_sku_month is not None
    sales_target_by_sku_month = sales_target_by_sku_month or {}
    plans_by_projection = {
        plan.sales_projection_id: plan for plan in (incoming_plans or [])
    }
    grouped = {}
    for projection in projections:
        sku = projection.sku
        product = sku.product_variant.product
        if grain == "sku":
            key = sku.id
            identity = sku.sku
        else:
            identity = product.parent_sku or product.code or sku.sku
            key = identity
        row = grouped.setdefault(
            key,
            {
                "identity": identity,
                "selection_value": str(sku.id) if grain == "sku" else identity,
                "parent_identity": product.parent_sku or product.code or sku.sku,
                "product_names": set(),
                "sku_ids": set(),
                "months": {},
            },
        )
        row["product_names"].add(product.name)
        row["sku_ids"].add(sku.id)
        bucket = row["months"].setdefault(
            projection.month,
            {
                "baseline": Decimal("0"),
                "beginning": Decimal("0"),
                "sales": Decimal("0"),
                "incoming": Decimal("0"),
                "minimum_incoming": Decimal("0"),
                "baseline_complete": True,
                "beginning_complete": True,
                "projections": [],
            },
        )
        if projection.baseline_qty is None:
            bucket["baseline_complete"] = False
        else:
            bucket["baseline"] += projection.baseline_qty
        if projection.beginning_qty is None:
            bucket["beginning_complete"] = False
        else:
            bucket["beginning"] += projection.beginning_qty
        bucket["sales"] += projection.proposed_qty
        product = projection.sku.product_variant.product
        incoming_allowed = (
            product.status.name not in NO_INCOMING_STATUSES
            and product.category.name not in NO_INCOMING_CATEGORIES
        )
        minimum_incoming = (
            planning_buffer_incoming(
                projection.proposed_qty,
                projection.beginning_qty,
                incoming_allowed=incoming_allowed,
            )
            if projection.beginning_qty is not None
            else Decimal("0")
        )
        plan = plans_by_projection.get(projection.id)
        bucket["incoming"] += max(plan.proposed_incoming, minimum_incoming) if plan else minimum_incoming
        bucket["minimum_incoming"] += minimum_incoming
        bucket["projections"].append(projection)

    def projection_quantities(projection):
        plan = plans_by_projection.get(projection.id)
        base_beginning = projection.beginning_qty
        product = projection.sku.product_variant.product
        incoming_allowed = (
            product.status.name not in NO_INCOMING_STATUSES
            and product.category.name not in NO_INCOMING_CATEGORIES
        )
        minimum_incoming = (
            planning_buffer_incoming(
                projection.proposed_qty,
                base_beginning,
                incoming_allowed=incoming_allowed,
            )
            if base_beginning is not None
            else Decimal("0")
        )
        incoming = plan.proposed_incoming if plan else minimum_incoming
        incoming = max(incoming, minimum_incoming)
        beginning = base_beginning + incoming if base_beginning is not None else None
        return {
            "baseline": projection.baseline_qty,
            "beginning": beginning,
            "sales": projection.proposed_qty,
            "ending": beginning - projection.proposed_qty if beginning is not None else None,
            "incoming_recommendation": incoming,
        }

    def projection_value(projection, metric, submetric):
        qty = projection_quantities(projection)[metric]
        if qty is None:
            return None
        if submetric == "qty":
            return qty
        if submetric == "cogs":
            return qty * projection.cogs_snapshot
        gross = qty * projection.retail_price_snapshot
        if submetric == "gross":
            return gross
        if submetric == "net" and metric == "sales":
            return (gross * projection.net_rate_snapshot).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        return None

    def bucket_value(bucket, metric, submetric=None):
        if not bucket:
            return None
        if metric == "stock_ratio":
            beginning_gross = Decimal("0")
            sales_gross = Decimal("0")
            for projection in bucket["projections"]:
                beginning_value = projection_value(projection, "beginning", "gross")
                sales_value = projection_value(projection, "sales", "gross")
                if beginning_value is None:
                    return None
                beginning_gross += beginning_value
                sales_gross += sales_value
            return beginning_gross / sales_gross if sales_gross else None
        values = [
            projection_value(projection, metric, submetric)
            for projection in bucket["projections"]
        ]
        if any(value is None for value in values):
            return None
        return sum(values, Decimal("0"))

    def bucket_growth(bucket):
        if not bucket or not bucket["baseline_complete"] or not bucket["baseline"]:
            return None
        return ((bucket["sales"] - bucket["baseline"]) / bucket["baseline"]) * Decimal("100")

    headers = [
        {
            "month": month,
            "month_label": month.strftime("%b %Y"),
            "metric": "historical_sales",
            "submetric": "qty",
            "label": (
                "Baseline Sales QTY"
                if index == len(history_months) - 1
                else "Historical Sales QTY"
            ),
            "kind": "number",
        }
        for index, month in enumerate(history_months)
    ]
    for month in selected_months:
        for metric in selected_metrics:
            if metric == "stock_ratio":
                headers.append({
                    "month": month,
                    "month_label": month.strftime("%b %Y"),
                    "metric": metric,
                    "submetric": None,
                    "label": metric_labels[metric],
                    "kind": "ratio",
                })
                continue
            for submetric in selected_submetrics:
                if submetric == "net" and metric != "sales":
                    continue
                submetric_label, kind = submetric_meta[submetric]
                if include_sales_target and metric == "sales" and submetric == "qty":
                    headers.append({
                        "month": month,
                        "month_label": month.strftime("%b %Y"),
                        "metric": "sales_target",
                        "submetric": "qty",
                        "label": "Target Sales (Sales) QTY",
                        "kind": "number",
                    })
                headers.append({
                    "month": month,
                    "month_label": month.strftime("%b %Y"),
                    "metric": metric,
                    "submetric": submetric,
                    "label": f"{metric_labels[metric]} {submetric_label}",
                    "kind": kind,
                })
    rows = []
    for row in grouped.values():
        row["product_name"] = " / ".join(sorted(row.pop("product_names")))
        row_sku_ids = set(row["sku_ids"])
        row["sku_count"] = len(row_sku_ids)
        cells = []
        for header in headers:
            if header["metric"] == "historical_sales":
                cells.append({
                    "value": sum(
                        (
                            Decimal(history_by_sku.get(sku_id, {}).get(header["month"], 0))
                            for sku_id in row_sku_ids
                        ),
                        Decimal("0"),
                    ),
                    "kind": header["kind"],
                    "metric": header["metric"],
                    "submetric": header["submetric"],
                    "month_key": header["month"].strftime("%Y-%m"),
                    "growth_pct": None,
                    "baseline_value": None,
                    "editable": None,
                })
                continue
            if header["metric"] == "sales_target":
                target_values = [
                    sales_target_by_sku_month.get((sku_id, header["month"]))
                    for sku_id in row_sku_ids
                ]
                cells.append({
                    "value": (
                        sum(target_values, Decimal("0"))
                        if target_values and all(value is not None for value in target_values)
                        else None
                    ),
                    "kind": header["kind"],
                    "metric": header["metric"],
                    "submetric": header["submetric"],
                    "month_key": header["month"].strftime("%Y-%m"),
                    "growth_pct": None,
                    "baseline_value": None,
                    "editable": None,
                })
                continue
            bucket = row["months"].get(header["month"])
            cell = {
                "value": bucket_value(bucket, header["metric"], header["submetric"]),
                "kind": header["kind"],
                "metric": header["metric"],
                "submetric": header["submetric"],
                "month_key": header["month"].strftime("%Y-%m"),
                "growth_pct": (
                    bucket_growth(bucket)
                    if header["metric"] == "sales" and header["submetric"] == "qty"
                    else None
                ),
                "baseline_value": (
                    bucket["baseline"]
                    if bucket and bucket["baseline_complete"]
                    else None
                ),
                "editable": None,
            }
            if grain == "sku" and bucket and len(bucket["projections"]) == 1:
                projection = bucket["projections"][0]
                cell["projection_id"] = projection.id
                cell["beginning"] = projection.beginning_qty or Decimal("0")
                if header["metric"] == "sales" and header["submetric"] == "qty":
                    cell["editable"] = "sales"
                    cell["input_name"] = f"sales_qty_{projection.id}"
                elif (
                    header["metric"] == "incoming_recommendation"
                    and header["submetric"] == "qty"
                ):
                    cell["editable"] = "incoming"
                    cell["input_name"] = f"incoming_qty_{projection.id}"
                    cell["minimum"] = bucket["minimum_incoming"]
            cells.append(cell)
        row["cells"] = cells
        row.pop("sku_ids")
        row.pop("months")
        rows.append(row)
    rows.sort(key=lambda row: (row["product_name"], row["identity"]))

    total_buckets = {}
    for projection in projections:
        bucket = total_buckets.setdefault(
            projection.month,
            {
                "baseline": Decimal("0"),
                "beginning": Decimal("0"),
                "sales": Decimal("0"),
                "incoming": Decimal("0"),
                "minimum_incoming": Decimal("0"),
                "baseline_complete": True,
                "beginning_complete": True,
                "projections": [],
            },
        )
        if projection.baseline_qty is None:
            bucket["baseline_complete"] = False
        else:
            bucket["baseline"] += projection.baseline_qty
        if projection.beginning_qty is None:
            bucket["beginning_complete"] = False
        else:
            bucket["beginning"] += projection.beginning_qty
        bucket["sales"] += projection.proposed_qty
        minimum_incoming = (
            max(projection.proposed_qty - projection.beginning_qty, Decimal("0"))
            if projection.beginning_qty is not None
            else Decimal("0")
        )
        plan = plans_by_projection.get(projection.id)
        bucket["incoming"] += plan.proposed_incoming if plan else minimum_incoming
        bucket["minimum_incoming"] += minimum_incoming
        bucket["projections"].append(projection)
    summary = {
        "projection_count": len(projections),
        "sku_count": len({row.sku_id for row in projections}),
        "product_count": len({row.sku.product_variant.product_id for row in projections}),
        "parent_count": len({
            row.sku.product_variant.product.parent_sku
            or row.sku.product_variant.product.code
            or row.sku.sku
            for row in projections
        }),
        "cells": [],
    }
    unique_sku_ids = {row.sku_id for row in projections}
    for header in headers:
        if header["metric"] == "historical_sales":
            value = sum(
                (
                    Decimal(history_by_sku.get(sku_id, {}).get(header["month"], 0))
                    for sku_id in unique_sku_ids
                ),
                Decimal("0"),
            )
            growth_pct = None
        elif header["metric"] == "sales_target":
            target_values = [
                sales_target_by_sku_month.get((sku_id, header["month"]))
                for sku_id in unique_sku_ids
            ]
            value = (
                sum(target_values, Decimal("0"))
                if target_values and all(item is not None for item in target_values)
                else None
            )
            growth_pct = None
        else:
            bucket = total_buckets.get(header["month"])
            value = bucket_value(bucket, header["metric"], header["submetric"])
            growth_pct = (
                bucket_growth(bucket)
                if header["metric"] == "sales" and header["submetric"] == "qty"
                else None
            )
        summary["cells"].append({
            "value": value,
            "kind": header["kind"],
            "metric": header["metric"],
            "submetric": header["submetric"],
            "month_key": header["month"].strftime("%Y-%m"),
            "growth_pct": growth_pct,
        })
    return rows, headers, summary


def aggregate_preview_by_parent(rows):
    """Aggregate a SKU-grain preview for display without losing SKU draft inputs."""
    grouped = {}
    sum_fields = (
        "sales_30d",
        "inbound_30d",
        "activity_ending_qty",
        "baseline_qty",
        "beginning_qty",
        "recommendation",
        "incoming_gap",
    )
    for row in rows:
        sku = row["sku"]
        product = sku.product_variant.product
        parent_sku = product.parent_sku or product.code or sku.sku
        group = grouped.setdefault(
            parent_sku,
            {
                "parent_sku": parent_sku,
                "product_names": set(),
                "sku_count": 0,
                **{field: Decimal("0") for field in sum_fields},
                "previous_ending_qty": Decimal("0"),
                "has_previous_ending": False,
                "history_cells": [
                    {
                        "month": cell["month"],
                        "value": Decimal("0"),
                        "is_baseline": cell.get("is_baseline", False),
                    }
                    for cell in row.get("history_cells", [])
                ],
            },
        )
        group["product_names"].add(product.name)
        group["sku_count"] += 1
        for field in sum_fields:
            group[field] += Decimal(row.get(field) or Decimal("0"))
        for index, cell in enumerate(row.get("history_cells", [])):
            group["history_cells"][index]["value"] += Decimal(cell.get("value") or 0)
        if row.get("previous_ending_qty") is not None:
            group["previous_ending_qty"] += Decimal(row["previous_ending_qty"])
            group["has_previous_ending"] = True

    result = []
    for group in grouped.values():
        recommendation = group["recommendation"]
        baseline = group["baseline_qty"]
        beginning = group["beginning_qty"] + group["incoming_gap"]
        group["product_name"] = " / ".join(sorted(group.pop("product_names")))
        group["previous_ending_qty"] = (
            group["previous_ending_qty"] if group.pop("has_previous_ending") else None
        )
        group["growth_pct"] = (
            (recommendation - baseline) / baseline * Decimal("100")
            if baseline else None
        )
        group["planned_beginning_qty"] = beginning
        group["ending_qty"] = beginning - recommendation
        group["stock_ratio"] = beginning / recommendation if recommendation else None
        result.append(group)
    return sorted(result, key=lambda row: (row["product_name"], row["parent_sku"]))


def summarize_preview(rows):
    """Build additive totals and recomputed ratios for the whole preview scope."""
    sum_fields = (
        "sales_30d",
        "inbound_30d",
        "activity_ending_qty",
        "baseline_qty",
        "beginning_qty",
        "recommendation",
        "incoming_gap",
    )
    summary = {
        "sku_count": len(rows),
        **{field: Decimal("0") for field in sum_fields},
        "previous_ending_qty": Decimal("0"),
        "has_previous_ending": False,
        "history_cells": [
            {
                "month": cell["month"],
                "value": Decimal("0"),
                "is_baseline": cell.get("is_baseline", False),
            }
            for cell in (rows[0].get("history_cells", []) if rows else [])
        ],
    }
    for row in rows:
        for field in sum_fields:
            summary[field] += Decimal(row.get(field) or Decimal("0"))
        for index, cell in enumerate(row.get("history_cells", [])):
            summary["history_cells"][index]["value"] += Decimal(cell.get("value") or 0)
        if row.get("previous_ending_qty") is not None:
            summary["previous_ending_qty"] += Decimal(row["previous_ending_qty"])
            summary["has_previous_ending"] = True

    recommendation = summary["recommendation"]
    baseline = summary["baseline_qty"]
    beginning = summary["beginning_qty"] + summary["incoming_gap"]
    summary["previous_ending_qty"] = (
        summary["previous_ending_qty"] if summary.pop("has_previous_ending") else None
    )
    summary["growth_pct"] = (
        (recommendation - baseline) / baseline * Decimal("100") if baseline else None
    )
    summary["planned_beginning_qty"] = beginning
    summary["ending_qty"] = beginning - recommendation
    summary["stock_ratio"] = beginning / recommendation if recommendation else None
    return summary


def official_values_for_skus(skus, today):
    batch = MerchandisingSnapshotBatch.objects.filter(is_active=True).first()
    if not batch:
        return {}
    state = official_planning_state(batch, run_date=today)
    if not state or (state["year"], state["current_month_number"]) != (today.year, today.month):
        return {}
    return official_current_month_values(batch, [sku.id for sku in skus], state)


def selected_skus(*, scope_type, product_status=None, category=None, product=None):
    queryset = SKU.objects.filter(is_active=True).select_related(
        "product_variant__product__status",
        "product_variant__product__category",
    )
    if scope_type == ProjectionRule.ScopeType.ALL_PRODUCTS:
        return queryset
    if scope_type == ProjectionRule.ScopeType.PRODUCT_STATUS and product_status:
        return queryset.filter(product_variant__product__status=product_status)
    if scope_type == ProjectionRule.ScopeType.CATEGORY and category:
        return queryset.filter(product_variant__product__category=category)
    if scope_type == ProjectionRule.ScopeType.PRODUCT and product:
        return queryset.filter(product_variant__product=product)
    raise ValidationError("Scope projection belum dipilih dengan benar.")


def projected_beginning(
    sku,
    target_month,
    today=None,
    official_current_value=None,
    scenario=None,
):
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
    future_start = current_month
    if official_current_value is not None:
        balance = Decimal(official_current_value["ending_qty"])
        future_start = next_month(current_month)
    approved_incoming = IncomingPlan.objects.filter(
        sku=sku,
        approval_status=IncomingPlan.ApprovalStatus.APPROVED,
        month__gte=future_start,
        month__lte=target_month,
    )
    incoming = approved_incoming.aggregate(total=Sum("final_approved_incoming"))["total"] or Decimal("0")
    approved_sales = SalesProjection.objects.filter(
        sku=sku,
        approval_status=SalesProjection.ApprovalStatus.APPROVED,
        month__gte=future_start,
        month__lt=target_month,
    )
    planned_sales = approved_sales.aggregate(total=Sum("final_approved_qty"))["total"] or Decimal("0")
    if scenario is not None:
        approved_incoming_months = set(approved_incoming.values_list("month", flat=True))
        draft_incoming = IncomingPlan.objects.filter(
            scenario=scenario,
            sku=sku,
            approval_status=IncomingPlan.ApprovalStatus.DRAFT,
            month__gte=future_start,
            month__lte=target_month,
        ).exclude(month__in=approved_incoming_months)
        incoming += sum((plan.proposed_incoming for plan in draft_incoming), Decimal("0"))

        approved_sales_months = set(approved_sales.values_list("month", flat=True))
        draft_sales = SalesProjection.objects.filter(
            scenario=scenario,
            sku=sku,
            approval_status=SalesProjection.ApprovalStatus.DRAFT,
            month__gte=future_start,
            month__lt=target_month,
        ).exclude(month__in=approved_sales_months)
        planned_sales += sum((projection.proposed_qty for projection in draft_sales), Decimal("0"))
    return balance + incoming - planned_sales


def recommendation_for(
    *,
    sku,
    target_month,
    method,
    parameter,
    today=None,
    official_current_value=None,
    scenario=None,
):
    today = today or date.today()
    current_month = today.replace(day=1)
    product = sku.product_variant.product
    incoming_allowed = (
        product.status.name not in NO_INCOMING_STATUSES
        and product.category.name not in NO_INCOMING_CATEGORIES
    )
    if target_month > current_month and official_current_value is None:
        official_current_value = official_values_for_skus([sku], today).get(sku.id)
    beginning = projected_beginning(
        sku,
        target_month,
        today=today,
        official_current_value=official_current_value,
        scenario=scenario,
    )
    baseline_month = None
    baseline_qty = None
    previous_ending_qty = None
    incoming_qty = None
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
        incoming_qty = IncomingPlan.objects.filter(
            sku=sku,
            month=target_month,
            approval_status=IncomingPlan.ApprovalStatus.APPROVED,
        ).aggregate(total=Sum("final_approved_incoming"))["total"] or Decimal("0")
        previous_ending_qty = beginning - incoming_qty
        baseline_month = previous_month(target_month)
        if baseline_month == current_month:
            if official_current_value is None:
                raise ValidationError(f"{sku.sku}: Official Current Projection {baseline_month:%b %Y} belum tersedia.")
            baseline_qty = Decimal(official_current_value["sales_qty"])
        else:
            prior = SalesProjection.objects.filter(
                sku=sku,
                month=baseline_month,
                approval_status=SalesProjection.ApprovalStatus.APPROVED,
            ).first()
            if prior is None and scenario is not None:
                prior = SalesProjection.objects.filter(
                    scenario=scenario,
                    sku=sku,
                    month=baseline_month,
                    approval_status=SalesProjection.ApprovalStatus.DRAFT,
                ).first()
            if method in {
                ProjectionRule.Method.SAME_AS_LAST_MONTH,
                ProjectionRule.Method.INCREASE_PERCENT,
                ProjectionRule.Method.DECREASE_PERCENT,
                ProjectionRule.Method.SELL_OUT_ENDING_MONTHS,
            }:
                if prior is None:
                    raise ValidationError(
                        f"{sku.sku}: Draft atau Final Approved Projection {baseline_month:%b %Y} "
                        "pada scenario ini belum tersedia."
                    )
                baseline_qty = (
                    prior.final_approved_qty
                    if prior.approval_status == SalesProjection.ApprovalStatus.APPROVED
                    else prior.proposed_qty
                )
            else:
                baseline_qty = (
                    prior.final_approved_qty
                    if prior and prior.approval_status == SalesProjection.ApprovalStatus.APPROVED
                    else prior.proposed_qty if prior else Decimal("0")
                )
        recommendation = future_projection(
            method,
            baseline_qty,
            parameter,
            beginning_qty=beginning,
            previous_ending_qty=previous_ending_qty,
        )
    else:
        raise ValidationError("Projection historis tidak boleh dibangun ulang dari halaman ini.")
    recommendation = apply_product_guardrail(
        recommendation,
        product.status.name,
        product.category.name,
        available_stock=beginning,
    )
    recommendation = recommendation.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    incoming_gap = planning_buffer_incoming(
        recommendation,
        beginning,
        incoming_allowed=incoming_allowed,
    )
    planned_beginning_qty = beginning + incoming_gap
    ending_qty = planned_beginning_qty - recommendation
    stock_ratio = planned_beginning_qty / recommendation if recommendation else None
    growth_pct = (
        (recommendation - Decimal(baseline_qty)) / Decimal(baseline_qty) * Decimal("100")
        if baseline_qty not in (None, Decimal("0"))
        else None
    )
    return {
        "sku": sku,
        "baseline_month": baseline_month,
        "baseline_qty": baseline_qty,
        "previous_ending_qty": previous_ending_qty,
        "incoming_qty": incoming_qty,
        "beginning_qty": beginning,
        "planned_beginning_qty": planned_beginning_qty,
        "recommendation": recommendation,
        "growth_pct": growth_pct,
        "ending_qty": ending_qty,
        "stock_ratio": stock_ratio,
        "incoming_gap": incoming_gap,
        "incoming_allowed": incoming_allowed,
    }


def preview_rule(*, scenario, target_month, scope_type, method, parameter, product_status=None, category=None, product=None):
    if target_month < scenario.start_month or target_month > scenario.end_month:
        raise ValidationError("Target Month harus berada dalam periode Scenario.")
    rows = []
    errors = []
    skus = list(selected_skus(
        scope_type=scope_type,
        product_status=product_status,
        category=category,
        product=product,
    ).exclude(product_variant__product_id__in=drafted_product_ids(target_month)))
    if not skus:
        return [], ["Tidak ada Product yang tersedia. Product untuk Target Month ini mungkin sudah masuk Draft Projection."]
    official_values = official_values_for_skus(skus, date.today()) if target_month > date.today().replace(day=1) else {}
    for sku in skus:
        try:
            rows.append(
                recommendation_for(
                    sku=sku,
                    target_month=target_month,
                    method=method,
                    parameter=parameter,
                    official_current_value=official_values.get(sku.id),
                    scenario=scenario,
                )
            )
        except ValidationError as exc:
            errors.extend(exc.messages)
    return rows, errors


@transaction.atomic
def apply_rule(*, scenario, target_month, scope_type, method, parameter, actor, product_status=None, category=None, product=None, reason="", adjustments=None, incoming_adjustments=None):
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
        adjusted_qty = row["recommendation"]
        if adjustments and str(row["sku"].id) in adjustments:
            adjusted_qty = Decimal(adjustments[str(row["sku"].id)])
        if adjusted_qty < 0 or adjusted_qty != adjusted_qty.to_integral_value():
            raise ValidationError(f"{row['sku'].sku}: Sales Projection harus bilangan bulat dan tidak boleh negatif.")
        adjustment = adjusted_qty - row["recommendation"]
        projection_exists = SalesProjection.objects.filter(
            scenario=scenario,
            month=target_month,
            sku=row["sku"],
        ).exists()
        projection_defaults = {
            "applied_rule": rule,
            "baseline_month": row["baseline_month"],
            "baseline_qty": row["baseline_qty"],
            "beginning_qty": row["beginning_qty"],
            "system_recommendation": row["recommendation"],
            "adit_adjustment": adjustment if adjustment else None,
            "final_approved_qty": None,
            "approval_status": SalesProjection.ApprovalStatus.DRAFT,
            "approved_by": None,
            "approved_at": None,
        }
        if not projection_exists:
            projection_defaults.update(
                cogs_snapshot=row["sku"].current_master_cogs or Decimal("0"),
                retail_price_snapshot=row["sku"].current_retail_price or Decimal("0"),
                net_rate_snapshot=Decimal("0.97"),
            )
        projection, _ = SalesProjection.objects.update_or_create(
            scenario=scenario,
            month=target_month,
            sku=row["sku"],
            defaults=projection_defaults,
        )
        if incoming_adjustments is not None:
            minimum_incoming = planning_buffer_incoming(
                adjusted_qty,
                row["beginning_qty"],
                incoming_allowed=row["incoming_allowed"],
            )
            chosen_incoming = Decimal(
                incoming_adjustments.get(str(row["sku"].id), minimum_incoming)
            )
            if chosen_incoming < 0 or chosen_incoming != chosen_incoming.to_integral_value():
                raise ValidationError(
                    f"{row['sku'].sku}: Incoming Recommendation harus bilangan bulat dan tidak boleh negatif."
                )
            if chosen_incoming < minimum_incoming:
                raise ValidationError(
                    f"{row['sku'].sku}: Incoming Recommendation tidak boleh di bawah minimum {minimum_incoming:.0f}."
                )
            product_master = row["sku"].product_variant.product
            no_incoming = (
                product_master.status.name in NO_INCOMING_STATUSES
                or product_master.category.name in NO_INCOMING_CATEGORIES
            )
            if no_incoming and chosen_incoming != 0:
                raise ValidationError(
                    f"{row['sku'].sku}: Product ini tidak boleh memiliki Incoming baru."
                )
            system_incoming = row["incoming_gap"]
            incoming_plan, _ = IncomingPlan.objects.update_or_create(
                scenario=scenario,
                month=target_month,
                sku=row["sku"],
                defaults={
                    "sales_projection": projection,
                    "prior_ending_qty": row["beginning_qty"],
                    "minimum_incoming": minimum_incoming,
                    "target_stock_ratio": None,
                    "recommended_incoming": system_incoming,
                    "adit_adjustment": chosen_incoming - system_incoming or None,
                    "final_approved_incoming": None,
                    "approval_status": IncomingPlan.ApprovalStatus.DRAFT,
                    "approved_by": None,
                    "approved_at": None,
                },
            )
            incoming_plan.full_clean()
        applied += 1
    return rule, {"applied": applied, "overridden": overridden}
