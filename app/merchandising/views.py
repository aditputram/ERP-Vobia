from calendar import month_abbr, month_name
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render

from inventory.services.fifo import inventory_balance

from .forms import IncomingMonthCloseForm, ProjectionBuilderForm, ProjectionScenarioForm
from .models import (
    IncomingCarryover,
    IncomingMonthClose,
    IncomingPlan,
    MerchandisingMonthlySnapshot,
    MerchandisingSnapshotBatch,
    ProjectionScenario,
    SalesProjection,
)
from .services.builder import apply_rule, preview_rule
from .services.official_projection import official_current_month_values, official_planning_state
from .services.incoming_actuals import (
    carryover_totals,
    close_incoming_month,
    incoming_comparison,
    official_incoming_actuals,
)
from .services.workflows import approve_incoming_plan, approve_sales_projection, create_incoming_plan


SUMMARY_METRICS = (
    ("incoming_cogs", "Incoming COGS", "money"),
    ("incoming_gross", "Incoming Gross", "money"),
    ("beginning_gross", "Beginning Gross", "money"),
    ("sales_gross", "Sales Gross", "money"),
    ("sales_net", "Sales Net", "money"),
    ("sales_cogs", "Sales COGS", "money"),
    ("ending_gross", "Ending Stock Gross", "money"),
    ("ending_cogs", "Ending Stock COGS", "money"),
)

PROJECTION_METRIC_GROUPS = (
    ("incoming", "Incoming"),
    ("beginning", "Beginning"),
    ("sales", "Sales"),
    ("ending", "Ending"),
    ("stock_ratio", "Stock Ratio"),
    ("mos", "MOS"),
)

PROJECTION_SUBMETRICS = (
    ("qty", "QTY"),
    ("cogs", "COGS"),
    ("gross", "Gross"),
    ("net", "Net"),
)

PROJECTION_FIELD_MAP = {
    "incoming": {"qty": "incoming_qty", "cogs": "incoming_cogs", "gross": "incoming_gross"},
    "beginning": {"qty": "beginning_qty", "cogs": "beginning_cogs", "gross": "beginning_gross"},
    "sales": {"qty": "sales_qty", "cogs": "sales_cogs", "gross": "sales_gross", "net": "sales_net"},
    "ending": {"qty": "ending_qty", "cogs": "ending_cogs", "gross": "ending_gross"},
}

PROJECTION_FIELD_META = {
    "incoming_qty": ("Incoming QTY", "number"),
    "incoming_cogs": ("Incoming COGS", "money"),
    "incoming_gross": ("Incoming Gross", "money"),
    "beginning_qty": ("Beginning QTY", "number"),
    "beginning_cogs": ("Beginning COGS", "money"),
    "beginning_gross": ("Beginning Gross", "money"),
    "sales_qty": ("Sales QTY", "number"),
    "sales_cogs": ("Sales COGS", "money"),
    "sales_gross": ("Sales Gross", "money"),
    "sales_net": ("Sales Net", "money"),
    "ending_qty": ("Ending QTY", "number"),
    "ending_cogs": ("Ending COGS", "money"),
    "ending_gross": ("Ending Gross", "money"),
    "ratio": ("Stock Ratio", "ratio"),
    "mos": ("MOS", "ratio"),
}


def _active_batch():
    return MerchandisingSnapshotBatch.objects.filter(is_active=True).first()


def _getlist(request, key):
    return [value for value in request.GET.getlist(key) if value]


def _filtered_snapshots(request, batch):
    rows = MerchandisingMonthlySnapshot.objects.filter(batch=batch)
    selected = {
        "status": _getlist(request, "status"),
        "category": _getlist(request, "category"),
        "product": _getlist(request, "product"),
    }
    if selected["status"]:
        rows = rows.filter(status_snapshot__in=selected["status"])
    if selected["category"]:
        rows = rows.filter(category_snapshot__in=selected["category"])
    if selected["product"]:
        rows = rows.filter(product_snapshot__in=selected["product"])
    query = request.GET.get("q", "").strip()
    if query:
        rows = rows.filter(
            Q(sku__sku__icontains=query)
            | Q(product_snapshot__icontains=query)
            | Q(variant_snapshot__icontains=query)
        )
    return rows, selected, query


