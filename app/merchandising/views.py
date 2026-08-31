from calendar import month_abbr, month_name
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from inventory.services.fifo import inventory_balance
from master_data.models import Category, Product, ProductStatus, Subcategory
from sales.models import SalesPlanSKU

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
from .services.builder import (
    aggregate_draft_by_parent,
    aggregate_preview_by_parent,
    apply_rule,
    build_draft_matrix,
    drafted_product_ids,
    historical_sales_qty_for_skus,
    next_month,
    preview_rule,
    previous_month,
    summarize_preview,
    summarize_draft,
)
from .services.calculations import planning_buffer_incoming
from .services.official_projection import official_current_month_values, official_planning_state
from .services.incoming_actuals import (
    carryover_totals,
    close_incoming_month,
    closed_cost_actuals,
    incoming_comparison,
    official_incoming_actuals,
)
from .services.planning_activity import (
    filter_products_by_planning_activity,
    planning_activity_snapshot,
)
from .services.planning_reporting import current_month_target_sales, future_planning_values
from .services.workflows import (
    approve_incoming_plan,
    approve_sales_projection,
    approve_scenario,
    open_scenario_revision,
    create_incoming_plan,
    delete_draft_scenario,
    delete_draft_scenario_items,
    save_scenario_draft,
    update_draft_scenario,
)


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

PROJECTION_DETAIL_COLUMNS = (
    ("status", "Status"),
    ("variant", "Variant"),
    ("category", "Category"),
    ("subcategory", "Sub Category"),
    ("size", "Size"),
    ("cogs", "COGS"),
    ("retail_price", "Retail Price"),
)

DRAFT_METRIC_OPTIONS = (
    ("baseline", "Baseline"),
    ("beginning", "Beginning"),
    ("sales", "Sales Projection"),
    ("ending", "Ending"),
    ("stock_ratio", "Stock Ratio"),
    ("incoming_recommendation", "Incoming Plan"),
)

DRAFT_SUBMETRIC_OPTIONS = PROJECTION_SUBMETRICS

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


def _latest_sales_target_qty(projections):
    """Return the latest saved Sales target for each SKU-month in scope."""
    projections = list(projections)
    sku_ids = {row.sku_id for row in projections}
    months = {row.month for row in projections}
    targets = {}
    rows = SalesPlanSKU.objects.filter(
        sku_id__in=sku_ids,
        plan__month__in=months,
    ).select_related("plan").order_by(
        "sku_id",
        "plan__month",
        "-updated_at",
        "-created_at",
        "-pk",
    )
    for row in rows:
        targets.setdefault((row.sku_id, row.plan.month), Decimal(row.quantity_target))
    return targets


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