def _filter_options(batch):
    if not batch:
        return {"statuses": [], "categories": [], "products": []}
    rows = MerchandisingMonthlySnapshot.objects.filter(batch=batch)
    return {
        "statuses": sorted(set(rows.values_list("status_snapshot", flat=True))),
        "categories": sorted(set(rows.values_list("category_snapshot", flat=True))),
        "products": sorted(set(rows.values_list("product_snapshot", flat=True))),
    }


def _divide(numerator, denominator):
    return numerator / denominator if denominator else None


def _present(values):
    return [value for value in values if value is not None]


def _sum_present(values):
    return sum(_present(values), Decimal("0"))


def _last_present(values):
    present = _present(values)
    return present[-1] if present else None


@login_required
def dashboard(request):
    batch = _active_batch()
    table_rows = []
    filtered_count = 0
    source_range_exceptions = 0
    selected = {"status": [], "category": [], "product": []}
    query = ""
    planning_state = None
    incoming_mode = request.GET.get("incoming_mode", "projection")
    if incoming_mode not in {"projection", "actual", "comparison"}:
        incoming_mode = "projection"
    incoming_comparison_summary = None
    if batch:
        snapshots, selected, query = _filtered_snapshots(request, batch)
        sku_ids = list(snapshots.order_by().values_list("sku_id", flat=True).distinct())
        filtered_count = len(sku_ids)
        source_range_exceptions = snapshots.filter(source_row__gt=693).values("sku_id").distinct().count()
        planning_state = official_planning_state(batch)
        planning_state["current_month"] = month_name[planning_state["current_month_number"]]
        current_values = official_current_month_values(batch, sku_ids, planning_state)
        aggregates = {
            row["month"].month: row
            for row in snapshots.values("month").annotate(
                **{field: Sum(field) for field, _, _ in SUMMARY_METRICS},
                prior_year_ending_cogs=Sum("prior_year_ending_cogs"),
            )
        }
        month_values = {month: aggregates.get(month, {}) for month in range(1, 13)}
        current_month = planning_state["current_month_number"]
        current_aggregate = month_values[current_month]
        for field in (
            "beginning_gross", "sales_gross", "sales_net", "sales_cogs",
            "ending_gross", "ending_cogs",
        ):
            current_aggregate[field] = sum(
                (values[field] for values in current_values.values()), Decimal("0")
            )
        comparison = incoming_comparison(batch, date(planning_state["year"], current_month, 1), sku_ids)
        projected_incoming = sum((row["projection"]["incoming_qty"] for row in comparison.values()), Decimal("0"))
        actual_incoming = sum((row["actual"]["incoming_qty"] for row in comparison.values()), Decimal("0"))
        incoming_comparison_summary = {
            "projected_qty": projected_incoming,
            "actual_qty": actual_incoming,
            "variance_qty": actual_incoming - projected_incoming,
            "is_closed": IncomingMonthClose.objects.filter(month=date(planning_state["year"], current_month, 1)).exists(),
        }
        if incoming_mode == "actual":
            current_actuals = official_incoming_actuals(date(planning_state["year"], current_month, 1), sku_ids)
            for field in ("incoming_qty", "incoming_cogs", "incoming_gross"):
                current_aggregate[field] = sum((values[field] for values in current_actuals.values()), Decimal("0"))
        for future_month in range(current_month + 1, 13):
            month_values[future_month] = {
                **month_values[future_month],
                **{field: None for field, _, _ in SUMMARY_METRICS},
            }

        for field, label, kind in SUMMARY_METRICS:
            values = [month_values[month].get(field) for month in range(1, 13)]
            total = _last_present(values) if field.startswith("ending_") else _sum_present(values)
            if field == "beginning_gross":
                available = _present(values)
                total = _divide(sum(available, Decimal("0")), Decimal(len(available)))
            table_rows.append({"label": label, "kind": kind, "values": values, "total": total})

        beginning = [month_values[m].get("beginning_gross") for m in range(1, 13)]
        sales_gross = [month_values[m].get("sales_gross") for m in range(1, 13)]
        sales_net = [month_values[m].get("sales_net") for m in range(1, 13)]
        sales_cogs = [month_values[m].get("sales_cogs") for m in range(1, 13)]
        ending_cogs = [month_values[m].get("ending_cogs") for m in range(1, 13)]
        incoming_cogs = [month_values[m].get("incoming_cogs") for m in range(1, 13)]
        previous_cogs = month_values[1].get("prior_year_ending_cogs", Decimal("0")) or Decimal("0")
        stock_ratio = [
            _divide(beginning[i], sales_gross[i])
            if beginning[i] is not None and sales_gross[i] is not None else None
            for i in range(12)
        ]
        cumulative_cogs = Decimal("0")
        ito = []
        for index in range(12):
            if sales_cogs[index] is None or ending_cogs[index] is None:
                ito.append(None)
                continue
            cumulative_cogs += sales_cogs[index]
            ito.append(_divide(cumulative_cogs, (previous_cogs + ending_cogs[index]) / 2))
        gpm = [
            sales_net[i] - sales_cogs[i]
            if sales_net[i] is not None and sales_cogs[i] is not None else None
            for i in range(12)
        ]
        gpm_rate = [
            _divide(sales_net[i] - sales_cogs[i], sales_gross[i])
            if None not in (sales_net[i], sales_cogs[i], sales_gross[i]) else None
            for i in range(12)
        ]
        margin = [
            _divide(sales_net[i], sales_cogs[i])
            if sales_net[i] is not None and sales_cogs[i] is not None else None
            for i in range(12)
        ]
        roi = [
            _divide(sales_gross[i], incoming_cogs[i])
            if sales_gross[i] is not None and incoming_cogs[i] is not None else None
            for i in range(12)
        ]
        beginning_total = _sum_present(beginning)
        sales_gross_total = _sum_present(sales_gross)
        sales_net_total = _sum_present(sales_net)
        sales_cogs_total = _sum_present(sales_cogs)
        incoming_cogs_total = _sum_present(incoming_cogs)
        calculated = (
            ("Stock Value Ratio", "ratio2", stock_ratio, _divide(beginning_total, sales_gross_total)),
            ("ITO (YTD)", "ratio2", ito, _last_present(ito)),
            ("GPM", "money", gpm, sales_net_total - sales_cogs_total),
            ("GPM Rate", "percent", gpm_rate, _divide(sales_net_total - sales_cogs_total, sales_gross_total)),
            ("Margin Ratio", "ratio2", margin, _divide(sales_net_total, sales_cogs_total)),
            ("Incoming Capital Turnover", "ratio2", roi, _divide(sales_gross_total, incoming_cogs_total)),
            ("Ending COGS (Last Year)", "money", [previous_cogs] * 12, previous_cogs),
        )
        table_rows.extend(
            {"label": label, "kind": kind, "values": values, "total": total}
            for label, kind, values, total in calculated
        )

    context = {
        "batch": batch,
        "months": [month_name[m] for m in range(1, 13)],
        "table_rows": table_rows,
        "filtered_count": filtered_count,
        "source_range_exceptions": source_range_exceptions,
        "selected": selected,
        "query": query,
        "planning_state": planning_state,
        "incoming_mode": incoming_mode,
        "incoming_comparison_summary": incoming_comparison_summary,
        **_filter_options(batch),
    }
    return render(request, "merchandising/dashboard.html", context)


@login_required
def overview(request):
    return dashboard(request)


@login_required
def projection(request):
    preview_rows = []
    preview_errors = []
    if request.method == "POST" and request.POST.get("form_name") == "scenario":
        scenario_form = ProjectionScenarioForm(request.POST)
        builder_form = ProjectionBuilderForm()
        if scenario_form.is_valid():
            scenario = scenario_form.save(commit=False)
            scenario.created_by = request.user
            scenario.full_clean()
            scenario.save()
            messages.success(request, "Scenario projection berhasil dibuat.")
            return redirect("merchandising:projection")
    elif request.method == "POST" and request.POST.get("form_name") == "builder":
        scenario_form = ProjectionScenarioForm()
        builder_form = ProjectionBuilderForm(request.POST)
        if builder_form.is_valid():
            data = builder_form.cleaned_data
            try:
                if request.POST.get("action") == "apply":
                    _, counts = apply_rule(actor=request.user, **data)
                    messages.success(request, f"Rule diterapkan ke {counts['applied']} SKU; {counts['overridden']} SKU tetap memakai rule prioritas lebih tinggi.")
                    return redirect("merchandising:projection")
                preview_rows, preview_errors = preview_rule(
                    scenario=data["scenario"],
                    target_month=data["target_month"],
                    scope_type=data["scope_type"],
                    method=data["method"],
                    parameter=data["parameter"],
                    product_status=data["product_status"],
                    category=data["category"],
                    product=data["product"],
                )
            except ValidationError as exc:
                builder_form.add_error(None, exc)
    else:
        scenario_form = ProjectionScenarioForm()
        builder_form = ProjectionBuilderForm()
    batch = _active_batch()
    selected = {"status": [], "category": [], "product": []}
    query = ""
    visible_row_count = 0
    dynamic_headers = []
    table_rows = []
    table_summary = None
    planning_state = None
    incoming_mode = request.GET.get("incoming_mode", "projection")
    if incoming_mode not in {"projection", "actual", "comparison"}:
        incoming_mode = "projection"
    incoming_comparison_summary = None
    carryover_rows = []
    selected_months = sorted({int(value) for value in _getlist(request, "month") if value.isdigit() and 1 <= int(value) <= 12})
    selected_metrics = [value for value in _getlist(request, "metric") if value in {item[0] for item in PROJECTION_METRIC_GROUPS}]
    selected_submetrics = [value for value in _getlist(request, "submetric") if value in {item[0] for item in PROJECTION_SUBMETRICS}]
    sku_type = request.GET.get("sku_type", "sku")
    if sku_type not in {"sku", "parent"}:
        sku_type = "sku"
    if not selected_months:
        selected_months = [date.today().month]
    if not selected_metrics:
        selected_metrics = ["incoming", "beginning", "sales", "ending", "stock_ratio", "mos"]
    if not selected_submetrics:
        selected_submetrics = ["qty"]
    if batch:
        snapshots, selected, query = _filtered_snapshots(request, batch)
        planning_state = official_planning_state(batch)
        current_month_number = planning_state["current_month_number"]
        planning_state["current_month"] = month_name[current_month_number]
        identity_rows = list(snapshots.order_by("source_row").values(
            "sku_id", "sku__sku", "source_row", "status_snapshot", "product_snapshot",
            "variant_snapshot", "category_snapshot", "subcategory_snapshot", "size_snapshot",
            "cogs_snapshot", "retail_price_snapshot",
            "sku__product_variant__product__parent_sku",
        ).distinct())
        sku_ids = [row["sku_id"] for row in identity_rows]
        monthly = MerchandisingMonthlySnapshot.objects.filter(
            batch=batch, sku_id__in=sku_ids
        ).in_bulk(field_name="id")
        by_sku_month = {(row.sku_id, row.month.month): row for row in monthly.values()}
        current_values = official_current_month_values(batch, sku_ids, planning_state)
        current_month_date = date(planning_state["year"], current_month_number, 1)
        comparison = incoming_comparison(batch, current_month_date, sku_ids)
        projected_incoming = sum((row["projection"]["incoming_qty"] for row in comparison.values()), Decimal("0"))
        actual_incoming = sum((row["actual"]["incoming_qty"] for row in comparison.values()), Decimal("0"))
        incoming_comparison_summary = {
            "month": current_month_date,
            "projected_qty": projected_incoming,
            "actual_qty": actual_incoming,
            "variance_qty": actual_incoming - projected_incoming,
            "is_closed": IncomingMonthClose.objects.filter(month=current_month_date).exists(),
        }
        if incoming_mode == "actual":
            actual_values = official_incoming_actuals(current_month_date, sku_ids)
            zero_actual = {
                "incoming_qty": Decimal("0"),
                "incoming_cogs": Decimal("0"),
                "incoming_gross": Decimal("0"),
            }
            for sku_id in sku_ids:
                if sku_id in current_values:
                    current_values[sku_id].update(actual_values.get(sku_id, zero_actual))
        approved_incoming = {}
        for plan in IncomingPlan.objects.filter(
            sku_id__in=sku_ids,
            month__gt=current_month_date,
            approval_status=IncomingPlan.ApprovalStatus.APPROVED,
        ).order_by("approved_at", "id"):
            approved_incoming[(plan.sku_id, plan.month.month)] = plan.final_approved_incoming or Decimal("0")
        approved_sales = {}
        for sales_plan in SalesProjection.objects.filter(
            sku_id__in=sku_ids,
            month__gt=current_month_date,
            approval_status=SalesProjection.ApprovalStatus.APPROVED,
        ).order_by("approved_at", "id"):
            approved_sales[(sales_plan.sku_id, sales_plan.month.month)] = sales_plan.final_approved_qty or Decimal("0")
        carryover_map = {
            (row["sku_id"], row["target_month"].month): row["qty"] or Decimal("0")
            for row in IncomingCarryover.objects.filter(
                target_month__gt=current_month_date,
                sku_id__in=sku_ids,
            ).order_by().values("sku_id", "target_month").annotate(qty=Sum("carryover_qty"))
        }
        future_values = {}
        identity_value_map = {row["sku_id"]: row for row in identity_rows}
        for sku_id in sku_ids:
            prior_ending = current_values.get(sku_id, {}).get("ending_qty")
            if prior_ending is None:
                current_snapshot = by_sku_month.get((sku_id, current_month_number))
                prior_ending = current_snapshot.ending_qty if current_snapshot else Decimal("0")
            identity = identity_value_map[sku_id]
            cogs = identity["cogs_snapshot"] or Decimal("0")
            retail = identity["retail_price_snapshot"] or Decimal("0")
            for future_month in range(current_month_number + 1, 13):
                new_incoming = approved_incoming.get((sku_id, future_month))
                carryover = carryover_map.get((sku_id, future_month), Decimal("0"))
                planned_sales = approved_sales.get((sku_id, future_month))
                has_plan = new_incoming is not None or planned_sales is not None or carryover > 0
                incoming_qty = (new_incoming or Decimal("0")) + carryover
                beginning_qty = prior_ending + incoming_qty
                sales_qty = min(planned_sales or Decimal("0"), max(beginning_qty, Decimal("0")))
                ending_qty = beginning_qty - sales_qty
                if has_plan:
                    sales_gross = sales_qty * retail
                    values = {
                        "incoming_qty": incoming_qty,
                        "incoming_cogs": incoming_qty * cogs,
                        "incoming_gross": incoming_qty * retail,
                        "beginning_qty": beginning_qty,
                        "beginning_cogs": beginning_qty * cogs,
                        "beginning_gross": beginning_qty * retail,
                        "sales_qty": sales_qty,
                        "sales_cogs": sales_qty * cogs,
                        "sales_gross": sales_gross,
                        "sales_net": None,
                        "ending_qty": ending_qty,
                        "ending_cogs": ending_qty * cogs,
                        "ending_gross": ending_qty * retail,
                        "ratio": (beginning_qty * retail / sales_gross) if sales_gross else None,
                        "mos": (ending_qty / sales_qty) if sales_qty else None,
                    }
                    future_values[(sku_id, future_month)] = values
                prior_ending = ending_qty
        carryover_rows = IncomingCarryover.objects.filter(
            target_month__gte=current_month_date,
            sku_id__in=sku_ids,
        ).select_related("source_close", "po_line__po", "sku")[:300]
        seen_columns = set()
        projection_year = planning_state["year"]
        for month_number in selected_months:
            previous_month = 12 if month_number == 1 else month_number - 1
            previous_year = projection_year - 1 if month_number == 1 else projection_year
            for submetric in selected_submetrics:
                previous_field = PROJECTION_FIELD_MAP["ending"].get(submetric)
                if not previous_field:
                    continue
                key = (previous_year, previous_month, previous_field)
                if key not in seen_columns:
                    label, kind = PROJECTION_FIELD_META[previous_field]
                    dynamic_headers.append({
                        "year": previous_year,
                        "month_number": previous_month,
                        "month": f"{month_abbr[previous_month]} {previous_year}" if previous_year != projection_year else month_abbr[previous_month],
                        "metric": previous_field,
                        "label": label,
                        "kind": kind,
                        "is_auto_previous": True,
                    })
                    seen_columns.add(key)
            for metric_group in selected_metrics:
                if metric_group in {"stock_ratio", "mos"}:
                    field = "ratio" if metric_group == "stock_ratio" else "mos"
                    fields = [field]
                else:
                    fields = [
                        field
                        for submetric in selected_submetrics
                        if (field := PROJECTION_FIELD_MAP[metric_group].get(submetric))
                    ]
                for field in fields:
                    if (
                        incoming_mode == "comparison"
                        and metric_group == "incoming"
                        and month_number == current_month_number
                    ):
                        label, kind = PROJECTION_FIELD_META[field]
                        for comparison_mode, comparison_label in (
                            ("projection", "Projection"),
                            ("actual", "Actual"),
                            ("variance", "Variance"),
                        ):
                            comparison_metric = f"{field}__{comparison_mode}"
                            key = (projection_year, month_number, comparison_metric)
                            if key in seen_columns:
                                continue
                            dynamic_headers.append({
                                "year": projection_year,
                                "month_number": month_number,
                                "month": month_abbr[month_number],
                                "metric": comparison_metric,
                                "label": f"{label} · {comparison_label}",
                                "kind": kind,
                                "is_auto_previous": False,
                                "comparison_mode": comparison_mode,
                            })
                            seen_columns.add(key)
                        continue
                    key = (projection_year, month_number, field)
                    if key in seen_columns:
                        continue
                    label, kind = PROJECTION_FIELD_META[field]
                    dynamic_headers.append({
                        "year": projection_year,
                        "month_number": month_number,
                        "month": month_abbr[month_number],
                        "metric": field,
                        "label": label,
                        "kind": kind,
                        "is_auto_previous": False,
                    })
                    seen_columns.add(key)
        def metric_value(sku_id, year, month_number, metric):
            if "__" in metric:
                base_metric, comparison_mode = metric.split("__", 1)
                if year != projection_year or month_number != current_month_number:
                    return None
                values = comparison.get(sku_id, {})
                projection_value = values.get("projection", {}).get(base_metric, Decimal("0"))
                actual_value = values.get("actual", {}).get(base_metric, Decimal("0"))
                if comparison_mode == "projection":
                    return projection_value
                if comparison_mode == "actual":
                    return actual_value
                return actual_value - projection_value
            snapshot = by_sku_month.get((sku_id, month_number))
            if year < projection_year:
                january = by_sku_month.get((sku_id, 1))
                prior_field = metric.replace("ending_", "prior_year_ending_", 1)
                return getattr(january, prior_field, None) if january else None
            if month_number > current_month_number:
                return future_values.get((sku_id, month_number), {}).get(metric)
            if not snapshot:
                return None
            if month_number == current_month_number and metric in current_values.get(sku_id, {}):
                return current_values[sku_id][metric]
            return getattr(snapshot, metric)

        def aggregate_metric_for_ids(ids, header, metric):
            values = [
                metric_value(sku_id, header["year"], header["month_number"], metric)
                for sku_id in ids
            ]
            present = [value for value in values if value is not None]
            return sum(present, Decimal("0")) if present else None

        if sku_type == "parent":
            grouped_rows = {}
            for identity in identity_rows:
                parent_sku = identity["sku__product_variant__product__parent_sku"] or identity["sku__sku"]
                group = grouped_rows.setdefault(parent_sku, {"identities": [], "sku_ids": []})
                group["identities"].append(identity)
                group["sku_ids"].append(identity["sku_id"])

            for parent_sku, group in grouped_rows.items():
                first = group["identities"][0]

                def common_value(field, mixed="Mixed"):
                    values = {row[field] for row in group["identities"] if row[field]}
                    return next(iter(values)) if len(values) == 1 else mixed

                parent_identity = {
                    **first,
                    "sku__sku": parent_sku,
                    "status_snapshot": common_value("status_snapshot"),
                    "product_snapshot": common_value("product_snapshot"),
                    "variant_snapshot": "All variants",
                    "category_snapshot": common_value("category_snapshot"),
                    "subcategory_snapshot": common_value("subcategory_snapshot", "—"),
                    "size_snapshot": "All sizes",
                    "cogs_snapshot": None,
                    "retail_price_snapshot": None,
                    "is_parent": True,
                    "child_sku_count": len(group["sku_ids"]),
                }

                cells = []
                for header in dynamic_headers:
                    metric = header["metric"]
                    if metric == "ratio":
                        numerator = aggregate_metric_for_ids(group["sku_ids"], header, "beginning_qty")
                        denominator = aggregate_metric_for_ids(group["sku_ids"], header, "sales_qty")
                        value = numerator / denominator if numerator is not None and denominator else None
                    elif metric == "mos":
                        numerator = aggregate_metric_for_ids(group["sku_ids"], header, "ending_qty")
                        denominator = aggregate_metric_for_ids(group["sku_ids"], header, "sales_qty")
                        value = numerator / denominator if numerator is not None and denominator else None
                    else:
                        value = aggregate_metric_for_ids(group["sku_ids"], header, metric)
                    cells.append({"value": value, "kind": header["kind"]})
                table_rows.append({"identity": parent_identity, "cells": cells})
        else:
            for identity in identity_rows:
                cells = [
                    {
                        "value": metric_value(
                            identity["sku_id"], header["year"], header["month_number"], header["metric"]
                        ),
                        "kind": header["kind"],
                    }
                    for header in dynamic_headers
                ]
                table_rows.append({"identity": identity, "cells": cells})
        summary_cells = []
        for header in dynamic_headers:
            metric = header["metric"]
            if metric == "ratio":
                numerator = aggregate_metric_for_ids(sku_ids, header, "beginning_qty")
                denominator = aggregate_metric_for_ids(sku_ids, header, "sales_qty")
                value = numerator / denominator if numerator is not None and denominator else None
            elif metric == "mos":
                numerator = aggregate_metric_for_ids(sku_ids, header, "ending_qty")
                denominator = aggregate_metric_for_ids(sku_ids, header, "sales_qty")
                value = numerator / denominator if numerator is not None and denominator else None
            else:
                value = aggregate_metric_for_ids(sku_ids, header, metric)
            summary_cells.append({"value": value, "kind": header["kind"]})
        table_summary = {"cells": summary_cells, "sku_count": len(sku_ids)}
        visible_row_count = len(table_rows)

    return render(
        request,
        "merchandising/projection.html",
        {
            "scenario_form": scenario_form,
            "builder_form": builder_form,
            "preview_rows": preview_rows,
            "preview_errors": preview_errors,
            "scenarios": ProjectionScenario.objects.all()[:20],
            "projections": SalesProjection.objects.select_related("scenario", "sku")[:200],
            "incoming_plans": IncomingPlan.objects.select_related("sku", "scenario")[:200],
            "batch": batch,
            "month_options": [(month, month_abbr[month]) for month in range(1, 13)],
            "metric_options": PROJECTION_METRIC_GROUPS,
            "submetric_options": PROJECTION_SUBMETRICS,
            "selected_months": selected_months,
            "selected_metrics": selected_metrics,
            "selected_submetrics": selected_submetrics,
            "sku_type": sku_type,
            "row_identity_label": "Parent SKU" if sku_type == "parent" else "SKU",
            "selected": selected,
            "query": query,
            "visible_row_count": visible_row_count,
            "dynamic_headers": dynamic_headers,
            "table_rows": table_rows,
            "table_summary": table_summary,
            "planning_state": planning_state,
            "incoming_mode": incoming_mode,
            "incoming_comparison_summary": incoming_comparison_summary,
            "carryover_rows": carryover_rows,
            "month_close_form": IncomingMonthCloseForm(),
            "month_closes": IncomingMonthClose.objects.prefetch_related("actual_rows", "carryovers")[:12],
            **_filter_options(batch),
        },
    )