def _cascading_projection_snapshots(request, batch):
    """Filter Projection rows and keep every downstream option within its parent scope."""
    base_rows = MerchandisingMonthlySnapshot.objects.filter(batch=batch)
    status_options = sorted(set(base_rows.values_list("status_snapshot", flat=True)))
    selected_statuses = [
        value for value in _getlist(request, "status") if value in status_options
    ]

    category_rows = base_rows
    if selected_statuses:
        category_rows = category_rows.filter(status_snapshot__in=selected_statuses)
    category_options = sorted(set(category_rows.values_list("category_snapshot", flat=True)))
    selected_categories = [
        value for value in _getlist(request, "category") if value in category_options
    ]

    product_rows = category_rows
    if selected_categories:
        product_rows = product_rows.filter(category_snapshot__in=selected_categories)
    product_options = sorted(set(product_rows.values_list("product_snapshot", flat=True)))
    selected_products = [
        value for value in _getlist(request, "product") if value in product_options
    ]

    rows = product_rows
    if selected_products:
        rows = rows.filter(product_snapshot__in=selected_products)
    query = request.GET.get("q", "").strip()
    if query:
        rows = rows.filter(
            Q(sku__sku__icontains=query)
            | Q(product_snapshot__icontains=query)
            | Q(variant_snapshot__icontains=query)
        )
    return (
        rows,
        {
            "status": selected_statuses,
            "category": selected_categories,
            "product": selected_products,
        },
        query,
        {
            "statuses": status_options,
            "categories": category_options,
            "products": product_options,
        },
    )


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
    planning_preview = {"draft_scenario_count": 0, "draft_scenarios": [], "draft_projection_count": 0}
    if batch:
        snapshots, selected, query = _filtered_snapshots(request, batch)
        sku_ids = list(snapshots.order_by().values_list("sku_id", flat=True).distinct())
        filtered_count = len(sku_ids)
        source_range_exceptions = snapshots.filter(source_row__gt=693).values("sku_id").distinct().count()
        planning_state = official_planning_state(batch)
        planning_state["current_month"] = month_name[planning_state["current_month_number"]]
        current_values = official_current_month_values(batch, sku_ids, planning_state)
        current_month_date = date(planning_state["year"], planning_state["current_month_number"], 1)
        current_snapshots = {
            row.sku_id: row
            for row in MerchandisingMonthlySnapshot.objects.filter(
                batch=batch,
                sku_id__in=sku_ids,
                month=current_month_date,
            )
        }
        prior_ending_by_sku = {
            sku_id: current_values.get(sku_id, {}).get(
                "ending_qty",
                current_snapshots.get(sku_id).ending_qty if current_snapshots.get(sku_id) else Decimal("0"),
            )
            for sku_id in sku_ids
        }
        price_by_sku = {
            sku_id: {
                "cogs": snapshot.cogs_snapshot,
                "retail": snapshot.retail_price_snapshot,
            }
            for sku_id, snapshot in current_snapshots.items()
        }
        future_values, planning_preview = future_planning_values(
            sku_ids=sku_ids,
            planning_year=planning_state["year"],
            current_month_number=planning_state["current_month_number"],
            prior_ending_by_sku=prior_ending_by_sku,
            price_by_sku=price_by_sku,
        )
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
        closed_values = closed_cost_actuals(planning_state["year"], sku_ids)
        closed_month_numbers = sorted({month for _, month in closed_values})
        for closed_month in closed_month_numbers:
            closed_rows = [
                values
                for (sku_id, month_number), values in closed_values.items()
                if month_number == closed_month
            ]
            month_values[closed_month]["incoming_cogs"] = sum(
                (row["incoming_cogs"] for row in closed_rows), Decimal("0")
            )
            month_values[closed_month]["incoming_gross"] = sum(
                (row["incoming_gross"] for row in closed_rows), Decimal("0")
            )
            month_values[closed_month]["ending_cogs"] = sum(
                (row["ending_cogs"] for row in closed_rows), Decimal("0")
            )
        for future_month in range(current_month + 1, 13):
            month_values[future_month] = {
                **month_values[future_month],
                **{field: None for field, _, _ in SUMMARY_METRICS},
            }
            future_rows = [
                values
                for (sku_id, month_number), values in future_values.items()
                if month_number == future_month
            ]
            if future_rows:
                for field, _, _ in SUMMARY_METRICS:
                    present = [row[field] for row in future_rows if row.get(field) is not None]
                    month_values[future_month][field] = (
                        sum(present, Decimal("0")) if present else None
                    )

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
        gpm_total = _sum_present(gpm)
        gpm_gross_total = sum(
            (sales_gross[i] for i in range(12) if gpm[i] is not None and sales_gross[i] is not None),
            Decimal("0"),
        )
        margin_net_total = sum(
            (sales_net[i] for i in range(12) if margin[i] is not None), Decimal("0")
        )
        margin_cogs_total = sum(
            (sales_cogs[i] for i in range(12) if margin[i] is not None), Decimal("0")
        )
        roi_sales_total = sum(
            (sales_gross[i] for i in range(12) if roi[i] is not None), Decimal("0")
        )
        roi_incoming_total = sum(
            (incoming_cogs[i] for i in range(12) if roi[i] is not None), Decimal("0")
        )
        calculated = (
            ("Stock Value Ratio", "ratio2", stock_ratio, _divide(beginning_total, sales_gross_total)),
            ("ITO (YTD)", "ratio2", ito, _last_present(ito)),
            ("GPM", "money", gpm, gpm_total),
            ("GPM Rate", "percent", gpm_rate, _divide(gpm_total, gpm_gross_total)),
            ("Margin Ratio", "ratio2", margin, _divide(margin_net_total, margin_cogs_total)),
            ("Incoming Capital Turnover", "ratio2", roi, _divide(roi_sales_total, roi_incoming_total)),
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
        "planning_preview": planning_preview,
        "closed_month_numbers": closed_month_numbers if batch else [],
        **_filter_options(batch),
    }
    return render(request, "merchandising/dashboard.html", context)