@login_required
def approve_projection(request, projection_id):
    projection = get_object_or_404(SalesProjection, pk=projection_id)
    if request.method == "POST":
        try:
            approve_sales_projection(projection.id, request.POST.get("final_qty", ""), request.user, request.POST.get("reason", ""))
        except (ValidationError, ArithmeticError) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, f"Projection {projection.sku.sku} disetujui.")
    return redirect("merchandising:projection")


@login_required
def make_incoming(request, projection_id):
    projection = get_object_or_404(SalesProjection, pk=projection_id)
    if request.method == "POST":
        prior = request.POST.get("prior_ending_qty")
        ratio = request.POST.get("target_stock_ratio") or None
        try:
            create_incoming_plan(projection.id, prior, ratio)
        except (ValidationError, ArithmeticError) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, f"Incoming Plan {projection.sku.sku} dibuat.")
    return redirect("merchandising:projection")


@login_required
def approve_incoming(request, plan_id):
    plan = get_object_or_404(IncomingPlan, pk=plan_id)
    if request.method == "POST":
        try:
            approve_incoming_plan(plan.id, request.POST.get("final_incoming", ""), request.user, request.POST.get("reason", ""))
        except (ValidationError, ArithmeticError) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, f"Incoming {plan.sku.sku} approved dan otomatis masuk PPIC.")
    return redirect("merchandising:projection")


@login_required
def close_incoming(request):
    if request.method != "POST":
        return redirect("merchandising:projection")
    form = IncomingMonthCloseForm(request.POST)
    if form.is_valid():
        try:
            close = close_incoming_month(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"Incoming {close.month:%B %Y} ditutup; actual snapshot dan carry-over PO telah dibekukan.")
    else:
        messages.error(request, "Month close belum valid. Isi bulan dan referensi bukti.")
    return redirect("merchandising:projection")