@login_required
def overview(request):
    return dashboard(request)


def _form_error_text(form):
    return " ".join(message for messages_list in form.errors.values() for message in messages_list)


@login_required
def planning_filter_options(request):
    product_status_id = request.GET.get("product_status", "").strip()
    category_id = request.GET.get("category", "").strip()
    subcategory_id = request.GET.get("subcategory", "").strip()
    planning_activity = request.GET.get("planning_activity", "ACTIVE").strip()
    target_month = None
    raw_target_month = request.GET.get("target_month", "").strip()
    if raw_target_month:
        try:
            target_month = date.fromisoformat(f"{raw_target_month}-01")
        except ValueError:
            target_month = None
    if planning_activity not in {"ACTIVE", "INACTIVE", "ALL"}:
        planning_activity = "ACTIVE"
    products = filter_products_by_planning_activity(
        Product.objects.filter(is_active=True),
        planning_activity,
        planning_activity_snapshot(target_month=target_month),
    ).exclude(id__in=drafted_product_ids(target_month))
    if product_status_id and ProductStatus.objects.filter(pk=product_status_id).exists():
        products = products.filter(status_id=product_status_id)

    categories = Category.objects.filter(
        is_active=True,
        products__in=products,
    ).distinct().order_by("name")
    valid_category_ids = {str(value) for value in categories.values_list("id", flat=True)}
    selected_category_valid = bool(category_id and category_id in valid_category_ids)
    if selected_category_valid:
        products = products.filter(category_id=category_id)

    subcategories = Subcategory.objects.filter(
        is_active=True,
        products__in=products,
    ).distinct().order_by("name")
    valid_subcategory_ids = {str(value) for value in subcategories.values_list("id", flat=True)}
    selected_subcategory_valid = bool(
        subcategory_id
        and subcategory_id in valid_subcategory_ids
        and (not category_id or selected_category_valid)
    )
    if selected_subcategory_valid:
        products = products.filter(subcategory_id=subcategory_id)

    return JsonResponse(
        {
            "categories": list(categories.values("id", "name")),
            "subcategories": list(subcategories.values("id", "name")),
            "products": list(products.order_by("name", "code").values("id", "name")),
            "selected_category_valid": selected_category_valid,
            "selected_subcategory_valid": selected_subcategory_valid,
        }
    )


@login_required
def planning_builder(request):
    preview_rows = []
    preview_parent_rows = []
    preview_totals = None
    preview_errors = []
    preview_product_count = 0
    preview_target_month = None
    preview_previous_month = None
    preview_history_months = []
    preview_grain = request.POST.get("preview_grain", "sku") if request.method == "POST" else "sku"
    if preview_grain not in {"sku", "parent_sku"}:
        preview_grain = "sku"
    activity_snapshot = planning_activity_snapshot()
    if request.method == "POST" and request.POST.get("form_name") == "scenario":
        scenario_form = ProjectionScenarioForm(request.POST)
        builder_form = ProjectionBuilderForm()
        if scenario_form.is_valid():
            scenario = scenario_form.save(commit=False)
            scenario.created_by = request.user
            scenario.full_clean()
            scenario.save()
            messages.success(request, "Scenario projection berhasil dibuat.")
            return redirect("merchandising:planning_builder")
    elif request.method == "POST" and request.POST.get("form_name") == "builder":
        if request.POST.get("action") == "cancel":
            messages.info(request, "Preview dibatalkan. Tidak ada Draft Projection yang disimpan.")
            return redirect("merchandising:planning_builder")
        scenario_form = ProjectionScenarioForm()
        builder_form = ProjectionBuilderForm(request.POST)
        if builder_form.is_valid():
            data = builder_form.cleaned_data
            activity_snapshot = planning_activity_snapshot(target_month=data["target_month"])
            preview_target_month = data["target_month"]
            preview_previous_month = previous_month(preview_target_month)
            selected_products = list(data["product"]) if data["scope_type"] == "PRODUCT" else [None]
            scoped_product_status = data["product_status"] if data["scope_type"] == "PRODUCT_STATUS" else None
            scoped_category = data["category"] if data["scope_type"] == "CATEGORY" else None
            rule_data = {
                "scenario": data["scenario"],
                "target_month": data["target_month"],
                "scope_type": data["scope_type"],
                "method": data["method"],
                "parameter": data["parameter"],
                "product_status": scoped_product_status,
                "category": scoped_category,
                "reason": data["reason"],
            }
            preview_data = {key: value for key, value in rule_data.items() if key != "reason"}
            try:
                for product in selected_products:
                    product_rows, product_errors = preview_rule(product=product, **preview_data)
                    preview_rows.extend(product_rows)
                    preview_errors.extend(product_errors)
                for row in preview_rows:
                    sku_id = row["sku"].id
                    row["sales_30d"] = activity_snapshot["sales_by_sku"].get(sku_id, Decimal("0"))
                    row["inbound_30d"] = activity_snapshot["inbound_by_sku"].get(sku_id, Decimal("0"))
                    row["activity_ending_qty"] = activity_snapshot["ending_by_sku"].get(sku_id, Decimal("0"))
                baseline_by_sku = {
                    row["sku"].id: row["baseline_qty"]
                    for row in preview_rows
                    if row.get("baseline_month")
                    and row["baseline_month"].replace(day=1) == preview_previous_month
                }
                preview_history_months, history_by_sku = historical_sales_qty_for_skus(
                    [row["sku"] for row in preview_rows],
                    preview_target_month,
                    scenario=data["scenario"],
                    baseline_by_sku=baseline_by_sku,
                )
                for row in preview_rows:
                    row["history_cells"] = [
                        {
                            "month": month,
                            "value": history_by_sku[row["sku"].id][month],
                            "is_baseline": month == preview_previous_month,
                        }
                        for month in preview_history_months
                    ]
                preview_product_count = len({
                    row["sku"].product_variant.product_id for row in preview_rows
                })
                preview_parent_rows = aggregate_preview_by_parent(preview_rows)
                preview_totals = summarize_preview(preview_rows)
                if request.POST.get("action") in {"apply", "draft"}:
                    if preview_errors:
                        raise ValidationError(preview_errors)
                    adjustments = {}
                    incoming_adjustments = {}
                    for row in preview_rows:
                        field_name = f"projection_qty_{row['sku'].id}"
                        raw_value = request.POST.get(field_name, str(row["recommendation"]))
                        try:
                            adjusted_qty = Decimal(raw_value)
                        except (ArithmeticError, TypeError, ValueError):
                            raise ValidationError(f"{row['sku'].sku}: Sales Projection harus berupa angka.")
                        if adjusted_qty < 0 or adjusted_qty != adjusted_qty.to_integral_value():
                            raise ValidationError(f"{row['sku'].sku}: Sales Projection harus bilangan bulat dan tidak boleh negatif.")
                        adjustments[str(row["sku"].id)] = adjusted_qty
                        minimum_incoming = planning_buffer_incoming(
                            adjusted_qty,
                            row["beginning_qty"],
                            incoming_allowed=row["incoming_allowed"],
                        )
                        incoming_field_name = f"incoming_qty_{row['sku'].id}"
                        incoming_raw_value = request.POST.get(
                            incoming_field_name,
                            str(minimum_incoming),
                        )
                        try:
                            adjusted_incoming = Decimal(incoming_raw_value)
                        except (ArithmeticError, TypeError, ValueError):
                            raise ValidationError(
                                f"{row['sku'].sku}: Incoming Recommendation harus berupa angka."
                            )
                        if (
                            adjusted_incoming < 0
                            or adjusted_incoming != adjusted_incoming.to_integral_value()
                        ):
                            raise ValidationError(
                                f"{row['sku'].sku}: Incoming Recommendation harus bilangan bulat dan tidak boleh negatif."
                            )
                        if adjusted_incoming < minimum_incoming:
                            raise ValidationError(
                                f"{row['sku'].sku}: Incoming Recommendation tidak boleh di bawah minimum {minimum_incoming:.0f}."
                            )
                        incoming_adjustments[str(row["sku"].id)] = adjusted_incoming
                    counts = {"applied": 0, "overridden": 0, "rules": 0}
                    with transaction.atomic():
                        for product in selected_products:
                            _, product_counts = apply_rule(
                                actor=request.user,
                                product=product,
                                adjustments=adjustments,
                                incoming_adjustments=incoming_adjustments,
                                **rule_data,
                            )
                            counts["applied"] += product_counts["applied"]
                            counts["overridden"] += product_counts["overridden"]
                            counts["rules"] += 1
                    messages.success(request, f"Draft Projection disimpan: {counts['rules']} rule untuk {counts['applied']} SKU; {counts['overridden']} SKU tetap memakai rule prioritas lebih tinggi.")
                    draft_url = reverse("merchandising:planning_builder")
                    return redirect(f"{draft_url}?view_draft={data['scenario'].id}#draft-projection")
            except ValidationError as exc:
                builder_form.add_error(None, exc)
    else:
        scenario_form = ProjectionScenarioForm()
        builder_form = ProjectionBuilderForm()
    viewed_draft_scenario = None
    draft_projections = []
    draft_parent_rows = []
    draft_totals = None
    draft_month_options = []
    selected_draft_months = []
    selected_draft_metrics = []
    selected_draft_submetrics = []
    draft_matrix_headers = []
    draft_sku_matrix_rows = []
    draft_parent_matrix_rows = []
    draft_matrix_summary = None
    draft_history_months = []
    draft_history_by_sku = {}
    draft_product_count = 0
    draft_incoming_plans = []
    draft_missing_months = []
    view_draft_id = request.GET.get("view_draft", "").strip()
    if view_draft_id:
        viewed_draft_scenario = ProjectionScenario.objects.filter(pk=view_draft_id).first()
        if viewed_draft_scenario:
            draft_projections = list(SalesProjection.objects.filter(
                scenario=viewed_draft_scenario,
            ).select_related("sku__product_variant__product").order_by(
                "month", "sku__product_variant__product__name", "sku__sku"
            ))
            draft_product_count = len({
                row.sku.product_variant.product_id for row in draft_projections
            })
            draft_incoming_plans = list(IncomingPlan.objects.filter(
                scenario=viewed_draft_scenario,
            ).select_related("sales_projection"))
            draft_sales_target_qty = _latest_sales_target_qty(draft_projections)
            draft_skus = list({row.sku_id: row.sku for row in draft_projections}.values())
            draft_baseline_by_sku = {
                row.sku_id: row.baseline_qty
                for row in draft_projections
                if row.month == viewed_draft_scenario.start_month
                and row.baseline_month
                and row.baseline_month.replace(day=1)
                == previous_month(viewed_draft_scenario.start_month)
            }
            draft_history_months, draft_history_by_sku = historical_sales_qty_for_skus(
                draft_skus,
                viewed_draft_scenario.start_month,
                scenario=viewed_draft_scenario,
                baseline_by_sku=draft_baseline_by_sku,
            )
            draft_parent_rows = aggregate_draft_by_parent(draft_projections)
            draft_totals = summarize_draft(draft_projections, draft_parent_rows)
            month_cursor = viewed_draft_scenario.start_month
            while month_cursor <= viewed_draft_scenario.end_month:
                draft_month_options.append((month_cursor, month_cursor.strftime("%b %Y")))
                month_cursor = next_month(month_cursor)
            valid_months = {month.strftime("%Y-%m"): month for month, _ in draft_month_options}
            selected_draft_months = [
                valid_months[value]
                for value in _getlist(request, "draft_month")
                if value in valid_months
            ]
            if not selected_draft_months:
                selected_draft_months = [month for month, _ in draft_month_options]
            valid_metrics = {value for value, _ in DRAFT_METRIC_OPTIONS}
            selected_draft_metrics = [
                value for value in _getlist(request, "draft_metric") if value in valid_metrics
            ]
            if not selected_draft_metrics:
                selected_draft_metrics = [value for value, _ in DRAFT_METRIC_OPTIONS]
            valid_submetrics = {value for value, _ in DRAFT_SUBMETRIC_OPTIONS}
            selected_draft_submetrics = [
                value
                for value in _getlist(request, "draft_submetric")
                if value in valid_submetrics
            ]
            if not selected_draft_submetrics:
                selected_draft_submetrics = ["qty"]
            draft_sku_matrix_rows, draft_matrix_headers, draft_matrix_summary = build_draft_matrix(
                draft_projections,
                selected_draft_months,
                selected_draft_metrics,
                grain="sku",
                incoming_plans=draft_incoming_plans,
                selected_submetrics=selected_draft_submetrics,
                history_months=draft_history_months,
                history_by_sku=draft_history_by_sku,
                sales_target_by_sku_month=draft_sales_target_qty,
            )
            draft_parent_matrix_rows, _, _ = build_draft_matrix(
                draft_projections,
                selected_draft_months,
                selected_draft_metrics,
                grain="parent_sku",
                incoming_plans=draft_incoming_plans,
                selected_submetrics=selected_draft_submetrics,
                history_months=draft_history_months,
                history_by_sku=draft_history_by_sku,
                sales_target_by_sku_month=draft_sales_target_qty,
            )
            projected_months = {row.month for row in draft_projections}
            draft_missing_months = [
                (month, label)
                for month, label in draft_month_options
                if month not in projected_months
            ]
    return render(
        request,
        "merchandising/planning_builder.html",
        {
            "scenario_form": scenario_form,
            "builder_form": builder_form,
            "preview_rows": preview_rows,
            "preview_parent_rows": preview_parent_rows,
            "preview_totals": preview_totals,
            "preview_errors": preview_errors,
            "preview_product_count": preview_product_count,
            "preview_target_month": preview_target_month,
            "preview_previous_month": preview_previous_month,
            "preview_history_months": preview_history_months,
            "preview_grain": preview_grain,
            "activity_window_start": activity_snapshot["window_start"],
            "activity_as_of_date": activity_snapshot["as_of_date"],
            "activity_prior_month": activity_snapshot["prior_month"],
            "viewed_draft_scenario": viewed_draft_scenario,
            "draft_projections": draft_projections,
            "draft_parent_rows": draft_parent_rows,
            "draft_totals": draft_totals,
            "draft_month_options": draft_month_options,
            "selected_draft_months": selected_draft_months,
            "draft_metric_options": DRAFT_METRIC_OPTIONS,
            "selected_draft_metrics": selected_draft_metrics,
            "draft_submetric_options": DRAFT_SUBMETRIC_OPTIONS,
            "selected_draft_submetrics": selected_draft_submetrics,
            "draft_matrix_headers": draft_matrix_headers,
            "draft_sku_matrix_rows": draft_sku_matrix_rows,
            "draft_parent_matrix_rows": draft_parent_matrix_rows,
            "draft_matrix_summary": draft_matrix_summary,
            "draft_product_count": draft_product_count,
            "draft_missing_months": draft_missing_months,
            "scenarios": ProjectionScenario.objects.annotate(
                projection_count=Count("projections"),
            )[:50],
            "projections": SalesProjection.objects.select_related("scenario", "sku")[:200],
            "incoming_plans": IncomingPlan.objects.select_related("sku", "scenario")[:200],
        },
    )


@login_required
@require_POST
def update_scenario_draft(request, scenario_id):
    scenario = get_object_or_404(ProjectionScenario, pk=scenario_id)
    sales_values = {
        key.removeprefix("sales_qty_"): value
        for key, value in request.POST.items()
        if key.startswith("sales_qty_")
    }
    incoming_values = {
        key.removeprefix("incoming_qty_"): value
        for key, value in request.POST.items()
        if key.startswith("incoming_qty_")
    }
    reason = request.POST.get("reason", "").strip()
    action = request.POST.get("action", "save")
    if action == "save":
        current_sales = {
            str(projection.id): projection.proposed_qty
            for projection in SalesProjection.objects.filter(scenario=scenario)
        }
        current_incoming = {
            str(plan.sales_projection_id): plan.proposed_incoming
            for plan in IncomingPlan.objects.filter(scenario=scenario)
        }

        def changed_values(posted, current):
            changed = {}
            for key, value in posted.items():
                try:
                    unchanged = key in current and Decimal(value) == current[key]
                except (ArithmeticError, TypeError, ValueError):
                    unchanged = False
                if not unchanged:
                    changed[key] = value
            return changed

        sales_values = changed_values(sales_values, current_sales)
        incoming_values = changed_values(incoming_values, current_incoming)
        if not sales_values and not incoming_values:
            messages.info(request, "Tidak ada perubahan pada Scenario Draft.")
            draft_url = reverse("merchandising:planning_builder")
            return redirect(f"{draft_url}?view_draft={scenario.id}#draft-projection")
    try:
        if action == "approve":
            approve_scenario(
                scenario.id,
                request.user,
                sales_values=sales_values,
                incoming_values=incoming_values,
                reason=reason,
            )
            messages.success(
                request,
                "Satu Scenario berhasil di-approve. Seluruh Sales Projection dan Incoming Plan sudah final dan tersinkron ke PPIC.",
            )
        else:
            save_scenario_draft(
                scenario.id,
                request.user,
                sales_values=sales_values,
                incoming_values=incoming_values,
                reason=reason,
            )
            messages.success(
                request,
                "Scenario Draft tersimpan. Penyesuaian Sales dan Incoming tetap dapat diedit sebelum approval.",
            )
    except (ValidationError, ArithmeticError, TypeError, ValueError) as exc:
        messages.error(request, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
    draft_url = reverse("merchandising:planning_builder")
    return redirect(f"{draft_url}?view_draft={scenario.id}#draft-projection")


@login_required
@require_POST
def delete_scenario_draft_items(request, scenario_id):
    scenario = get_object_or_404(ProjectionScenario, pk=scenario_id)
    grain = request.POST.get("selection_grain", "sku")
    identifiers = request.POST.getlist("selected_item")
    try:
        deleted = delete_draft_scenario_items(
            scenario.id,
            request.user,
            grain=grain,
            identifiers=identifiers,
            reason=request.POST.get(
                "reason",
                "Baris dihapus dari Scenario Draft oleh user",
            ).strip(),
        )
        item_label = "Parent SKU" if grain == "parent_sku" else "SKU"
        messages.success(
            request,
            f"{len(identifiers)} {item_label} terpilih berhasil dihapus dari seluruh bulan Scenario Draft "
            f"({deleted['deleted_projection_count']} SKU-bulan).",
        )
    except (ValidationError, TypeError, ValueError) as exc:
        messages.error(request, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
    draft_url = reverse("merchandising:planning_builder")
    return redirect(f"{draft_url}?view_draft={scenario.id}#draft-projection")


@login_required
@require_POST
def revise_scenario(request, scenario_id):
    scenario = get_object_or_404(ProjectionScenario, pk=scenario_id)
    try:
        open_scenario_revision(
            scenario.id,
            request.user,
            request.POST.get("reason", ""),
        )
        messages.success(
            request,
            "Revision Draft dibuka. Angka Approved lama tersimpan di audit trail; simpan perubahan lalu approve ulang.",
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    draft_url = reverse("merchandising:planning_builder")
    return redirect(f"{draft_url}?view_draft={scenario.id}#draft-projection")


@login_required
@require_POST
def edit_scenario(request, scenario_id):
    scenario = get_object_or_404(ProjectionScenario, pk=scenario_id)
    if scenario.status != ProjectionScenario.Status.DRAFT:
        messages.error(request, "Scenario yang sudah Approved dikunci untuk menjaga audit trail.")
        return redirect("merchandising:planning_builder")
    form = ProjectionScenarioForm(request.POST, instance=scenario)
    if form.is_valid():
        try:
            update_draft_scenario(
                scenario.id,
                request.user,
                name=form.cleaned_data["name"],
                start_month=form.cleaned_data["start_month"],
                end_month=form.cleaned_data["end_month"],
            )
            messages.success(request, "Scenario Draft berhasil diperbarui.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        messages.error(request, _form_error_text(form))
    return redirect("merchandising:planning_builder")


@login_required
@require_POST
def delete_scenario(request, scenario_id):
    scenario = get_object_or_404(ProjectionScenario, pk=scenario_id)
    scenario_name = scenario.name
    try:
        delete_draft_scenario(
            scenario.id,
            request.user,
            request.POST.get("reason", "Draft scenario dihapus oleh user").strip(),
        )
        messages.success(
            request,
            f"Scenario Draft {scenario_name} beserta seluruh data draft-nya berhasil dihapus.",
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("merchandising:planning_builder")


@login_required
def projection(request):
    batch = _active_batch()
    filter_options = _filter_options(batch)
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
    current_target_sales = {}
    planning_preview = {"draft_scenario_count": 0, "draft_scenarios": [], "draft_projection_count": 0}
    selected_months = sorted({int(value) for value in _getlist(request, "month") if value.isdigit() and 1 <= int(value) <= 12})
    selected_metrics = [value for value in _getlist(request, "metric") if value in {item[0] for item in PROJECTION_METRIC_GROUPS}]
    selected_submetrics = [value for value in _getlist(request, "submetric") if value in {item[0] for item in PROJECTION_SUBMETRICS}]
    requested_detail_columns = set(_getlist(request, "detail"))
    selected_detail_columns = [
        value
        for value, _ in PROJECTION_DETAIL_COLUMNS
        if value in requested_detail_columns
    ]
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
        snapshots, selected, query, filter_options = _cascading_projection_snapshots(
            request, batch
        )
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
        current_target_sales, current_target_preview = current_month_target_sales(
            sku_ids=sku_ids,
            planning_year=planning_state["year"],
            current_month_number=current_month_number,
        )
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
        identity_value_map = {row["sku_id"]: row for row in identity_rows}
        prior_ending_by_sku = {}
        price_by_sku = {}
        for sku_id in sku_ids:
            prior_ending = current_values.get(sku_id, {}).get("ending_qty")
            if prior_ending is None:
                current_snapshot = by_sku_month.get((sku_id, current_month_number))
                prior_ending = current_snapshot.ending_qty if current_snapshot else Decimal("0")
            identity = identity_value_map[sku_id]
            prior_ending_by_sku[sku_id] = prior_ending
            price_by_sku[sku_id] = {
                "cogs": identity["cogs_snapshot"],
                "retail": identity["retail_price_snapshot"],
            }
        projection_year = planning_state["year"]
        future_values, planning_preview = future_planning_values(
            sku_ids=sku_ids,
            planning_year=planning_state["year"],
            current_month_number=current_month_number,
            prior_ending_by_sku=prior_ending_by_sku,
            price_by_sku=price_by_sku,
        )
        closed_values = closed_cost_actuals(projection_year, sku_ids)
        draft_scenarios = {
            str(row["id"]): row
            for row in [
                *planning_preview["draft_scenarios"],
                *current_target_preview["draft_scenarios"],
            ]
        }
        planning_preview["draft_scenarios"] = list(draft_scenarios.values())
        planning_preview["draft_scenario_count"] = len(draft_scenarios)
        planning_preview["draft_projection_count"] += current_target_preview[
            "draft_projection_count"
        ]
        carryover_rows = IncomingCarryover.objects.filter(
            target_month__gte=current_month_date,
            sku_id__in=sku_ids,
        ).select_related("source_close", "po_line__po", "sku")[:300]
        seen_columns = set()
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
                        metric_group == "sales"
                        and field == "sales_qty"
                        and month_number == current_month_number
                    ):
                        target_key = (
                            projection_year,
                            month_number,
                            "target_sales_qty",
                        )
                        if target_key not in seen_columns:
                            dynamic_headers.append({
                                "year": projection_year,
                                "month_number": month_number,
                                "month": month_abbr[month_number],
                                "metric": "target_sales_qty",
                                "label": "Target Sales QTY",
                                "kind": "number",
                                "is_auto_previous": False,
                            })
                            seen_columns.add(target_key)
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
            if metric == "target_sales_qty":
                if year == projection_year and month_number == current_month_number:
                    return current_target_sales.get(sku_id)
                return None
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
            closed_value = closed_values.get((sku_id, month_number))
            if closed_value and metric in closed_value:
                return closed_value[metric]
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
            "batch": batch,
            "month_options": [(month, month_abbr[month]) for month in range(1, 13)],
            "metric_options": PROJECTION_METRIC_GROUPS,
            "submetric_options": PROJECTION_SUBMETRICS,
            "selected_months": selected_months,
            "selected_metrics": selected_metrics,
            "selected_submetrics": selected_submetrics,
            "detail_column_options": PROJECTION_DETAIL_COLUMNS,
            "selected_detail_columns": selected_detail_columns,
            "identity_summary_colspan": 1 + len(selected_detail_columns),
            "projection_table_colspan": (
                2 + len(selected_detail_columns) + len(dynamic_headers)
            ),
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
            "planning_preview": planning_preview,
            "carryover_rows": carryover_rows,
            "month_close_form": IncomingMonthCloseForm(),
            "month_closes": IncomingMonthClose.objects.prefetch_related("actual_rows", "carryovers")[:12],
            **filter_options,
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
    return redirect("merchandising:planning_builder")


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
    return redirect("merchandising:planning_builder")


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
    return redirect("merchandising:planning_builder")


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
