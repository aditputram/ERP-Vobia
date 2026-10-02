from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from statistics import median
from string import capwords
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, CharField, Count, F, Max, Min, Q, Sum, Value, When
from django.http import HttpResponse, JsonResponse
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from openpyxl import Workbook

from audit.services import record_audit
from accounts.access import module_level
from inventory.models import FIFOOpeningSnapshot, InventoryMovement
from inventory.services.fifo import CUTOVER_DATE
from inventory.services.reporting import inventory_summary_rows
from master_data.models import Category, MarketplaceProductMapping, Product, ProductStatus, SKU, Subcategory
from merchandising.models import MerchandisingMonthlySnapshot
from merchandising.services.planning_activity import (
    filter_products_by_planning_activity,
    planning_activity_snapshot,
)
from merchandising.services.builder import historical_sales_qty_for_skus, official_values_for_skus
from merchandising.services.official_projection import _selling_contexts
from traffic.models import StoreTrafficMetric, TrafficProductMetric

from .forms import ManualSaleHeaderForm, ManualSaleLineFormSet
from .models import SalesOrder, SalesOrderLine, SalesPlan, SalesPlanSKU, SalesPlanningScenario
from .services.manual import create_manual_sales


MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

POTENTIAL_SALES_START_MONTH = date(2026, 7, 1)

SALES_PROJECTION_METHODS = (
    ("INCREASE_PERCENT", "Increase by %"),
    ("DECREASE_PERCENT", "Decrease by %"),
    ("SAME_AS_LAST_MONTH", "Sama dengan Bulan Lalu"),
)

PRODUCT_PERFORMANCE_DIMENSIONS = (
    ("product", "Product"),
    ("month", "Bulan"),
    ("date", "Tanggal"),
    ("source_group", "Source Group"),
    ("source", "Source"),
    ("product_status", "Status Produk"),
    ("category", "Category"),
)

PRODUCT_PERFORMANCE_METRICS = (
    ("qty", "Qty", "number"),
    ("orders", "Order", "number"),
    ("gross", "Gross Sales", "money"),
    ("net", "Net Sales", "money"),
    ("discount", "Discount", "money"),
    ("discount_rate", "Discount Rate", "percent"),
    ("cogs", "COGS", "money"),
    ("gpm", "Gross Profit", "money"),
    ("gpm_rate", "GPM Rate", "percent"),
    ("aov", "AOV", "money"),
    ("avg_price", "Avg. Selling Price", "money"),
    ("str", "STR", "percent"),
)


def _date(value, fallback):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


def _source_group(source):
    return "Marketplace" if source in {"Shopee", "Tiktok"} else "Other"


def _apply_source_filters(queryset, sources, source_groups):
    if sources:
        queryset = queryset.filter(order__source_label__in=sources)
    if source_groups:
        group_filter = Q()
        if "Marketplace" in source_groups:
            group_filter |= Q(order__source__in=["Shopee", "Tiktok"])
        if "Other" in source_groups:
            group_filter |= Q(order__source="Other")
        queryset = queryset.filter(group_filter)
    return queryset


def _source_options(queryset, source_groups=()):
    filtered = _apply_source_filters(queryset, (), source_groups)
    rows = (
        filtered.exclude(order__source_label="")
        .order_by("order__source_label")
        .values("order__source", "order__source_label")
        .distinct()
    )
    return [
        {
            "value": row["order__source_label"],
            "group": _source_group(row["order__source"]),
        }
        for row in rows
    ]


def _shift_month(month, offset):
    month_index = month.year * 12 + month.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _pareto_period_options(earliest, latest):
    months = []
    quarters = []
    semesters = []
    seen_quarters = set()
    seen_semesters = set()
    current = earliest.replace(day=1)
    latest_month = latest.replace(day=1)
    while current <= latest_month:
        months.append({"value": current.strftime("%Y-%m"), "label": date_format(current, "M Y")})
        quarter = (current.month - 1) // 3 + 1
        quarter_key = (current.year, quarter)
        if quarter_key not in seen_quarters:
            quarters.append({"value": f"{current.year}-Q{quarter}", "label": f"Q{quarter} · {current.year}"})
            seen_quarters.add(quarter_key)
        semester = 1 if current.month <= 6 else 2
        semester_key = (current.year, semester)
        if semester_key not in seen_semesters:
            semesters.append({"value": f"{current.year}-S{semester}", "label": f"Semester {semester} · {current.year}"})
            seen_semesters.add(semester_key)
        current = _shift_month(current, 1)
    years = [{"value": str(year), "label": str(year)} for year in range(earliest.year, latest.year + 1)]
    return {"month": months, "quarter": quarters, "semester": semesters, "year": years}


def _pareto_period_bounds(period_type, period_value):
    if period_type == "month":
        start = datetime.strptime(period_value, "%Y-%m").date().replace(day=1)
        return start, _shift_month(start, 1) - timedelta(days=1)
    year = int(period_value[:4])
    if period_type == "quarter":
        quarter = int(period_value[-1])
        start = date(year, (quarter - 1) * 3 + 1, 1)
        return start, _shift_month(start, 3) - timedelta(days=1)
    if period_type == "semester":
        semester = int(period_value[-1])
        start = date(year, 1 if semester == 1 else 7, 1)
        return start, _shift_month(start, 6) - timedelta(days=1)
    return date(year, 1, 1), date(year, 12, 31)


def _line_filters(
    request,
    queryset=None,
    *,
    product_statuses=None,
    categories=None,
    products=None,
):
    qs = queryset if queryset is not None else SalesOrderLine.objects.filter(is_counted=True)
    sources = [item for item in request.GET.getlist("source") if item]
    source_groups = [item for item in request.GET.getlist("source_group") if item]
    if product_statuses is None:
        product_statuses = [item for item in request.GET.getlist("product_status") if item]
    if categories is None:
        categories = [item for item in request.GET.getlist("category") if item]
    subcategory = request.GET.get("subcategory", "")
    if products is None:
        products = [item for item in request.GET.getlist("product") if item]
    qs = _apply_source_filters(qs, sources, source_groups)
    if product_statuses:
        qs = qs.filter(product_status_snapshot__in=product_statuses)
    if categories:
        qs = qs.filter(category_snapshot__in=categories)
    if subcategory:
        qs = qs.filter(subcategory_snapshot=subcategory)
    if products:
        qs = qs.filter(product_name_snapshot__in=products)
    return qs


def _snapshot_values(queryset, field):
    return tuple(
        queryset.exclude(**{field: ""})
        .exclude(**{f"{field}__isnull": True})
        .order_by(field)
        .values_list(field, flat=True)
        .distinct()
    )


def _valid_multi_values(request, name, options):
    allowed = set(options)
    return [value for value in request.GET.getlist(name) if value and value in allowed]


def _excel_text(value):
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _excel_datetime(value):
    if value and timezone.is_aware(value):
        return timezone.localtime(value).replace(tzinfo=None)
    return value


def _export_transactions(lines, start, end):
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Transactions")
    sheet.freeze_panes = "A2"
    headers = (
        "Order Date", "Order Datetime", "Shipped Datetime", "Source", "Order Number",
        "Current Status", "Source Status", "Final", "Import Origin", "Posting",
        "SKU", "Product Status", "Category", "Subcategory", "Product", "Variation",
        "Qty", "Retail Price / Unit", "Net Price / Unit", "Gross Sales", "Net Sales",
        "COGS / Unit", "Total COGS", "GPM", "GPM Rate", "Counted",
    )
    sheet.append(headers)
    row_count = 1
    for line in lines.iterator(chunk_size=2000):
        order = line.order
        sheet.append((
            order.order_date,
            _excel_datetime(order.order_datetime),
            _excel_datetime(order.shipped_datetime),
            _excel_text(order.display_source),
            _excel_text(order.order_number),
            _excel_text(line.current_status),
            _excel_text(line.source_status),
            line.is_final,
            _excel_text(order.get_import_origin_display()),
            "Inventory" if order.affects_inventory else "Report only",
            _excel_text(line.sku_code_snapshot),
            _excel_text(line.product_status_snapshot),
            _excel_text(line.category_snapshot),
            _excel_text(line.subcategory_snapshot),
            _excel_text(line.product_name_snapshot),
            _excel_text(line.variant_name_snapshot),
            line.quantity,
            line.retail_price_snapshot,
            line.net_unit_price,
            line.total_gross_sales,
            line.total_net_sales,
            line.sales_cogs_snapshot,
            line.total_cogs,
            line.gpm,
            line.gpm_rate,
            line.is_counted,
        ))
        row_count += 1
    sheet.auto_filter.ref = f"A1:Z{row_count}"

    output = BytesIO()
    workbook.save(output)
    filename = f"vobia-transactions_{start:%Y-%m-%d}_{end:%Y-%m-%d}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _sales_actuals_by_sku(skus, months):
    sku_ids = [sku.id for sku in skus]
    if not sku_ids or not months:
        return {}
    rows = (
        SalesOrderLine.objects.filter(
            is_counted=True,
            sku_id__in=sku_ids,
            order__order_date__gte=months[0],
            order__order_date__lt=_shift_month(months[-1], 1),
        )
        .annotate(actual_month=TruncMonth("order__order_date"))
        .values("sku_id", "actual_month")
        .annotate(qty=Sum("quantity"), gross=Sum("total_gross_sales"))
    )
    actuals = {}
    for row in rows:
        month = row["actual_month"]
        if isinstance(month, datetime):
            month = month.date()
        actuals[(row["sku_id"], month.replace(day=1))] = {
            "qty": row["qty"] or 0,
            "gross": row["gross"] or Decimal("0"),
        }
    return actuals


def _sales_history_by_sku(skus, target_month, scenario):
    today = timezone.localdate()
    current_month = today.replace(day=1)
    history_months, layered_qty = historical_sales_qty_for_skus(skus, target_month, today=today)
    sku_actuals = _sales_actuals_by_sku(skus, history_months)
    official_values = official_values_for_skus(skus, today) if current_month in history_months else {}
    saved_targets = {
        (target.sku_id, target.plan.month): target
        for target in SalesPlanSKU.objects.filter(
            plan__scenario=scenario,
            plan__month__in=history_months,
            sku_id__in=[sku.id for sku in skus],
        ).select_related("plan")
    }
    histories = {}
    for sku in skus:
        histories[sku.id] = []
        for history_month in history_months:
            saved = saved_targets.get((sku.id, history_month))
            projected = history_month >= current_month
            qty = Decimal(saved.quantity_target) if saved else layered_qty[sku.id][history_month]
            if saved:
                gross = saved.gross_sales_target
            elif history_month == current_month and sku.id in official_values:
                gross = official_values[sku.id]["sales_gross"]
            elif not projected:
                gross = sku_actuals.get((sku.id, history_month), {}).get("gross", Decimal("0"))
            else:
                gross = qty * (sku.current_retail_price or Decimal("0"))
            histories[sku.id].append({
                "month": history_month,
                "qty": qty,
                "gross": gross,
                "is_projection": projected,
            })
    return history_months, histories


def _sales_planning_month(value):
    try:
        return datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except (TypeError, ValueError):
        return None


def _scenario_months(scenario):
    months = []
    month = scenario.start_month
    while month <= scenario.end_month:
        months.append(month)
        month = _shift_month(month, 1)
    return months


def _create_sales_planning_scenario(request):
    name = request.POST.get("name", "").strip()
    start = _sales_planning_month(request.POST.get("start_month"))
    end = _sales_planning_month(request.POST.get("end_month"))
    if not name or not start or not end:
        raise ValidationError("Nama, Mulai, dan Selesai Scenario wajib diisi.")
    scenario = SalesPlanningScenario(
        name=name,
        start_month=start,
        end_month=end,
        created_by=request.user,
    )
    scenario.full_clean()
    scenario.save()
    record_audit(
        actor=request.user,
        action="sales_planning_scenario_created",
        entity_type="sales.salesplanningscenario",
        entity_id=scenario.id,
        after_values={"name": name, "start_month": start.isoformat(), "end_month": end.isoformat()},
    )
    return scenario


def _planned_sales_product_ids(month):
    return SalesPlan.objects.filter(month=month).values_list("product_id", flat=True)


def _selected_sales_planning_products(request, month):
    activity = request.POST.get("planning_activity", "ACTIVE")
    if activity not in {"ACTIVE", "INACTIVE", "ALL"}:
        raise ValidationError("Planning Activity tidak valid.")
    products = filter_products_by_planning_activity(
        Product.objects.filter(is_active=True).select_related("status", "category", "subcategory"),
        activity,
        planning_activity_snapshot(target_month=month),
    ).exclude(id__in=_planned_sales_product_ids(month))
    filters = {
        "product_status": request.POST.get("product_status", ""),
        "category": request.POST.get("category", ""),
        "subcategory": request.POST.get("subcategory", ""),
    }
    for field, value in filters.items():
        if value:
            products = products.filter(**{f"{field if field != 'product_status' else 'status'}_id": value})
    selected_ids = list(dict.fromkeys(value for value in request.POST.getlist("product") if value))
    if selected_ids:
        if SalesPlan.objects.filter(month=month, product_id__in=selected_ids).exists():
            raise ValidationError(
                "Product sudah memiliki Sales Projection pada bulan ini. "
                "Ubah target melalui Scenario Draft yang sudah ada."
            )
        products = products.filter(id__in=selected_ids)
        if products.count() != len(selected_ids):
            raise ValidationError("Product harus sesuai dengan filter yang dipilih.")
    products = list(products.order_by("name", "code"))
    if not products:
        raise ValidationError(
            "Tidak ada Product yang tersedia. Product sesuai filter mungkin sudah "
            "memiliki Sales Projection pada bulan ini."
        )
    return products, activity, filters, selected_ids


def _sales_planning_totals(parent_rows, history_months):
    history = [
        {
            "month": month,
            "qty": sum(row["history"][index]["qty"] for row in parent_rows),
            "gross": sum((row["history"][index]["gross"] for row in parent_rows), Decimal("0")),
        }
        for index, month in enumerate(history_months)
    ]
    qty = sum(row["target_qty"] for row in parent_rows)
    baseline = Decimal(history[-1]["qty"]) if history else Decimal("0")
    return {
        "history": history,
        "qty": qty,
        "gross": sum((row["target_gross"] for row in parent_rows), Decimal("0")),
        "sku_count": sum(row["sku_count"] for row in parent_rows),
        "baseline_qty": baseline,
        "growth_pct": (Decimal(qty) - baseline) / baseline * 100 if baseline else None,
    }


def _parent_target_input_name(parent_sku, month):
    return f"parent_qty_{month:%Y-%m}_{parent_sku}"


def _allocate_parent_quantity(total, weights):
    """Keep legacy SKU rows in sync without making size allocation a Sales input."""
    weights = [max(int(weight or 0), 0) for weight in weights]
    if not weights:
        return []
    weight_total = sum(weights)
    if not weight_total:
        base, remainder = divmod(total, len(weights))
        return [base + (index < remainder) for index in range(len(weights))]
    weighted = [total * weight for weight in weights]
    allocated = [value // weight_total for value in weighted]
    remainder = total - sum(allocated)
    order = sorted(
        range(len(weights)),
        key=lambda index: (weighted[index] % weight_total, -index),
        reverse=True,
    )
    for index in order[:remainder]:
        allocated[index] += 1
    return allocated


def _sales_plan_summary(request):
    targets = SalesPlanSKU.objects.all()
    bounds = targets.aggregate(start=Min("plan__month"), end=Max("plan__month"))
    default_month = _shift_month(timezone.localdate().replace(day=1), 1)
    start = _sales_planning_month(request.GET.get("summary_start")) or bounds["start"] or default_month
    end = _sales_planning_month(request.GET.get("summary_end")) or bounds["end"] or start
    error = ""
    if any(request.GET.get(key) and not _sales_planning_month(request.GET[key]) for key in ("summary_start", "summary_end")):
        error = "Start Month dan End Month harus berupa bulan yang valid."
    elif start > end:
        error = "End Month tidak boleh sebelum Start Month."
    targets = targets.none() if error else targets.filter(plan__month__range=(start, end))
    filters = []
    params = [("summary_start", f"{start:%Y-%m}"), ("summary_end", f"{end:%Y-%m}")]
    for name, label, field, model in (
        ("summary_status", "Product Status", "plan__product__status_id", ProductStatus),
        ("summary_category", "Category", "plan__product__category_id", Category),
        ("summary_subcategory", "Subcategory", "plan__product__subcategory_id", Subcategory),
        ("summary_product", "Product", "plan__product_id", Product),
    ):
        options = [
            {"value": str(row["id"]), "label": row["name"]}
            for row in model.objects.filter(pk__in=targets.values_list(field, flat=True)).order_by("name", "id").values("id", "name")
        ]
        selected = list(dict.fromkeys(_valid_multi_values(request, name, [option["value"] for option in options])))
        filters.append({"name": name, "label": label, "options": options, "selected": selected, "all_label": f"All {label}"})
        if selected:
            targets = targets.filter(**{f"{field}__in": selected})
            params.extend((name, value) for value in selected)
    rows = list(targets.order_by("plan__month").values(month=F("plan__month")).annotate(
        qty=Sum("quantity_target"), gross=Sum("gross_sales_target"),
    ))
    return {
        "start": start, "end": end, "error": error, "filters": filters, "params": params, "rows": rows,
        "qty": sum(row["qty"] for row in rows),
        "gross": sum((row["gross"] for row in rows), Decimal("0")),
    }


def _sales_forecast_matrix(request):
    targets = SalesPlanSKU.objects.all()
    bounds = targets.aggregate(start=Min("plan__month"), end=Max("plan__month"))
    default_month = _shift_month(timezone.localdate().replace(day=1), 1)
    start = _sales_planning_month(request.GET.get("start_month")) or bounds["start"] or default_month
    end = _sales_planning_month(request.GET.get("end_month")) or bounds["end"] or start
    error = ""
    if any(request.GET.get(key) and not _sales_planning_month(request.GET[key]) for key in ("start_month", "end_month")):
        error = "Start Month dan End Month harus berupa bulan yang valid."
    elif start > end:
        error = "End Month tidak boleh sebelum Start Month."
    targets = targets.none() if error else targets.filter(plan__month__range=(start, end))

    scenario_options = list(
        SalesPlanningScenario.objects.filter(projections__sku_targets__in=targets)
        .distinct().order_by("-created_at")
    )
    selected_scenario = request.GET.get("scenario", "")
    if selected_scenario and not any(str(item.id) == selected_scenario for item in scenario_options):
        selected_scenario = ""
    if selected_scenario:
        targets = targets.filter(plan__scenario_id=selected_scenario)

    valid_statuses = {key for key, _label in SalesPlanningScenario.Status.choices}
    selected_status = request.GET.get("scenario_status", "")
    if selected_status not in valid_statuses:
        selected_status = ""
    if selected_status:
        targets = targets.filter(plan__scenario__status=selected_status)

    product_statuses = list(
        targets.exclude(plan__product__status__name="")
        .order_by("plan__product__status__name")
        .values_list("plan__product__status__name", flat=True).distinct()
    )
    selected_product_status = request.GET.get("product_status", "")
    if selected_product_status not in product_statuses:
        selected_product_status = ""
    if selected_product_status:
        targets = targets.filter(plan__product__status__name=selected_product_status)

    categories = list(
        targets.exclude(plan__product__category__name="")
        .order_by("plan__product__category__name")
        .values_list("plan__product__category__name", flat=True).distinct()
    )
    selected_category = request.GET.get("category", "")
    if selected_category not in categories:
        selected_category = ""
    if selected_category:
        targets = targets.filter(plan__product__category__name=selected_category)

    product_options = list(
        targets.order_by("plan__product__name", "plan__product_id")
        .values("plan__product_id", "plan__product__name").distinct()
    )
    selected_product = request.GET.get("product", "")
    if selected_product and not any(str(item["plan__product_id"]) == selected_product for item in product_options):
        selected_product = ""
    if selected_product:
        targets = targets.filter(plan__product_id=selected_product)

    metric_definitions = {
        "qty": {"key": "qty", "label": "Qty", "kind": "number"},
        "gross": {"key": "gross", "label": "Gross Sales", "kind": "money"},
    }
    selected_metric_keys = [
        key for key in ("qty", "gross") if key in request.GET.getlist("metric")
    ] or ["qty", "gross"]
    metrics = [metric_definitions[key] for key in selected_metric_keys]
    aggregated = list(
        targets.values("plan__product_id", "plan__product__name", "plan__month")
        .annotate(qty=Sum("quantity_target"), gross=Sum("gross_sales_target"))
        .order_by("plan__product__name", "plan__month")
    )

    months = []
    month = start
    while month <= end:
        months.append(month)
        month = _shift_month(month, 1)

    def values(qty=0, gross=Decimal("0")):
        raw = {"qty": qty or 0, "gross": gross or Decimal("0")}
        return [{**metric, "value": raw[metric["key"]]} for metric in metrics]

    products = {}
    month_totals = {month: {"qty": 0, "gross": Decimal("0")} for month in months}
    for item in aggregated:
        product = products.setdefault(item["plan__product_id"], {
            "label": item["plan__product__name"],
            "months": {},
            "qty": 0,
            "gross": Decimal("0"),
        })
        product["months"][item["plan__month"]] = item
        product["qty"] += item["qty"] or 0
        product["gross"] += item["gross"] or Decimal("0")
        month_totals[item["plan__month"]]["qty"] += item["qty"] or 0
        month_totals[item["plan__month"]]["gross"] += item["gross"] or Decimal("0")

    rows = []
    for product in products.values():
        cells = []
        for month in months:
            item = product["months"].get(month, {})
            cells.append({"values": values(item.get("qty"), item.get("gross"))})
        rows.append({
            "label": product["label"],
            "cells": cells,
            "total": values(product["qty"], product["gross"]),
        })

    grand_qty = sum(item["qty"] for item in month_totals.values())
    grand_gross = sum((item["gross"] for item in month_totals.values()), Decimal("0"))
    return {
        "start": start,
        "end": end,
        "error": error,
        "scenario_options": scenario_options,
        "selected_scenario": selected_scenario,
        "selected_status": selected_status,
        "product_statuses": product_statuses,
        "selected_product_status": selected_product_status,
        "categories": categories,
        "selected_category": selected_category,
        "product_options": product_options,
        "selected_product": selected_product,
        "metric_options": [
            {**metric_definitions[key], "selected": key in selected_metric_keys}
            for key in ("qty", "gross")
        ],
        "metrics": metrics,
        "months": months,
        "rows": rows,
        "month_totals": [{"values": values(**month_totals[month])} for month in months],
        "grand_total": values(grand_qty, grand_gross),
    }


def _sales_projection_preview(request, scenario, month):
    if scenario.status != SalesPlanningScenario.Status.DRAFT:
        raise ValidationError("Scenario sudah approved dan tidak dapat diubah.")
    if month not in _scenario_months(scenario):
        raise ValidationError("Target Month berada di luar periode Scenario.")

    method = request.POST.get("method", "SAME_AS_LAST_MONTH")
    if method not in {value for value, _ in SALES_PROJECTION_METHODS}:
        raise ValidationError("Method Projection tidak valid.")
    if method == "SAME_AS_LAST_MONTH":
        parameter = Decimal("0")
        factor = Decimal("1")
    else:
        try:
            parameter = Decimal(request.POST.get("parameter") or "0")
        except ArithmeticError as exc:
            raise ValidationError("Parameter harus berupa angka yang valid.") from exc
        if parameter < 0 or (method == "DECREASE_PERCENT" and parameter > 100):
            raise ValidationError("Parameter persentase harus berada dalam rentang yang valid.")
        factor = Decimal("1") + parameter / 100
        if method == "DECREASE_PERCENT":
            factor = Decimal("1") - parameter / 100

    products, activity, filters, selected_ids = _selected_sales_planning_products(request, month)
    skus = list(
        SKU.objects.filter(
            is_active=True,
            product_variant__product_id__in=[product.id for product in products],
        )
        .select_related("product_variant__product")
        .order_by("product_variant__product__name", "sku")
    )
    current_month = timezone.localdate().replace(day=1)
    history_months, histories = _sales_history_by_sku(skus, month, scenario)

    sku_rows = []
    for sku in skus:
        product = sku.product_variant.product
        history = histories[sku.id]
        baseline_qty = Decimal(history[-1]["qty"])
        target_qty = max(0, round(baseline_qty * factor))
        gross_per_qty = Decimal(sku.current_retail_price or Decimal("0"))
        if not gross_per_qty and baseline_qty:
            gross_per_qty = Decimal(history[-1]["gross"]) / baseline_qty
        target_gross = (gross_per_qty * target_qty).quantize(Decimal("1"))
        sku_rows.append({
            "sku": sku,
            "product": product,
            "parent_sku": product.parent_sku or product.code or sku.sku,
            "history": history,
            "baseline_qty": baseline_qty,
            "target_qty": target_qty,
            "target_gross": target_gross,
            "gross_per_qty": gross_per_qty,
            "growth_pct": (
                (Decimal(target_qty) - baseline_qty) / baseline_qty * Decimal("100")
                if baseline_qty else None
            ),
        })

    parent_groups = {}
    for row in sku_rows:
        group = parent_groups.setdefault(row["parent_sku"], {
            "parent_sku": row["parent_sku"],
            "product_names": set(),
            "product_ids": set(),
            "sku_count": 0,
            "history": [
                {"month": history_month, "qty": 0, "gross": Decimal("0")}
                for history_month in history_months
            ],
            "target_qty": 0,
            "target_gross": Decimal("0"),
        })
        group["product_names"].add(row["product"].name)
        group["product_ids"].add(row["product"].id)
        group["sku_count"] += 1
        group["target_qty"] += row["target_qty"]
        group["target_gross"] += row["target_gross"]
        for index, history in enumerate(row["history"]):
            group["history"][index]["qty"] += history["qty"]
            group["history"][index]["gross"] += history["gross"]
    parent_rows = []
    for group in parent_groups.values():
        group["product_name"] = " / ".join(sorted(group.pop("product_names")))
        group["product_count"] = len(group.pop("product_ids"))
        baseline_qty = Decimal(group["history"][-1]["qty"])
        group["baseline_qty"] = baseline_qty
        group["target_input_name"] = _parent_target_input_name(
            group["parent_sku"], month
        )
        group["gross_per_qty"] = (
            group["target_gross"] / Decimal(group["target_qty"])
            if group["target_qty"]
            else Decimal("0")
        )
        group["growth_pct"] = (
            (Decimal(group["target_qty"]) - baseline_qty) / baseline_qty * Decimal("100")
            if baseline_qty else None
        )
        parent_rows.append(group)
    parent_rows.sort(key=lambda row: (row["product_name"], row["parent_sku"]))

    return {
        "rows": sku_rows,
        "parent_rows": parent_rows,
        "sku_rows": sku_rows,
        "totals": _sales_planning_totals(parent_rows, history_months),
        "scenario": scenario,
        "month": month,
        "history_months": history_months,
        "history_headers": [
            {"month": history_month, "is_projection": history_month >= current_month}
            for history_month in history_months
        ],
        "growth_baseline_month": history_months[-1],
        "planning_activity": activity,
        "method": method,
        "method_label": dict(SALES_PROJECTION_METHODS)[method],
        "parameter": parameter,
        "reason": request.POST.get("reason", "").strip(),
        "selected_product_ids": selected_ids,
        **filters,
    }


def _save_sales_projection_preview(request, preview):
    scenario = preview["scenario"]
    submitted_targets = {
        key for key in request.POST if key.startswith("parent_qty_")
    }
    expected_targets = {
        row["target_input_name"] for row in preview["parent_rows"]
    }
    targets = []
    if submitted_targets:
        if submitted_targets != expected_targets:
            raise ValidationError("Pilihan Product sudah berubah. Buat Preview ulang sebelum menyimpan target.")
        rows_by_parent = {}
        for row in preview["sku_rows"]:
            rows_by_parent.setdefault(row["parent_sku"], []).append(row)
        for parent_row in preview["parent_rows"]:
            try:
                parent_qty = int(request.POST.get(parent_row["target_input_name"]))
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"Target Sales Qty {parent_row['parent_sku']} harus berupa angka bulat."
                ) from exc
            if parent_qty < 0:
                raise ValidationError(
                    f"Target Sales Qty {parent_row['parent_sku']} tidak boleh negatif."
                )
            parent_sku_rows = rows_by_parent[parent_row["parent_sku"]]
            allocations = _allocate_parent_quantity(
                parent_qty, [row["target_qty"] for row in parent_sku_rows]
            )
            for row, target_qty in zip(parent_sku_rows, allocations):
                targets.append({
                    **row,
                    "target_qty": target_qty,
                    "target_gross": (row["gross_per_qty"] * target_qty).quantize(Decimal("1")),
                })
    else:
        submitted_skus = {
            key.removeprefix("target_qty_")
            for key in request.POST
            if key.startswith("target_qty_")
        }
        if submitted_skus != {str(row["sku"].id) for row in preview["sku_rows"]}:
            raise ValidationError("Pilihan Product sudah berubah. Buat Preview ulang sebelum menyimpan target.")
        for row in preview["sku_rows"]:
            try:
                target_qty = int(request.POST.get(f"target_qty_{row['sku'].id}"))
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"Target Sales Qty {row['sku'].sku} harus berupa angka bulat."
                ) from exc
            if target_qty < 0:
                raise ValidationError(f"Target Sales Qty {row['sku'].sku} tidak boleh negatif.")
            targets.append({
                **row,
                "target_qty": target_qty,
                "target_gross": (row["gross_per_qty"] * target_qty).quantize(Decimal("1")),
            })

    with transaction.atomic():
        scenario = SalesPlanningScenario.objects.select_for_update().get(pk=scenario.pk)
        if scenario.status != SalesPlanningScenario.Status.DRAFT:
            raise ValidationError("Scenario sudah approved dan tidak dapat diubah.")
        product_targets = {}
        for row in targets:
            product_targets.setdefault(row["product"].id, []).append(row)
        # Serialize claims of the same products across different Sales scenarios.
        list(Product.objects.select_for_update().filter(pk__in=product_targets).order_by("pk"))
        if SalesPlan.objects.filter(month=preview["month"], product_id__in=product_targets).exists():
            raise ValidationError(
                "Product sudah memiliki Sales Projection pada bulan ini. "
                "Ubah target melalui Scenario Draft yang sudah ada."
            )
        for product_rows in product_targets.values():
            product = product_rows[0]["product"]
            plan = SalesPlan(
                scenario=scenario,
                month=preview["month"],
                product=product,
            )
            plan.gross_sales_target = sum((row["target_gross"] for row in product_rows), Decimal("0"))
            plan.quantity_target = sum(row["target_qty"] for row in product_rows)
            plan.full_clean()
            plan.save()
            for row in product_rows:
                target = SalesPlanSKU(
                    plan=plan,
                    sku=row["sku"],
                    gross_sales_target=row["target_gross"],
                    quantity_target=row["target_qty"],
                )
                target.full_clean()
                target.save()
        record_audit(
            actor=request.user,
            action="sales_projection_builder_saved",
            entity_type="sales.salesplan",
            entity_id=scenario.id,
            reason=preview["reason"],
            after_values={
                "scenario": scenario.name,
                "month": preview["month"].isoformat(),
                "parent_skus": sorted({row["parent_sku"] for row in targets}),
                "method": preview["method"],
                "parameter": str(preview["parameter"]),
            },
        )


def _save_sales_projection(request, scenario, month):
    if scenario.status != SalesPlanningScenario.Status.DRAFT:
        raise ValidationError("Scenario sudah approved dan tidak dapat diubah.")
    if month not in _scenario_months(scenario):
        raise ValidationError("Bulan projection berada di luar periode Scenario.")

    months = [_sales_planning_month(value) for value in request.POST.getlist("draft_month")] or [month]
    if any(value not in _scenario_months(scenario) for value in months):
        raise ValidationError("Bulan projection berada di luar periode Scenario.")

    targets = list(
        SalesPlanSKU.objects.filter(plan__scenario=scenario, plan__month__in=months)
        .select_related("plan", "sku", "sku__product_variant__product")
    )
    target_groups = {}
    for target in targets:
        product = target.sku.product_variant.product
        parent_sku = product.parent_sku or product.code or target.sku.sku
        target_groups.setdefault((parent_sku, target.plan.month), []).append(target)
    expected_names = {
        _parent_target_input_name(parent_sku, target_month)
        for parent_sku, target_month in target_groups
    }
    submitted_names = {
        key for key in request.POST if key.startswith("parent_qty_")
    }
    values = []
    if submitted_names:
        if submitted_names != expected_names:
            raise ValidationError("Isi Draft telah berubah. Muat ulang sebelum menyimpan agar target lain tidak tertimpa.")
        grouped_values = []
        for (parent_sku, target_month), grouped_targets in target_groups.items():
            try:
                parent_qty = int(
                    request.POST.get(_parent_target_input_name(parent_sku, target_month))
                )
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ValidationError(
                    f"Target {parent_sku} harus berupa angka yang valid."
                ) from exc
            if parent_qty < 0:
                raise ValidationError(f"Target {parent_sku} tidak boleh negatif.")
            allocations = _allocate_parent_quantity(
                parent_qty, [target.quantity_target for target in grouped_targets]
            )
            grouped_values.extend(zip(grouped_targets, allocations))
    else:
        submitted_ids = {
            key.removeprefix("qty_")
            for key in request.POST
            if key.startswith("qty_")
        }
        if submitted_ids != {str(target.id) for target in targets}:
            raise ValidationError("Isi Draft telah berubah. Muat ulang sebelum menyimpan agar target lain tidak tertimpa.")
        grouped_values = []
        for target in targets:
            try:
                qty = int(request.POST.get(f"qty_{target.id}") or "0")
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ValidationError(
                    f"Target {target.sku.sku} harus berupa angka yang valid."
                ) from exc
            grouped_values.append((target, qty))
    for target, qty in grouped_values:
        if qty < 0:
            raise ValidationError(f"Target {target.sku.sku} tidak boleh negatif.")
        gross = (Decimal(target.sku.current_retail_price or 0) * qty).quantize(Decimal("1"))
        candidate = SalesPlanSKU(
            plan=target.plan,
            sku=target.sku,
            gross_sales_target=gross,
            quantity_target=qty,
        )
        candidate.full_clean(validate_unique=False, validate_constraints=False)
        values.append((target, gross, qty))

    if not values or not any(gross or qty for _, gross, qty in values):
        raise ValidationError("Projection kosong tidak dapat disimpan.")

    with transaction.atomic():
        scenario = SalesPlanningScenario.objects.select_for_update().get(pk=scenario.pk)
        if scenario.status != SalesPlanningScenario.Status.DRAFT:
            raise ValidationError("Scenario sudah approved dan tidak dapat diubah.")
        product_plans = {}
        for target, gross, qty in values:
            target.gross_sales_target = gross
            target.quantity_target = qty
            target.full_clean()
            target.save()
            product_plans[target.plan_id] = target.plan
        for plan in product_plans.values():
            totals = plan.sku_targets.aggregate(gross=Sum("gross_sales_target"), qty=Sum("quantity_target"))
            plan.gross_sales_target = totals["gross"] or Decimal("0")
            plan.quantity_target = totals["qty"] or 0
            plan.full_clean()
            plan.save()
        record_audit(
            actor=request.user,
            action="sales_projection_draft_saved",
            entity_type="sales.salesplan",
            entity_id=scenario.id,
            after_values={
                "scenario": scenario.name,
                "months": [value.isoformat() for value in sorted(set(months))],
                "parent_skus": len(target_groups),
                "gross_sales_target": str(sum((value[1] for value in values), Decimal("0"))),
                "quantity_target": sum(value[2] for value in values),
            },
        )


def _delete_sales_projection_items(request, scenario):
    grain = request.POST.get("selection_grain", "sku")
    identifiers = list(dict.fromkeys(
        value.strip() for value in request.POST.getlist("selected_item") if value.strip()
    ))
    if grain not in {"sku", "parent_sku"}:
        raise ValidationError("Tipe pilihan baris tidak dikenal.")
    if not identifiers:
        raise ValidationError("Pilih minimal satu SKU atau Parent SKU yang akan dihapus.")

    with transaction.atomic():
        scenario = SalesPlanningScenario.objects.select_for_update().get(pk=scenario.pk)
        if scenario.status != SalesPlanningScenario.Status.DRAFT:
            raise ValidationError("Hanya baris dari Scenario yang masih Draft yang dapat dihapus.")
        targets = list(
            SalesPlanSKU.objects.select_for_update()
            .filter(plan__scenario=scenario)
            .select_related("plan", "sku__product_variant__product")
        )
        selected = []
        for target in targets:
            product = target.sku.product_variant.product
            identity = str(target.sku_id) if grain == "sku" else (
                product.parent_sku or product.code or target.sku.sku
            )
            if identity in identifiers:
                selected.append(target)
        if not selected:
            raise ValidationError("Baris terpilih tidak ditemukan pada Scenario Draft ini.")

        plan_ids = {target.plan_id for target in selected}
        snapshot = {
            "scenario": scenario.name,
            "grain": grain,
            "identifiers": identifiers,
            "sku_codes": sorted({target.sku.sku for target in selected}),
            "months": sorted({target.plan.month.isoformat() for target in selected}),
            "target_count": len(selected),
        }
        SalesPlanSKU.objects.filter(id__in=[target.id for target in selected]).delete()
        for plan in SalesPlan.objects.select_for_update().filter(id__in=plan_ids):
            totals = plan.sku_targets.aggregate(
                gross=Sum("gross_sales_target"),
                qty=Sum("quantity_target"),
                count=Count("id"),
            )
            if not totals["count"]:
                plan.delete()
                continue
            plan.gross_sales_target = totals["gross"] or Decimal("0")
            plan.quantity_target = totals["qty"] or 0
            plan.save(update_fields=["gross_sales_target", "quantity_target", "updated_at"])
        record_audit(
            actor=request.user,
            action="sales_projection_draft_items_deleted",
            entity_type="sales.salesplanningscenario",
            entity_id=scenario.id,
            reason="Baris dihapus dari Scenario Draft oleh user",
            before_values=snapshot,
            after_values={"deleted_target_count": len(selected)},
        )
    return grain, identifiers, len(selected)


def _delete_sales_planning_scenario(request, scenario):
    if not (request.user.is_superuser or module_level(request.user, "sales") == "approve"):
        raise PermissionDenied("Hapus Scenario memerlukan akses Approve Sales.")

    with transaction.atomic():
        scenario = SalesPlanningScenario.objects.select_for_update().get(pk=scenario.pk)
        if scenario.status != SalesPlanningScenario.Status.DRAFT:
            raise ValidationError("Hanya Scenario yang masih Draft yang dapat dihapus.")
        snapshot = {
            "name": scenario.name,
            "start_month": scenario.start_month.isoformat(),
            "end_month": scenario.end_month.isoformat(),
            "plan_count": scenario.projections.count(),
            "target_count": SalesPlanSKU.objects.filter(plan__scenario=scenario).count(),
        }
        scenario_id = scenario.id
        SalesPlan.objects.filter(scenario=scenario).delete()
        scenario.delete()
        record_audit(
            actor=request.user,
            action="sales_planning_scenario_draft_deleted",
            entity_type="sales.salesplanningscenario",
            entity_id=scenario_id,
            reason="Draft scenario dihapus oleh user Approve Sales",
            before_values=snapshot,
            after_values={"deleted": True},
        )
    return snapshot


def _approve_sales_planning_scenario(request, scenario):
    if not request.user.has_perm("sales.approve_sales_plan"):
        raise PermissionDenied("User ini tidak memiliki izin approval Sales Planning.")
    with transaction.atomic():
        scenario = SalesPlanningScenario.objects.select_for_update().get(pk=scenario.pk)
        if scenario.status != SalesPlanningScenario.Status.DRAFT:
            raise ValidationError("Scenario sudah approved.")
        targets = SalesPlanSKU.objects.filter(plan__scenario=scenario)
        missing = [
            month for month in _scenario_months(scenario)
            if not targets.filter(plan__month=month).filter(
                Q(gross_sales_target__gt=0)
                | Q(quantity_target__gt=0)
            ).exists()
        ]
        if missing:
            raise ValidationError(
                "Projection belum lengkap untuk: " + ", ".join(month.strftime("%b %Y") for month in missing)
            )
        scenario.status = SalesPlanningScenario.Status.APPROVED
        scenario.approved_by = request.user
        scenario.approved_at = timezone.now()
        scenario.full_clean()
        scenario.save()
        record_audit(
            actor=request.user,
            action="sales_planning_scenario_approved",
            entity_type="sales.salesplanningscenario",
            entity_id=scenario.id,
            after_values={
                "name": scenario.name,
                "start_month": scenario.start_month.isoformat(),
                "end_month": scenario.end_month.isoformat(),
                "projection_count": targets.count(),
            },
        )
    return scenario


@login_required
def planning_filter_options(request):
    activity = request.GET.get("planning_activity", "ACTIVE")
    if activity not in {"ACTIVE", "INACTIVE", "ALL"}:
        activity = "ACTIVE"
    month = _sales_planning_month(request.GET.get("target_month"))
    products = filter_products_by_planning_activity(
        Product.objects.filter(is_active=True),
        activity,
        planning_activity_snapshot(target_month=month),
    ).exclude(id__in=_planned_sales_product_ids(month))
    status_id = request.GET.get("product_status", "")
    category_id = request.GET.get("category", "")
    subcategory_id = request.GET.get("subcategory", "")
    if status_id and ProductStatus.objects.filter(pk=status_id).exists():
        products = products.filter(status_id=status_id)
    categories = Category.objects.filter(is_active=True, products__in=products).distinct().order_by("name")
    if category_id and categories.filter(pk=category_id).exists():
        products = products.filter(category_id=category_id)
    subcategories = Subcategory.objects.filter(is_active=True, products__in=products).distinct().order_by("name")
    if subcategory_id and subcategories.filter(pk=subcategory_id).exists():
        products = products.filter(subcategory_id=subcategory_id)
    return JsonResponse({
        "categories": list(categories.values("id", "name")),
        "subcategories": list(subcategories.values("id", "name")),
        "products": list(products.order_by("name", "code").values("id", "name")),
    })


@login_required
def forecast(request):
    return render(request, "sales/forecast.html", {
        "forecast": _sales_forecast_matrix(request),
    })


@login_required
def potential_sales(request):
    return render(request, "sales/potential_sales.html", {
        "report": _potential_sales_context(request),
    })


@login_required
def forecast_recommendation(request):
    return render(request, "sales/forecast_recommendation.html", {
        "recommendation": _forecast_recommendation_context(request),
    })


@login_required
def planning_builder(request):
    builder_preview = None
    forced_scenario = None
    forced_month = None
    if request.method == "POST":
        form_name = request.POST.get("form_name")
        scenario = None
        month = _sales_planning_month(request.POST.get("month"))
        try:
            if form_name == "scenario":
                scenario = _create_sales_planning_scenario(request)
                month = scenario.start_month
                messages.success(request, f"Scenario {scenario.name} berhasil dibuat.")
            else:
                scenario = get_object_or_404(SalesPlanningScenario, pk=request.POST.get("scenario"))
                if form_name == "delete_selection":
                    grain, identifiers, deleted_count = _delete_sales_projection_items(request, scenario)
                    item_label = "Parent SKU" if grain == "parent_sku" else "SKU"
                    messages.success(
                        request,
                        f"{len(identifiers)} {item_label} terpilih berhasil dihapus dari seluruh bulan "
                        f"Scenario Draft ({deleted_count} SKU-bulan).",
                    )
                elif form_name == "delete_scenario":
                    deleted = _delete_sales_planning_scenario(request, scenario)
                    messages.success(
                        request,
                        f"Scenario Draft {deleted['name']} beserta seluruh targetnya berhasil dihapus.",
                    )
                    scenario = None
                elif form_name == "builder":
                    builder_preview = _sales_projection_preview(request, scenario, month)
                    if request.POST.get("action") == "save":
                        _save_sales_projection_preview(request, builder_preview)
                        messages.success(
                            request,
                            f"Preview {month:%B %Y} tersimpan ke Scenario {scenario.name}.",
                        )
                    elif request.POST.get("action") == "preview":
                        forced_scenario = scenario
                        forced_month = month
                    else:
                        raise ValidationError("Action Projection Builder tidak valid.")
                elif form_name == "projection":
                    _save_sales_projection(request, scenario, month)
                    messages.success(request, f"Projection {month:%B %Y} tersimpan ke Scenario {scenario.name}.")
                elif form_name == "approval":
                    scenario = _approve_sales_planning_scenario(request, scenario)
                    month = month or scenario.start_month
                    messages.success(request, f"Scenario {scenario.name} approved dan seluruh projection dikunci.")
                else:
                    raise ValidationError("Form Sales Planning tidak valid.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        if builder_preview and forced_scenario:
            pass
        elif scenario:
            month = month if month in _scenario_months(scenario) else scenario.start_month
            params = [("scenario", str(scenario.id)), ("month", f"{month:%Y-%m}")]
            for name in ("draft_month", "draft_metric", "draft_grain", "summary_start", "summary_end", "summary_status", "summary_category", "summary_subcategory", "summary_product"):
                params.extend((name, value) for value in request.POST.getlist(name))
            return redirect(f"{reverse('sales:planning_builder')}?{urlencode(params)}#draft-projection")
        else:
            return redirect("sales:planning_builder")

    scenarios = list(
        SalesPlanningScenario.objects.select_related("created_by", "approved_by")
        .annotate(projection_count=Count("projections", distinct=True))
    )
    scenario_id = request.GET.get("scenario")
    viewed_scenario = forced_scenario or (
        get_object_or_404(SalesPlanningScenario, pk=scenario_id)
        if scenario_id else (scenarios[0] if scenarios else None)
    )
    scenario_months = _scenario_months(viewed_scenario) if viewed_scenario else []
    selected_month = forced_month or _sales_planning_month(request.GET.get("month"))
    if selected_month not in scenario_months:
        selected_month = scenario_months[0] if scenario_months else None
    selected_draft_months = sorted({
        month for value in request.GET.getlist("draft_month")
        if (month := _sales_planning_month(value)) in scenario_months
    }) or ([selected_month] if selected_month else [])
    draft_metric_options = [("qty", "Qty"), ("gross", "Gross Sales")]
    selected_draft_metrics = [key for key, _ in draft_metric_options if key in request.GET.getlist("draft_metric")] or ["qty", "gross"]
    targets = list(
        SalesPlanSKU.objects.filter(plan__scenario=viewed_scenario, plan__month__in=selected_draft_months)
        .select_related(
            "plan",
            "sku",
            "sku__product_variant",
            "sku__product_variant__product",
            "sku__product_variant__product__status",
            "sku__product_variant__product__category",
            "sku__product_variant__product__subcategory",
        )
        .order_by("sku__product_variant__product__name", "sku__sku")
    ) if viewed_scenario else []
    skus = list({target.sku_id: target.sku for target in targets}.values())
    target_lookup = {(target.sku_id, target.plan.month): target for target in targets}
    actuals = _sales_actuals_by_sku(skus, selected_draft_months) if skus else {}
    draft_history_months, draft_histories = (
        _sales_history_by_sku(skus, selected_draft_months[0], viewed_scenario)
        if selected_draft_months and skus else ([], {})
    )
    rows = []
    for sku in skus:
        product = sku.product_variant.product
        cells = []
        for month in selected_draft_months:
            target = target_lookup.get((sku.id, month))
            cells.append({"month": month, "target": target,
                          "qty": target.quantity_target if target else None,
                          "gross": target.gross_sales_target if target else None})
        target = next(cell["target"] for cell in cells if cell["target"])
        actual = {
            key: sum((actuals.get((sku.id, month), {}).get(key, 0) for month in selected_draft_months), Decimal("0"))
            for key in ("qty", "gross")
        }
        rows.append({
            "plan": target.plan,
            "target": target,
            "sku": sku,
            "product": product,
            "history": draft_histories.get(sku.id, []),
            "targets": cells,
            "actual": actual,
            "gross_gap": actual["gross"] - target.gross_sales_target,
            "qty_gap": actual["qty"] - target.quantity_target,
        })

    draft_parent_groups = {}
    for row in rows:
        parent_sku = row["product"].parent_sku or row["product"].code or row["sku"].sku
        group = draft_parent_groups.setdefault(parent_sku, {
            "parent_sku": parent_sku,
            "product_names": set(),
            "product_status_ids": set(),
            "category_ids": set(),
            "status_category_pairs": set(),
            "sku_count": 0,
            "target_qty": 0,
            "target_gross": Decimal("0"),
            "actual_qty": 0,
            "actual_gross": Decimal("0"),
            "history": [
                {"month": history_month, "qty": 0, "gross": Decimal("0")}
                for history_month in draft_history_months
            ],
            "targets": [{"month": month, "qty": None, "gross": None} for month in selected_draft_months],
        })
        group["product_names"].add(row["product"].name)
        if row["product"].status_id:
            group["product_status_ids"].add(str(row["product"].status_id))
        if row["product"].category_id:
            group["category_ids"].add(str(row["product"].category_id))
        if row["product"].status_id and row["product"].category_id:
            group["status_category_pairs"].add(
                f'{row["product"].status_id}:{row["product"].category_id}'
            )
        group["sku_count"] += 1
        for index, cell in enumerate(row["targets"]):
            if cell["target"] is not None:
                group["target_qty"] += cell["qty"]
                group["target_gross"] += cell["gross"]
                group["targets"][index]["qty"] = (group["targets"][index]["qty"] or 0) + cell["qty"]
                group["targets"][index]["gross"] = (group["targets"][index]["gross"] or Decimal("0")) + cell["gross"]
        group["actual_qty"] += row["actual"]["qty"]
        group["actual_gross"] += row["actual"]["gross"]
        for index, history in enumerate(row["history"]):
            group["history"][index]["qty"] += history["qty"]
            group["history"][index]["gross"] += history["gross"]
    draft_parent_rows = []
    for group in draft_parent_groups.values():
        group["product_name"] = " / ".join(sorted(group.pop("product_names")))
        group["product_status_ids"] = " ".join(sorted(group["product_status_ids"]))
        group["category_ids"] = " ".join(sorted(group["category_ids"]))
        group["status_category_pairs"] = " ".join(sorted(group["status_category_pairs"]))
        for cell in group["targets"]:
            cell["input_name"] = _parent_target_input_name(
                group["parent_sku"], cell["month"]
            )
            cell["gross_per_qty"] = (
                cell["gross"] / Decimal(cell["qty"])
                if cell["qty"]
                else Decimal("0")
            )
        group["qty_gap"] = group["actual_qty"] - group["target_qty"]
        group["gross_gap"] = group["actual_gross"] - group["target_gross"]
        draft_parent_rows.append(group)
    draft_parent_rows.sort(key=lambda row: row["parent_sku"])

    target_totals = _sales_planning_totals(draft_parent_rows, draft_history_months)
    target_totals["targets"] = [
        {"month": month,
         "qty": sum(row["targets"][index]["qty"] or 0 for row in draft_parent_rows),
         "gross": sum((row["targets"][index]["gross"] or Decimal("0") for row in draft_parent_rows), Decimal("0"))}
        for index, month in enumerate(selected_draft_months)
    ]
    actual_totals = {
        "gross": sum((row["actual"]["gross"] for row in rows), Decimal("0")),
        "qty": sum(row["actual"]["qty"] for row in rows),
    }
    missing_months = []
    if viewed_scenario:
        scenario_targets = SalesPlanSKU.objects.filter(plan__scenario=viewed_scenario)
        missing_months = [
            month for month in scenario_months
            if not scenario_targets.filter(plan__month=month).filter(
                Q(gross_sales_target__gt=0)
                | Q(quantity_target__gt=0)
            ).exists()
        ]
    default_month = _shift_month(timezone.localdate().replace(day=1), 1)
    return render(request, "sales/planning_builder.html", {
        "plan_summary": _sales_plan_summary(request),
        "scenarios": scenarios,
        "viewed_scenario": viewed_scenario,
        "scenario_months": scenario_months,
        "selected_month": selected_month,
        "selected_draft_months": selected_draft_months,
        "draft_metric_options": draft_metric_options,
        "selected_draft_metrics": selected_draft_metrics,
        "show_draft_qty": "qty" in selected_draft_metrics,
        "show_draft_gross": "gross" in selected_draft_metrics,
        "selected_draft_grain": "parent_sku",
        "rows": rows,
        "draft_parent_rows": draft_parent_rows,
        "draft_history_headers": [
            {
                "month": history_month,
                "is_projection": history_month >= timezone.localdate().replace(day=1),
            }
            for history_month in draft_history_months
        ],
        "close_draft": request.GET.get("close_draft") == "1",
        "target_totals": target_totals,
        "actual_totals": actual_totals,
        "gross_gap": actual_totals["gross"] - target_totals["gross"],
        "qty_gap": actual_totals["qty"] - target_totals["qty"],
        "missing_months": missing_months,
        "default_scenario_month": default_month,
        "can_approve": request.user.has_perm("sales.approve_sales_plan"),
        "can_delete_scenario": request.user.is_superuser or module_level(request.user, "sales") == "approve",
        "builder_preview": builder_preview,
        "projection_methods": SALES_PROJECTION_METHODS,
        "builder_planning_activity": builder_preview["planning_activity"] if builder_preview else "ACTIVE",
        "builder_method": builder_preview["method"] if builder_preview else "SAME_AS_LAST_MONTH",
        "builder_parameter": builder_preview["parameter"] if builder_preview else Decimal("0"),
        "builder_reason": builder_preview["reason"] if builder_preview else "",
        "builder_product_status": builder_preview["product_status"] if builder_preview else "",
        "builder_category": builder_preview["category"] if builder_preview else "",
        "builder_subcategory": builder_preview["subcategory"] if builder_preview else "",
        "builder_selected_products": builder_preview["selected_product_ids"] if builder_preview else [],
        "product_statuses": ProductStatus.objects.filter(is_active=True).order_by("name"),
        "categories": Category.objects.filter(is_active=True).order_by("name"),
        "subcategories": Subcategory.objects.filter(is_active=True).select_related("category").order_by("name"),
        "product_options": Product.objects.filter(is_active=True).exclude(
            id__in=_planned_sales_product_ids(selected_month),
        ).order_by("name", "code"),
    })


def _product_performance_filter_state(request):
    lines = SalesOrderLine.objects.filter(is_counted=True)

    product_status_options = _snapshot_values(lines, "product_status_snapshot")
    selected_product_statuses = _valid_multi_values(
        request, "product_status", product_status_options
    )

    category_lines = lines
    if selected_product_statuses:
        category_lines = category_lines.filter(
            product_status_snapshot__in=selected_product_statuses
        )
    category_options = _snapshot_values(category_lines, "category_snapshot")
    selected_categories = _valid_multi_values(request, "category", category_options)

    product_lines = category_lines
    if selected_categories:
        product_lines = product_lines.filter(category_snapshot__in=selected_categories)
    product_options = _snapshot_values(product_lines, "product_name_snapshot")
    selected_products = _valid_multi_values(request, "product", product_options)

    return {
        "product_statuses": product_status_options,
        "categories": category_options,
        "products": product_options,
        "selected_product_statuses": selected_product_statuses,
        "selected_categories": selected_categories,
        "selected_products": selected_products,
    }


def _filter_options():
    lines = SalesOrderLine.objects.filter(is_counted=True)
    return {
        "sources": lines.order_by().values_list("order__source_label", flat=True).distinct(),
        "product_statuses": lines.exclude(product_status_snapshot="").order_by().values_list("product_status_snapshot", flat=True).distinct(),
        "categories": lines.exclude(category_snapshot="").order_by().values_list("category_snapshot", flat=True).distinct(),
        "subcategories": lines.exclude(subcategory_snapshot="").order_by().values_list("subcategory_snapshot", flat=True).distinct(),
        "products": lines.exclude(product_name_snapshot="").order_by().values_list("product_name_snapshot", flat=True).distinct(),
    }


def _traffic_analysis_filter_state(request):
    products = Product.objects.select_related("status", "category").all()
    product_statuses = tuple(
        products.order_by("status__name").values_list("status__name", flat=True).distinct()
    )
    selected_product_statuses = _valid_multi_values(
        request, "product_status", product_statuses
    )
    if selected_product_statuses:
        products = products.filter(status__name__in=selected_product_statuses)

    categories = tuple(
        products.order_by("category__name").values_list("category__name", flat=True).distinct()
    )
    selected_categories = _valid_multi_values(request, "category", categories)
    if selected_categories:
        products = products.filter(category__name__in=selected_categories)

    product_options = tuple(
        products.order_by("name").values_list("name", flat=True).distinct()
    )
    selected_products = _valid_multi_values(request, "product", product_options)
    if selected_products:
        products = products.filter(name__in=selected_products)

    return {
        "products_queryset": products,
        "product_statuses": product_statuses,
        "categories": categories,
        "products": product_options,
        "selected_product_statuses": selected_product_statuses,
        "selected_categories": selected_categories,
        "selected_products": selected_products,
    }


def _totals(qs):
    values = qs.aggregate(
        qty=Sum("quantity"),
        gross=Sum("total_gross_sales"),
        net=Sum("total_net_sales"),
        cogs=Sum("total_cogs"),
        gpm=Sum("gpm"),
        orders=Count("order_id", distinct=True),
    )
    for key in ("qty", "gross", "net", "cogs", "gpm", "orders"):
        values[key] = values[key] or 0
    values["discount"] = values["gross"] - values["net"]
    values["discount_rate"] = values["discount"] / values["gross"] if values["gross"] else None
    values["gpm_rate"] = values["gpm"] / values["gross"] if values["gross"] else None
    values["discount_rate_pct"] = values["discount_rate"] * 100 if values["discount_rate"] is not None else None
    values["gpm_rate_pct"] = values["gpm_rate"] * 100 if values["gpm_rate"] is not None else None
    values["aov"] = values["net"] / values["orders"] if values["orders"] else None
    return values


def _product_performance_pivot(lines, request):
    dimension_labels = dict(PRODUCT_PERFORMANCE_DIMENSIONS)
    row_dimension = request.GET.get("pivot_row", "product")
    column_dimension = request.GET.get("pivot_column", "month")
    if row_dimension not in dimension_labels:
        row_dimension = "product"
    if column_dimension not in dimension_labels:
        column_dimension = "month"
    if row_dimension == column_dimension:
        column_dimension = "month" if row_dimension != "month" else "source_group"

    metric_definitions = {
        key: {"key": key, "label": label, "kind": kind}
        for key, label, kind in PRODUCT_PERFORMANCE_METRICS
    }
    selected_metric_keys = list(dict.fromkeys(
        key for key in request.GET.getlist("metric")
        if key in metric_definitions
    ))
    if not selected_metric_keys:
        selected_metric_keys = ["qty", "gross", "net"]
    selected_metrics = [metric_definitions[key] for key in selected_metric_keys]
    include_str = "str" in selected_metric_keys

    metric_inputs = {
        "qty": {"qty"},
        "orders": set(),
        "gross": {"gross"},
        "net": {"net"},
        "discount": {"gross", "net"},
        "discount_rate": {"gross", "net"},
        "cogs": {"cogs"},
        "gpm": {"gpm"},
        "gpm_rate": {"gpm", "gross"},
        "aov": {"net"},
        "avg_price": {"net", "qty"},
        "str": {"qty"},
    }
    required_sums = set().union(*(metric_inputs[key] for key in selected_metric_keys))
    include_orders = any(key in {"orders", "aov"} for key in selected_metric_keys)

    dimensions = {
        "product": F("product_name_snapshot"),
        "month": TruncMonth("order__order_date"),
        "date": F("order__order_date"),
        "source_group": Case(
            When(order__source__in=[SalesOrder.Source.SHOPEE, SalesOrder.Source.TIKTOK], then=Value("Marketplace")),
            default=Value("Other"),
            output_field=CharField(),
        ),
        "source": Case(
            When(order__source_label="", then=F("order__source")),
            default=F("order__source_label"),
            output_field=CharField(),
        ),
        "product_status": F("product_status_snapshot"),
        "category": F("category_snapshot"),
    }

    def aggregate(*dimension_names, orders=False):
        annotations = {
            f"pivot_{index}": dimensions[name]
            for index, name in enumerate(dimension_names)
        }
        measures = {}
        if "qty" in required_sums:
            measures["qty"] = Sum("quantity")
        if "gross" in required_sums:
            measures["gross"] = Sum("total_gross_sales")
        if "net" in required_sums:
            measures["net"] = Sum("total_net_sales")
        if "cogs" in required_sums:
            measures["cogs"] = Sum("total_cogs")
        if "gpm" in required_sums:
            measures["gpm"] = Sum("gpm")
        if orders:
            measures["orders"] = Count("order_id", distinct=True)
        return list(
            lines.annotate(**annotations)
            .values(*annotations)
            .annotate(**measures)
            .order_by(*annotations)
        )

    cell_rows = aggregate(row_dimension, column_dimension, orders=include_orders)
    row_keys = list(dict.fromkeys(row["pivot_0"] for row in cell_rows))
    column_keys = list(dict.fromkeys(row["pivot_1"] for row in cell_rows))
    cells = {(row["pivot_0"], row["pivot_1"]): row for row in cell_rows}

    def add_sums(target, row):
        for field in required_sums:
            target[field] = target.get(field, 0) + (row.get(field) or 0)

    row_total_rows = {}
    column_total_rows = {}
    grand_totals = {}
    for row in cell_rows:
        add_sums(row_total_rows.setdefault(row["pivot_0"], {}), row)
        add_sums(column_total_rows.setdefault(row["pivot_1"], {}), row)
        add_sums(grand_totals, row)

    if include_orders:
        def order_totals(*dimension_names):
            annotations = {
                f"pivot_{index}": dimensions[name]
                for index, name in enumerate(dimension_names)
            }
            return {
                tuple(row[f"pivot_{index}"] for index in range(len(dimension_names))): row["orders"]
                for row in lines.annotate(**annotations)
                .values(*annotations)
                .annotate(orders=Count("order_id", distinct=True))
            }

        for key, value in order_totals(row_dimension).items():
            row_total_rows.setdefault(key[0], {})["orders"] = value
        for key, value in order_totals(column_dimension).items():
            column_total_rows.setdefault(key[0], {})["orders"] = value
        grand_totals["orders"] = lines.aggregate(
            orders=Count("order_id", distinct=True)
        )["orders"] or 0

    if include_str:
        stock_annotations = {
            "pivot_0": dimensions[row_dimension],
            "pivot_1": dimensions[column_dimension],
            "stock_month": TruncMonth("order__order_date"),
        }
        stock_rows = list(
            lines.filter(sku_id__isnull=False)
            .annotate(**stock_annotations)
            .values("pivot_0", "pivot_1", "sku_id", "stock_month")
            .distinct()
        )
        active_batch_id = (
            MerchandisingMonthlySnapshot.objects.filter(batch__is_active=True)
            .order_by("-batch__imported_at")
            .values_list("batch_id", flat=True)
            .first()
        )
        beginning_by_stock_key = {}
        if active_batch_id and stock_rows:
            beginning_by_stock_key = {
                (row["sku_id"], row["month"]): row["beginning_qty"]
                for row in MerchandisingMonthlySnapshot.objects.filter(
                    batch_id=active_batch_id,
                    sku_id__in={row["sku_id"] for row in stock_rows},
                    month__in={row["stock_month"] for row in stock_rows},
                ).values("sku_id", "month", "beginning_qty")
            }

        beginning_cells = defaultdict(Decimal)
        beginning_rows = defaultdict(Decimal)
        beginning_columns = defaultdict(Decimal)
        beginning_grand = Decimal("0")
        seen_cells = set()
        seen_rows = set()
        seen_columns = set()
        seen_grand = set()
        for row in stock_rows:
            stock_key = (row["sku_id"], row["stock_month"])
            beginning = beginning_by_stock_key.get(stock_key)
            if beginning is None:
                continue
            cell_key = (row["pivot_0"], row["pivot_1"])
            unique_cell = (*cell_key, *stock_key)
            if unique_cell not in seen_cells:
                beginning_cells[cell_key] += beginning
                seen_cells.add(unique_cell)
            unique_row = (row["pivot_0"], *stock_key)
            if unique_row not in seen_rows:
                beginning_rows[row["pivot_0"]] += beginning
                seen_rows.add(unique_row)
            unique_column = (row["pivot_1"], *stock_key)
            if unique_column not in seen_columns:
                beginning_columns[row["pivot_1"]] += beginning
                seen_columns.add(unique_column)
            if stock_key not in seen_grand:
                beginning_grand += beginning
                seen_grand.add(stock_key)
        for key, row in cells.items():
            row["beginning"] = beginning_cells.get(key)
        for key, row in row_total_rows.items():
            row["beginning"] = beginning_rows.get(key)
        for key, row in column_total_rows.items():
            row["beginning"] = beginning_columns.get(key)
        grand_totals["beginning"] = beginning_grand or None

    def metric_values(row):
        qty = row.get("qty") or 0
        orders = row.get("orders") or 0
        gross = row.get("gross") or Decimal("0")
        net = row.get("net") or Decimal("0")
        cogs = row.get("cogs") or Decimal("0")
        gpm = row.get("gpm") or Decimal("0")
        beginning = row.get("beginning")
        values = {
            "qty": qty,
            "orders": orders,
            "gross": gross,
            "net": net,
            "discount": gross - net,
            "discount_rate": (gross - net) / gross * 100 if gross else None,
            "cogs": cogs,
            "gpm": gpm,
            "gpm_rate": gpm / gross * 100 if gross else None,
            "aov": net / orders if orders else None,
            "avg_price": net / qty if qty else None,
            "str": Decimal(qty) / beginning * 100 if beginning else None,
        }
        return [
            {**metric, "value": values[metric["key"]]}
            for metric in selected_metrics
        ]

    def dimension_label(value, dimension):
        if not value:
            return "Tidak diketahui"
        if dimension == "month":
            return date_format(value, "M Y")
        if dimension == "date":
            return date_format(value, "d M Y")
        if dimension == "product":
            return capwords(str(value))
        return str(value)

    return {
        "dimension_options": [
            {"key": key, "label": label}
            for key, label in PRODUCT_PERFORMANCE_DIMENSIONS
        ],
        "row_dimension": row_dimension,
        "row_label": dimension_labels[row_dimension],
        "column_dimension": column_dimension,
        "column_label": dimension_labels[column_dimension],
        "metric_options": [
            {**metric_definitions[key], "selected": key in selected_metric_keys}
            for key, _label, _kind in PRODUCT_PERFORMANCE_METRICS
        ],
        "metrics": selected_metrics,
        "columns": [
            {"key": value, "label": dimension_label(value, column_dimension)}
            for value in column_keys
        ],
        "rows": [
            {
                "label": dimension_label(row_key, row_dimension),
                "cells": [
                    {"values": metric_values(cells.get((row_key, column_key), {}))}
                    for column_key in column_keys
                ],
                "total": metric_values(row_total_rows.get(row_key, {})),
            }
            for row_key in row_keys
        ],
        "column_totals": [
            {"values": metric_values(column_total_rows.get(column_key, {}))}
            for column_key in column_keys
        ],
        "grand_total": metric_values(grand_totals),
    }


def _monthly_gross_chart(lines, monthly_start, monthly_end):
    aggregates = {
        row["month"]: row["gross"] or 0
        for row in lines.annotate(month=TruncMonth("order__order_date"))
        .values("month")
        .annotate(gross=Sum("total_gross_sales"))
        .order_by("month")
    }
    chart = []
    previous_gross = aggregates.get(_shift_month(monthly_start, -1))
    current_month = monthly_start
    while current_month <= monthly_end:
        gross = aggregates.get(current_month, 0)
        growth_pct = None
        if previous_gross not in (None, 0):
            growth_pct = (gross - previous_gross) / previous_gross * Decimal("100")
        chart.append({
            "month": current_month,
            "label": date_format(current_month, "M"),
            "gross": gross,
            "gross_billion": gross / Decimal("1000000000"),
            "growth_pct": growth_pct,
            "growth_class": "positive" if growth_pct is not None and growth_pct > 0 else "negative" if growth_pct is not None and growth_pct < 0 else "neutral",
        })
        previous_gross = gross
        current_month = _shift_month(current_month, 1)
    max_gross = max((row["gross"] for row in chart), default=0)
    for row in chart:
        row["bar"] = float(row["gross"] / max_gross * 100) if max_gross else 0
    return chart


def _dashboard_period_trend(lines, start, end, grain):
    if grain == "month":
        aggregates = {
            row["period"]: row
            for row in lines.annotate(period=TruncMonth("order__order_date"))
            .values("period")
            .annotate(
                qty=Sum("quantity"),
                net=Sum("total_net_sales"),
                gross=Sum("total_gross_sales"),
                orders=Count("order_id", distinct=True),
            )
            .order_by("period")
        }
        rows = []
        current = start.replace(day=1)
        final = end.replace(day=1)
        while current <= final:
            aggregate = aggregates.get(current, {})
            rows.append({
                "month": current,
                "label": date_format(current, "M Y"),
                "qty": aggregate.get("qty") or 0,
                "net": aggregate.get("net") or 0,
                "gross": aggregate.get("gross") or 0,
                "orders": aggregate.get("orders") or 0,
            })
            current = _shift_month(current, 1)
    else:
        aggregates = {
            row["period"]: row
            for row in lines.annotate(period=F("order__order_date"))
            .values("period")
            .annotate(
                qty=Sum("quantity"),
                net=Sum("total_net_sales"),
                gross=Sum("total_gross_sales"),
                orders=Count("order_id", distinct=True),
            )
            .order_by("period")
        }
        rows = []
        current = start
        while current <= end:
            aggregate = aggregates.get(current, {})
            rows.append({
                "day": current,
                "label": date_format(current, "d M Y" if start.year != end.year else "d M"),
                "qty": aggregate.get("qty") or 0,
                "net": aggregate.get("net") or 0,
                "gross": aggregate.get("gross") or 0,
                "orders": aggregate.get("orders") or 0,
            })
            current += timedelta(days=1)

    max_gross = max((row["gross"] for row in rows), default=0)
    for row in rows:
        row["bar"] = float((row["gross"] or 0) / max_gross * 100) if max_gross else 0
    return rows


def _potential_lost_sku_rows(selected_month, cutoff_date, product_ids=None):
    """Estimate lost demand per SKU while keeping stock exceptions visible."""
    if (
        not selected_month
        or not cutoff_date
        or selected_month < POTENTIAL_SALES_START_MONTH
        or cutoff_date < selected_month
    ):
        return []

    month_end = _shift_month(selected_month, 1) - timedelta(days=1)
    cutoff_date = min(cutoff_date, month_end)
    sku_queryset = SKU.objects.filter(
        is_active=True,
        product_variant__is_active=True,
        product_variant__product__is_active=True,
    ).exclude(product_variant__product__status__code__iexact="DISCONTINUE")
    if product_ids is not None:
        sku_queryset = sku_queryset.filter(product_variant__product_id__in=product_ids)
    skus = list(
        sku_queryset.select_related(
            "product_variant__product__status",
            "product_variant__product__category",
        ).order_by("product_variant__product__name", "size", "sku")
    )
    if not skus:
        return []

    sku_ids = [sku.id for sku in skus]
    openings = {
        row["sku_id"]: Decimal(row["opening_qty"] or 0)
        for row in FIFOOpeningSnapshot.objects.filter(sku_id__in=sku_ids).values(
            "sku_id", "opening_qty"
        )
    }
    movement_rows = list(
        InventoryMovement.objects.filter(
            sku_id__in=sku_ids,
            movement_date__range=(CUTOVER_DATE + timedelta(days=1), cutoff_date),
        )
        .exclude(movement_type=InventoryMovement.MovementType.OPENING)
        .exclude(sales_line__order__affects_inventory=False)
        .values("sku_id", "movement_date", "direction")
        .annotate(total=Sum("quantity"))
        .order_by("movement_date")
    )
    daily_movements = defaultdict(
        lambda: defaultdict(lambda: {"in": Decimal("0"), "out": Decimal("0")})
    )
    for movement in movement_rows:
        direction = (
            "in"
            if movement["direction"] == InventoryMovement.Direction.IN
            else "out"
        )
        daily_movements[movement["sku_id"]][movement["movement_date"]][direction] += Decimal(
            movement["total"] or 0
        )

    sales_rows = list(
        SalesOrderLine.objects.filter(
            is_counted=True,
            sku_id__in=sku_ids,
            order__order_date__gte=date(2026, 1, 1),
            order__order_date__lte=cutoff_date,
        )
        .annotate(sales_month=TruncMonth("order__order_date"))
        .values("sku_id", "sales_month")
        .annotate(
            actual_qty=Sum("quantity"),
            actual_gross=Sum("total_gross_sales"),
            first_sale_date=Min("order__order_date"),
            last_sale_date=Max("order__order_date"),
        )
        .order_by("sales_month")
    )
    sales_by_sku_month = {}
    for item in sales_rows:
        sales_month = item["sales_month"]
        if isinstance(sales_month, datetime):
            sales_month = sales_month.date()
        sales_month = sales_month.replace(day=1)
        sales_by_sku_month[(item["sku_id"], sales_month)] = {
            **item,
            "sales_month": sales_month,
            "actual_qty": Decimal(item["actual_qty"] or 0),
            "actual_gross": Decimal(item["actual_gross"] or 0),
        }

    rows = []
    for sku in skus:
        if sku.id not in openings:
            continue
        product = sku.product_variant.product
        selected_actual = sales_by_sku_month.get((sku.id, selected_month), {})
        actual_qty = Decimal(selected_actual.get("actual_qty") or 0)
        actual_gross = Decimal(selected_actual.get("actual_gross") or 0)
        opening_balance = openings[sku.id]
        ending_balance = opening_balance
        available_days = 0
        lost_days = 0
        inventory_exception = opening_balance < 0

        if selected_month == POTENTIAL_SALES_START_MONTH:
            # The FIFO opening is the physical Ending 31 July. July has no daily
            # movement ledger, so a zero ending plus the last historical sale is
            # the auditable stock-out evidence available for that cutover month.
            ending_balance = opening_balance
            last_sale = selected_actual.get("last_sale_date")
            first_sale = selected_actual.get("first_sale_date")
            if ending_balance > 0 or actual_qty <= 0 or not first_sale or not last_sale:
                continue
            available_days = (last_sale - first_sale).days + 1
            lost_days = max((cutoff_date - last_sale).days, 0)
            inventory_exception = ending_balance < 0
        else:
            for movement_date, totals in daily_movements[sku.id].items():
                if movement_date >= selected_month:
                    break
                opening_balance += totals["in"] - totals["out"]
            balance = opening_balance
            cursor = selected_month
            while cursor <= cutoff_date:
                totals = daily_movements[sku.id].get(
                    cursor, {"in": Decimal("0"), "out": Decimal("0")}
                )
                balance_before_out = balance + totals["in"]
                if balance_before_out > 0:
                    available_days += 1
                else:
                    lost_days += 1
                    if totals["out"] > 0:
                        inventory_exception = True
                balance = balance_before_out - totals["out"]
                if balance < 0:
                    inventory_exception = True
                cursor += timedelta(days=1)
            ending_balance = balance
            if not lost_days:
                continue

        reference = selected_actual if actual_qty > 0 and available_days > 0 else None
        if reference is None:
            prior_candidates = [
                item
                for (item_sku_id, item_month), item in sales_by_sku_month.items()
                if item_sku_id == sku.id and item_month < selected_month and item["actual_qty"] > 0
            ]
            reference = max(prior_candidates, key=lambda item: item["sales_month"], default=None)
        if reference is None or lost_days <= 0:
            continue

        if reference is selected_actual:
            selling_days = available_days
        else:
            selling_days = (
                reference["last_sale_date"] - reference["first_sale_date"]
            ).days + 1
        reference_qty = Decimal(reference["actual_qty"] or 0)
        if selling_days <= 0 or reference_qty <= 0:
            continue
        daily_rate = reference_qty / Decimal(selling_days)
        lost_qty = (daily_rate * Decimal(lost_days)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        if lost_qty <= 0:
            continue
        reference_gross = Decimal(reference["actual_gross"] or 0)
        unit_gross = (
            reference_gross / reference_qty
            if reference_gross > 0
            else Decimal(sku.current_retail_price or 0)
        )
        lost_gross = (unit_gross * lost_qty).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        rows.append({
            "product_id": product.id,
            "product": product,
            "article": product.article or product.name,
            "sku": sku,
            "size": sku.size or "—",
            "reference_month": reference["sales_month"],
            "reference_start": reference["first_sale_date"],
            "reference_end": reference["last_sale_date"],
            "selling_days": selling_days,
            "daily_rate": daily_rate,
            "lost_days": lost_days,
            "actual_qty": actual_qty,
            "actual_gross": actual_gross,
            "lost_qty": lost_qty,
            "lost_gross": lost_gross,
            "potential_qty": actual_qty + lost_qty,
            "potential_gross": actual_gross + lost_gross,
            "opening_balance": opening_balance,
            "ending_balance": ending_balance,
            "inventory_exception": inventory_exception,
        })
    return sorted(
        rows,
        key=lambda row: (-row["lost_qty"], row["product"].name.casefold(), row["sku"].sku),
    )


def _potential_sales_context(request):
    latest = SalesOrderLine.objects.filter(
        is_counted=True,
        sku__isnull=False,
    ).aggregate(latest=Max("order__order_date"))["latest"]
    latest = latest or POTENTIAL_SALES_START_MONTH
    latest_month = max(latest.replace(day=1), POTENTIAL_SALES_START_MONTH)
    month_options = []
    month = POTENTIAL_SALES_START_MONTH
    while month <= latest_month:
        month_options.append({
            "value": month.strftime("%Y-%m"),
            "label": date_format(month, "F Y"),
        })
        month = _shift_month(month, 1)
    selected_value = request.GET.get("month", latest_month.strftime("%Y-%m"))
    allowed_values = {item["value"] for item in month_options}
    if selected_value not in allowed_values:
        selected_value = month_options[-1]["value"]
    selected_month = datetime.strptime(selected_value, "%Y-%m").date()
    cutoff = min(latest, _shift_month(selected_month, 1) - timedelta(days=1))
    affected_rows = _potential_lost_sku_rows(selected_month, cutoff)
    affected_by_sku = {row["sku"].id: row for row in affected_rows}
    affected_product_ids = {row["product_id"] for row in affected_rows}
    history_months = [_shift_month(selected_month, offset) for offset in (-3, -2, -1)]
    skus = list(
        SKU.objects.filter(
            is_active=True,
            product_variant__is_active=True,
            product_variant__product_id__in=affected_product_ids,
        )
        .select_related(
            "product_variant__product__status",
            "product_variant__product__category",
        )
        .order_by("product_variant__product__name", "size", "sku")
    )
    sku_ids = [sku.id for sku in skus]
    sales_by_sku_month = {}
    if sku_ids:
        sales_rows = (
            SalesOrderLine.objects.filter(
                is_counted=True,
                sku_id__in=sku_ids,
                order__order_date__range=(history_months[0], cutoff),
            )
            .annotate(sales_month=TruncMonth("order__order_date"))
            .values("sku_id", "sales_month")
            .annotate(
                qty=Sum("quantity"),
                gross=Sum("total_gross_sales"),
            )
        )
        for item in sales_rows:
            sales_month = item["sales_month"]
            if isinstance(sales_month, datetime):
                sales_month = sales_month.date()
            sales_by_sku_month[(item["sku_id"], sales_month.replace(day=1))] = item

    active_batch_id = (
        MerchandisingMonthlySnapshot.objects.filter(batch__is_active=True)
        .order_by("-batch__imported_at")
        .values_list("batch_id", flat=True)
        .first()
    )
    beginning_by_sku = {}
    if active_batch_id and sku_ids:
        beginning_by_sku = {
            row["sku_id"]: row["beginning_qty"]
            for row in MerchandisingMonthlySnapshot.objects.filter(
                batch_id=active_batch_id,
                sku_id__in=sku_ids,
                month=selected_month,
            ).values("sku_id", "beginning_qty")
        }

    products = {}
    for sku in skus:
        product = sku.product_variant.product
        selected_sales = sales_by_sku_month.get((sku.id, selected_month), {})
        actual_qty = Decimal(selected_sales.get("qty") or 0)
        actual_gross = Decimal(selected_sales.get("gross") or 0)
        beginning_qty = beginning_by_sku.get(sku.id)
        affected = affected_by_sku.get(sku.id)
        lost_qty = affected["lost_qty"] if affected else Decimal("0")
        lost_gross = affected["lost_gross"] if affected else Decimal("0")
        size_row = {
            "sku": sku,
            "size": sku.size or "—",
            "affected": bool(affected),
            "history_cells": [
                {
                    "month": history_month,
                    "qty": Decimal(
                        sales_by_sku_month.get((sku.id, history_month), {}).get("qty") or 0
                    ),
                }
                for history_month in history_months
            ],
            "beginning_qty": beginning_qty,
            "str": (
                actual_qty / Decimal(beginning_qty) * 100
                if beginning_qty is not None and beginning_qty > 0
                else None
            ),
            "actual_qty": actual_qty,
            "actual_gross": actual_gross,
            "lost_days": affected["lost_days"] if affected else 0,
            "lost_qty": lost_qty,
            "lost_gross": lost_gross,
            "potential_qty": actual_qty + lost_qty,
            "potential_gross": actual_gross + lost_gross,
            "reference_month": affected["reference_month"] if affected else None,
            "reference_start": affected["reference_start"] if affected else None,
            "reference_end": affected["reference_end"] if affected else None,
            "selling_days": affected["selling_days"] if affected else None,
            "daily_rate": affected["daily_rate"] if affected else None,
            "inventory_exception": affected["inventory_exception"] if affected else False,
        }
        group = products.setdefault(product.id, {
            "product_id": product.id,
            "product": product,
            "article": product.article or product.name,
            "sizes": [],
            "affected_sizes": [],
            "actual_qty": Decimal("0"),
            "actual_gross": Decimal("0"),
            "lost_qty": Decimal("0"),
            "lost_gross": Decimal("0"),
            "potential_qty": Decimal("0"),
            "potential_gross": Decimal("0"),
        })
        group["sizes"].append(size_row)
        if size_row["affected"]:
            group["affected_sizes"].append(size_row["size"])
        for field in (
            "actual_qty", "actual_gross", "lost_qty", "lost_gross",
            "potential_qty", "potential_gross",
        ):
            group[field] += size_row[field]
    product_rows = sorted(products.values(), key=lambda row: row["product"].name.casefold())
    return {
        "month_options": month_options,
        "selected_value": selected_value,
        "selected_month": selected_month,
        "cutoff": cutoff,
        "history_months": history_months,
        "rows": affected_rows,
        "products": product_rows,
        "sku_count": len(affected_rows),
        "displayed_sku_count": len(skus),
        "product_count": len(product_rows),
        "actual_qty": sum((row["actual_qty"] for row in product_rows), Decimal("0")),
        "lost_qty": sum((row["lost_qty"] for row in product_rows), Decimal("0")),
        "potential_qty": sum((row["potential_qty"] for row in product_rows), Decimal("0")),
        "actual_gross": sum((row["actual_gross"] for row in product_rows), Decimal("0")),
        "lost_gross": sum((row["lost_gross"] for row in product_rows), Decimal("0")),
        "potential_gross": sum((row["potential_gross"] for row in product_rows), Decimal("0")),
        "exception_count": sum(1 for row in affected_rows if row["inventory_exception"]),
    }


def _potential_sales_rows(cutoff_date, selected_month, product_ids=None):
    """Estimate monthly demand through the cutoff for physically sold-out products."""
    if not cutoff_date or cutoff_date <= CUTOVER_DATE:
        return []
    tracking_start = CUTOVER_DATE + timedelta(days=1)
    month_lines = SalesOrderLine.objects.filter(
        is_counted=True,
        sku__isnull=False,
        order__order_date__range=(tracking_start, cutoff_date),
    ).exclude(sku__product_variant__product__status__code__iexact="DISCONTINUE")
    if product_ids is not None:
        month_lines = month_lines.filter(
            sku__product_variant__product_id__in=product_ids
        )
    monthly_actuals = list(
        month_lines.annotate(sales_month=TruncMonth("order__order_date")).values(
            product_id=F("sku__product_variant__product_id"),
            article=F("sku__product_variant__product__article"),
            product_name=F("sku__product_variant__product__name"),
            sales_month=F("sales_month"),
        ).annotate(
            actual_qty=Sum("quantity"),
            actual_gross=Sum("total_gross_sales"),
            sold_out_date=Max("order__order_date"),
        ).order_by("sales_month")
    )
    actuals_by_product = {row["product_id"]: row for row in monthly_actuals}
    actuals = list(actuals_by_product.values())
    if not actuals:
        return []

    product_ids = {row["product_id"] for row in actuals}
    skus = list(
        SKU.objects.filter(
            is_active=True,
            product_variant__product_id__in=product_ids,
        ).select_related("product_variant__product__status")
    )
    balances_by_product = {}
    for row in inventory_summary_rows(skus, as_of_date=cutoff_date):
        product_id = row["sku"].product_variant.product_id
        balances_by_product.setdefault(product_id, []).append(row["balance"])
    inventory_days = list(
        InventoryMovement.objects.filter(
            sku__product_variant__product_id__in=product_ids,
            movement_date__range=(tracking_start, cutoff_date),
        )
        .exclude(movement_type=InventoryMovement.MovementType.OPENING)
        .exclude(sales_line__order__affects_inventory=False)
        .values("sku__product_variant__product_id", "movement_date")
        .annotate(
            sales_out_qty=Sum(
                "quantity",
                filter=Q(movement_type=InventoryMovement.MovementType.SALES_OUT),
            )
        )
    )
    latest_inventory_date = {}
    sales_out_days = set()
    for row in inventory_days:
        product_id = row["sku__product_variant__product_id"]
        movement_date = row["movement_date"]
        latest_inventory_date[product_id] = max(
            latest_inventory_date.get(product_id, movement_date), movement_date
        )
        if row["sales_out_qty"]:
            sales_out_days.add((product_id, movement_date))

    first_sales = {
        (row["sales_month"], row["sku_id"]): row["first_sale_date"]
        for row in month_lines.annotate(sales_month=TruncMonth("order__order_date"))
        .values("sales_month", "sku_id")
        .annotate(
            first_sale_date=Min("order__order_date")
        )
    }
    skus_by_sales_month = {}
    for sku in skus:
        sales_month = actuals_by_product[sku.product_variant.product_id]["sales_month"]
        skus_by_sales_month.setdefault(sales_month, []).append(sku)

    selling_contexts = {}
    for sales_month, month_skus in skus_by_sales_month.items():
        month_end = min(cutoff_date, _shift_month(sales_month, 1) - timedelta(days=1))
        selling_contexts.update(_selling_contexts(
            month_skus,
            sales_month.year,
            sales_month.month,
            month_end,
            {
                sku.id: first_sales[(sales_month, sku.id)]
                for sku in month_skus
                if (sales_month, sku.id) in first_sales
            },
        ))
    starts_by_product = {}
    for sku in skus:
        start_date = selling_contexts.get(sku.id, {}).get("selling_start_date")
        if start_date:
            starts_by_product.setdefault(sku.product_variant.product_id, []).append(start_date)

    rows = []
    for actual in actuals:
        balances = balances_by_product.get(actual["product_id"], [])
        start_dates = starts_by_product.get(actual["product_id"], [])
        sold_out_date = actual["sold_out_date"]
        if (
            not balances
            or any(balance != 0 for balance in balances)
            or not start_dates
            or sold_out_date >= cutoff_date
            or latest_inventory_date.get(actual["product_id"]) != sold_out_date
            or (actual["product_id"], sold_out_date) not in sales_out_days
        ):
            continue
        selling_start = min(start_dates)
        selling_days = (sold_out_date - selling_start).days + 1
        actual_qty = Decimal(actual["actual_qty"] or 0)
        if selling_days <= 0 or actual_qty <= 0:
            continue
        lost_start = max(selected_month, sold_out_date + timedelta(days=1))
        lost_days = max((cutoff_date - lost_start).days + 1, 0)
        if not lost_days:
            continue
        monthly_actual_qty = actual_qty if actual["sales_month"] == selected_month else Decimal("0")
        lost_qty = (
            actual_qty / Decimal(selling_days) * Decimal(lost_days)
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        potential_qty = monthly_actual_qty + lost_qty
        actual_gross = Decimal(actual["actual_gross"] or 0)
        monthly_actual_gross = actual_gross if actual["sales_month"] == selected_month else Decimal("0")
        lost_gross = (
            actual_gross / actual_qty * lost_qty
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        potential_gross = monthly_actual_gross + lost_gross
        if lost_qty <= 0:
            continue
        rows.append({
            **actual,
            "article": actual["article"] or actual["product_name"],
            "selling_start": selling_start,
            "selling_days": selling_days,
            "selling_reference": actual["sales_month"] != selected_month,
            "actual_qty": monthly_actual_qty,
            "actual_gross": monthly_actual_gross,
            "potential_qty": potential_qty,
            "lost_qty": lost_qty,
            "potential_gross": potential_gross,
            "lost_gross": lost_gross,
        })
    return sorted(rows, key=lambda row: (-row["lost_qty"], row["article"].casefold()))


def _conservative_forecast(demand_values):
    """Apply the agreed percentage trend to three monthly demand values."""
    values = [Decimal(value or 0) for value in demand_values]
    if len(values) != 3 or values[0] <= 0 or values[1] <= 0:
        return None, [None, None]
    changes = [
        (values[1] - values[0]) / values[0],
        (values[2] - values[1]) / values[1],
    ]
    trend = median(changes)
    forecast = (values[2] * (Decimal("1") + trend)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return max(forecast, Decimal("0")), changes


def _percent_text(value):
    if value is None:
        return "—"
    return f"{value * Decimal('100'):+.2f}%".replace(".", ",")


def _forecast_potential_lost_by_month(history_months, product_ids, monthly_actuals):
    """Calculate completed-month lost demand with one shared inventory read."""
    tracking_start = CUTOVER_DATE + timedelta(days=1)
    potential_months = [
        month for month in history_months
        if _shift_month(month, 1) - timedelta(days=1) > CUTOVER_DATE
    ]
    if not potential_months:
        return {}

    product_ids = set(product_ids)
    actuals_by_product = defaultdict(list)
    for item in monthly_actuals:
        month = item["sales_month"]
        if isinstance(month, datetime):
            month = month.date()
        month = month.replace(day=1)
        if item["product_id"] in product_ids and month >= tracking_start.replace(day=1):
            actuals_by_product[item["product_id"]].append({**item, "sales_month": month})
    if not actuals_by_product:
        return {}

    skus = list(
        SKU.objects.filter(
            is_active=True,
            product_variant__product_id__in=actuals_by_product,
        ).select_related("product_variant__product")
    )
    if not skus:
        return {}
    sku_ids = [sku.id for sku in skus]
    product_by_sku = {sku.id: sku.product_variant.product_id for sku in skus}
    max_cutoff = _shift_month(potential_months[-1], 1) - timedelta(days=1)

    openings = {
        row["sku_id"]: Decimal(row["opening_qty"] or 0)
        for row in FIFOOpeningSnapshot.objects.filter(
            sku_id__in=sku_ids,
            cutover_date__lte=max_cutoff,
        ).values("sku_id", "opening_qty")
    }
    movement_rows = list(
        InventoryMovement.objects.filter(
            sku_id__in=sku_ids,
            movement_date__lte=max_cutoff,
        )
        .exclude(movement_type=InventoryMovement.MovementType.OPENING)
        .exclude(sales_line__order__affects_inventory=False)
        .values("sku_id", "movement_date", "direction", "movement_type")
        .annotate(total=Sum("quantity"))
        .order_by("movement_date")
    )
    daily_by_sku = defaultdict(lambda: defaultdict(lambda: {
        "in": Decimal("0"),
        "out": Decimal("0"),
    }))
    known_skus = set(openings)
    for movement in movement_rows:
        sku_id = movement["sku_id"]
        known_skus.add(sku_id)
        day = daily_by_sku[sku_id][movement["movement_date"]]
        direction = "in" if movement["direction"] == InventoryMovement.Direction.IN else "out"
        day[direction] += Decimal(movement["total"] or 0)

    first_sales = {}
    first_sale_rows = (
        SalesOrderLine.objects.filter(
            is_counted=True,
            sku_id__in=sku_ids,
            order__order_date__range=(tracking_start, max_cutoff),
        )
        .annotate(sales_month=TruncMonth("order__order_date"))
        .values("sales_month", "sku_id")
        .annotate(first_sale_date=Min("order__order_date"))
    )
    for row in first_sale_rows:
        month = row["sales_month"]
        if isinstance(month, datetime):
            month = month.date()
        first_sales[(month.replace(day=1), row["sku_id"])] = row["first_sale_date"]

    def balance_through(sku_id, cutoff):
        balance = openings.get(sku_id, Decimal("0"))
        for movement_date, totals in daily_by_sku[sku_id].items():
            if movement_date > cutoff:
                break
            balance += totals["in"] - totals["out"]
        return balance

    reference_months = {
        item["sales_month"]
        for items in actuals_by_product.values()
        for item in items
        if item["sales_month"] <= potential_months[-1]
    }
    selling_starts = {}
    for month in reference_months:
        cutoff = _shift_month(month, 1) - timedelta(days=1)
        for sku in skus:
            balance = openings.get(sku.id, Decimal("0"))
            for movement_date, totals in daily_by_sku[sku.id].items():
                if movement_date >= month:
                    break
                balance += totals["in"] - totals["out"]
            if sku.id not in known_skus or balance > 0:
                start_date = month
            else:
                start_date = None
                for movement_date, totals in daily_by_sku[sku.id].items():
                    if movement_date < month:
                        continue
                    if movement_date > cutoff:
                        break
                    balance += totals["in"]
                    if balance > 0:
                        start_date = movement_date
                        break
                    balance -= totals["out"]
            first_sale = first_sales.get((month, sku.id))
            if first_sale and (not start_date or first_sale < start_date):
                start_date = first_sale
            selling_starts[(sku.id, month)] = start_date

    lost_by_product_month = {}
    for selected_month in potential_months:
        cutoff = _shift_month(selected_month, 1) - timedelta(days=1)
        balances_by_product = defaultdict(list)
        starts_by_product_month = defaultdict(list)
        latest_inventory_date = {}
        sales_out_days = set()
        for sku in skus:
            product_id = product_by_sku[sku.id]
            balances_by_product[product_id].append(balance_through(sku.id, cutoff))
        for movement in movement_rows:
            movement_date = movement["movement_date"]
            if movement_date < tracking_start:
                continue
            if movement_date > cutoff:
                break
            product_id = product_by_sku[movement["sku_id"]]
            latest_inventory_date[product_id] = max(
                latest_inventory_date.get(product_id, movement_date), movement_date
            )
            if (
                movement["movement_type"] == InventoryMovement.MovementType.SALES_OUT
                and movement["total"]
            ):
                sales_out_days.add((product_id, movement_date))

        for product_id, items in actuals_by_product.items():
            eligible = [item for item in items if item["sales_month"] <= selected_month]
            if not eligible:
                continue
            actual = max(eligible, key=lambda item: item["sales_month"])
            reference_month = actual["sales_month"]
            for sku in skus:
                if product_by_sku[sku.id] != product_id:
                    continue
                start_date = selling_starts.get((sku.id, reference_month))
                if start_date:
                    starts_by_product_month[(product_id, reference_month)].append(start_date)
            balances = balances_by_product.get(product_id, [])
            starts = starts_by_product_month.get((product_id, reference_month), [])
            sold_out_date = actual["sold_out_date"]
            if (
                not balances
                or any(balance != 0 for balance in balances)
                or not starts
                or sold_out_date >= cutoff
                or latest_inventory_date.get(product_id) != sold_out_date
                or (product_id, sold_out_date) not in sales_out_days
            ):
                continue
            selling_days = (sold_out_date - min(starts)).days + 1
            actual_qty = Decimal(actual["actual_qty"] or 0)
            lost_start = max(selected_month, sold_out_date + timedelta(days=1))
            lost_days = max((cutoff - lost_start).days + 1, 0)
            if selling_days <= 0 or actual_qty <= 0 or not lost_days:
                continue
            lost_qty = (
                actual_qty / Decimal(selling_days) * Decimal(lost_days)
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if lost_qty > 0:
                lost_by_product_month[(product_id, selected_month)] = lost_qty
    return lost_by_product_month


def _forecast_recommendation_context(request):
    today = timezone.localdate()
    current_month = today.replace(day=1)
    target_value = request.GET.get("target_month", "")
    target_month = _sales_planning_month(target_value) or current_month
    error = ""
    if target_value and not _sales_planning_month(target_value):
        error = "Target Month harus berupa bulan yang valid."

    forecastable_products = Product.objects.filter(is_active=True).filter(
        Q(status__name__iexact="Regular")
        | Q(status__name__iexact="Seasonal Regular")
    ).select_related("status", "category")
    status_options = list(
        forecastable_products.order_by("status__name")
        .values_list("status__name", flat=True).distinct()
    )
    selected_status = request.GET.get("product_status", "")
    if selected_status not in status_options:
        selected_status = ""
    if selected_status:
        forecastable_products = forecastable_products.filter(status__name=selected_status)

    category_options = list(
        Category.objects.filter(products__in=forecastable_products)
        .distinct().order_by("name")
    )
    selected_category = request.GET.get("category", "")
    if selected_category and not any(
        str(category.id) == selected_category for category in category_options
    ):
        selected_category = ""
    if selected_category:
        forecastable_products = forecastable_products.filter(category_id=selected_category)

    product_options = list(forecastable_products.order_by("name", "code"))
    selected_product = request.GET.get("product", "")
    if selected_product and not any(
        str(product.id) == selected_product for product in product_options
    ):
        selected_product = ""
    if selected_product:
        forecastable_products = forecastable_products.filter(pk=selected_product)

    products = list(forecastable_products.order_by("name", "code"))
    product_ids = [product.id for product in products]
    history_months = [_shift_month(target_month, offset) for offset in (-3, -2, -1)]
    histories = {
        product.id: {
            month: {"actual_qty": Decimal("0"), "lost_qty": Decimal("0")}
            for month in history_months
        }
        for product in products
    }
    if product_ids:
        query_start = min(history_months[0], CUTOVER_DATE + timedelta(days=1))
        actuals = list(
            SalesOrderLine.objects.filter(
                is_counted=True,
                sku__product_variant__product_id__in=product_ids,
                order__order_date__gte=query_start,
                order__order_date__lt=target_month,
            )
            .annotate(sales_month=TruncMonth("order__order_date"))
            .values(
                product_id=F("sku__product_variant__product_id"),
                sales_month=F("sales_month"),
            )
            .annotate(
                actual_qty=Sum("quantity"),
                sold_out_date=Max("order__order_date"),
            )
        )
        for item in actuals:
            month = item["sales_month"]
            if isinstance(month, datetime):
                month = month.date()
            month = month.replace(day=1)
            if month in histories[item["product_id"]]:
                histories[item["product_id"]][month]["actual_qty"] = Decimal(
                    item["actual_qty"] or 0
                )

        for (product_id, month), lost_qty in _forecast_potential_lost_by_month(
            history_months, product_ids, actuals
        ).items():
            if product_id in histories:
                histories[product_id][month]["lost_qty"] = lost_qty

    months_complete = all(month < current_month for month in history_months)
    rows = []
    month_totals = {
        month: {"actual_qty": Decimal("0"), "lost_qty": Decimal("0"), "demand_qty": Decimal("0")}
        for month in history_months
    }
    recommendation_total = Decimal("0")
    ready_count = 0
    for product in products:
        cells = []
        for month in history_months:
            cell = histories[product.id][month]
            cell["demand_qty"] = cell["actual_qty"] + cell["lost_qty"]
            cells.append({"month": month, **cell})
            for key in ("actual_qty", "lost_qty", "demand_qty"):
                month_totals[month][key] += cell[key]
        if not any(cell["demand_qty"] for cell in cells):
            continue
        forecast, changes = _conservative_forecast(
            [cell["demand_qty"] for cell in cells]
        )
        if not months_complete:
            forecast = None
        trend = median([change for change in changes if change is not None]) if all(
            change is not None for change in changes
        ) else None
        if forecast is not None:
            ready_count += 1
            recommendation_total += forecast
        rows.append({
            "product": product,
            "cells": cells,
            "change_1": changes[0],
            "change_2": changes[1],
            "change_1_display": _percent_text(changes[0]),
            "change_2_display": _percent_text(changes[1]),
            "trend": trend,
            "trend_display": _percent_text(trend),
            "forecast_qty": forecast,
        })
    rows.sort(key=lambda row: (
        row["forecast_qty"] is None,
        -(row["forecast_qty"] or Decimal("0")),
        row["product"].name.casefold(),
    ))

    return {
        "target_month": target_month,
        "target_max": current_month,
        "history_months": history_months,
        "months_complete": months_complete,
        "error": error,
        "status_options": status_options,
        "selected_status": selected_status,
        "category_options": category_options,
        "selected_category": selected_category,
        "product_options": product_options,
        "selected_product": selected_product,
        "forecastable_count": len(products),
        "analyzed_count": len(rows),
        "ready_count": ready_count,
        "recommendation_total": recommendation_total,
        "rows": rows,
        "month_totals": [month_totals[month] for month in history_months],
    }


def _save_store_traffic(request, *, traffic_date=None, values=None):
    if not (
        request.user.is_superuser
        or module_level(request.user, "sales") in {"edit", "approve"}
    ):
        raise PermissionDenied("Input Traffic Toko memerlukan akses Edit Sales.")

    if traffic_date is None:
        try:
            traffic_date = date.fromisoformat(request.POST.get("traffic_date", ""))
        except (TypeError, ValueError):
            raise ValidationError("Tanggal traffic wajib diisi.")
    if traffic_date > timezone.localdate():
        raise ValidationError("Tanggal traffic tidak boleh melebihi hari ini.")

    if values is None:
        values = {}
        for source, field, label in (
            ("Shopee", "shopee_visitors", "Traffic Shopee"),
            ("Tiktok", "tiktok_visitors", "Traffic TikTok"),
        ):
            raw_value = request.POST.get(field, "").strip()
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                raise ValidationError(f"{label} wajib berupa angka bulat.")
            if value < 0:
                raise ValidationError(f"{label} tidak boleh negatif.")
            values[source] = value

    with transaction.atomic():
        existing = {
            metric.source: metric.visitors
            for metric in StoreTrafficMetric.objects.select_for_update().filter(
                traffic_date=traffic_date
            )
        }
        for source, visitors in values.items():
            StoreTrafficMetric.objects.update_or_create(
                source=source,
                traffic_date=traffic_date,
                defaults={"visitors": visitors, "recorded_by": request.user},
            )
        record_audit(
            actor=request.user,
            action="sales_store_traffic_saved",
            entity_type="traffic.storetrafficmetric",
            entity_id=traffic_date.isoformat(),
            before_values=existing,
            after_values=values,
        )


@login_required
def dashboard(request):
    all_lines = SalesOrderLine.objects.filter(is_counted=True)
    latest = all_lines.order_by("-order__order_date").values_list("order__order_date", flat=True).first() or date.today()
    earliest = all_lines.order_by("order__order_date").values_list("order__order_date", flat=True).first() or latest
    period_options = _pareto_period_options(earliest, latest)
    period_type = request.GET.get("period_type", "custom")
    if period_type not in {*period_options, "custom"}:
        period_type = "custom"
    if period_type == "custom":
        period_value = ""
        start = _date(request.GET.get("date_from"), latest.replace(day=1))
        end = _date(request.GET.get("date_to"), latest)
        if start > end:
            start, end = end, start
        period_trend_grain = "day"
    else:
        valid_periods = {item["value"] for item in period_options[period_type]}
        period_value = request.GET.get("period", "")
        if period_value not in valid_periods:
            period_value = period_options[period_type][-1]["value"]
        start, end = _pareto_period_bounds(period_type, period_value)
        period_trend_grain = "day" if period_type == "month" else "month"
    source_groups = [
        item for item in request.GET.getlist("source_group")
        if item in {"Marketplace", "Other"}
    ]
    source_options = _source_options(all_lines, source_groups)
    allowed_sources = {item["value"] for item in source_options}
    sources = [
        item for item in request.GET.getlist("source")
        if item and item in allowed_sources
    ]
    filtered_all_lines = _apply_source_filters(all_lines, sources, source_groups)
    lines = filtered_all_lines.filter(order__order_date__range=(start, end))
    totals = _totals(lines)
    status_rows = list(lines.values("current_status").annotate(orders=Count("order_id", distinct=True)).order_by("-orders"))
    source_rows = []
    for row in lines.values("order__source", "order__source_label").annotate(qty=Sum("quantity"), net=Sum("total_net_sales"), orders=Count("order_id", distinct=True)).order_by("-net"):
        row["source_group"] = _source_group(row["order__source"])
        source_rows.append(row)
    period_trend = _dashboard_period_trend(lines, start, end, period_trend_grain)

    monthly_end = latest.replace(day=1)
    monthly_start = max(earliest.replace(day=1), _shift_month(monthly_end, -11))
    monthly_lines = filtered_all_lines.filter(order__order_date__gte=_shift_month(monthly_start, -1))
    monthly_gross = _monthly_gross_chart(monthly_lines, monthly_start, monthly_end)
    try:
        mtd_cutoff_day = int(request.GET.get("mtd_cutoff_day", latest.day))
    except (TypeError, ValueError):
        mtd_cutoff_day = latest.day
    mtd_cutoff_day = max(1, min(mtd_cutoff_day, latest.day))
    mtd_gross = _monthly_gross_chart(
        monthly_lines.filter(order__order_date__day__lte=mtd_cutoff_day),
        monthly_start,
        monthly_end,
    )
    monthly_period_label = f"{date_format(monthly_start, 'M Y')} – {date_format(monthly_end, 'M Y')}"
    potential_month_options = []
    potential_month = (CUTOVER_DATE + timedelta(days=1)).replace(day=1)
    while potential_month <= latest.replace(day=1):
        potential_month_options.append({
            "value": potential_month.strftime("%Y-%m"),
            "label": date_format(potential_month, "F Y"),
        })
        potential_month = _shift_month(potential_month, 1)
    potential_month_value = request.GET.get(
        "potential_month", latest.strftime("%Y-%m")
    )
    if potential_month_value not in {item["value"] for item in potential_month_options}:
        potential_month_value = potential_month_options[-1]["value"] if potential_month_options else ""
    selected_potential_month = (
        datetime.strptime(potential_month_value, "%Y-%m").date()
        if potential_month_value else None
    )
    potential_cutoff = (
        min(latest, _shift_month(selected_potential_month, 1) - timedelta(days=1))
        if selected_potential_month else None
    )
    potential_sales_rows = (
        _potential_sales_rows(potential_cutoff, selected_potential_month)
        if selected_potential_month else []
    )
    traffic_sources = {SalesOrder.Source.SHOPEE, SalesOrder.Source.TIKTOK}
    if source_groups and "Marketplace" not in source_groups:
        traffic_sources.clear()
    if sources:
        selected_traffic_sources = set(
            all_lines.filter(order__source_label__in=sources)
            .values_list("order__source", flat=True)
            .distinct()
        )
        traffic_sources.intersection_update(selected_traffic_sources)

    store_traffic_by_date = {}
    for metric in StoreTrafficMetric.objects.filter(
        traffic_date__range=(start, end),
        source__in=traffic_sources,
    ).values("traffic_date", "source", "visitors"):
        row = store_traffic_by_date.setdefault(metric["traffic_date"], {
            "date": metric["traffic_date"],
            "Shopee": 0,
            "Tiktok": 0,
        })
        row[metric["source"]] = metric["visitors"]
    store_traffic_rows = []
    for traffic_date in sorted(store_traffic_by_date, reverse=True):
        row = store_traffic_by_date[traffic_date]
        row["total"] = row["Shopee"] + row["Tiktok"]
        store_traffic_rows.append(row)
    store_traffic_totals = {
        "Shopee": sum(row["Shopee"] for row in store_traffic_rows),
        "Tiktok": sum(row["Tiktok"] for row in store_traffic_rows),
    }
    store_traffic_totals["total"] = (
        store_traffic_totals["Shopee"] + store_traffic_totals["Tiktok"]
    )
    conversion_orders = (
        lines.filter(order__source__in=traffic_sources)
        .values("order_id")
        .distinct()
        .count()
    )
    conversion_rate_pct = (
        Decimal(conversion_orders)
        * Decimal("100")
        / Decimal(store_traffic_totals["total"])
        if store_traffic_totals["total"]
        else None
    )
    return render(request, "sales/dashboard.html", {
        "date_from": start,
        "date_to": end,
        "period_type": period_type,
        "period_value": period_value,
        "period_options": period_options,
        "period_trend": period_trend,
        "period_trend_grain": period_trend_grain,
        "period_trend_title": "Daily Gross Sales" if period_trend_grain == "day" else "Monthly Gross Sales Trend",
        "latest": latest,
        "totals": totals,
        "status_rows": status_rows,
        "source_rows": source_rows,
        "monthly_gross": monthly_gross,
        "mtd_gross": mtd_gross,
        "mtd_cutoff_day": mtd_cutoff_day,
        "mtd_cutoff_days": range(1, latest.day + 1),
        "monthly_period_label": monthly_period_label,
        "potential_month_options": potential_month_options,
        "potential_month_value": potential_month_value,
        "selected_potential_month": selected_potential_month,
        "potential_sales_cutoff": potential_cutoff,
        "potential_sales_rows": potential_sales_rows,
        "potential_sales_total": sum(
            (row["potential_qty"] for row in potential_sales_rows), Decimal("0")
        ),
        "potential_lost_total": sum(
            (row["lost_qty"] for row in potential_sales_rows), Decimal("0")
        ),
        "potential_sales_gross_total": sum(
            (row["potential_gross"] for row in potential_sales_rows), Decimal("0")
        ),
        "potential_lost_gross_total": sum(
            (row["lost_gross"] for row in potential_sales_rows), Decimal("0")
        ),
        "source_groups": ("Marketplace", "Other"),
        "source_options": source_options,
        "selected_sources": sources,
        "selected_source_groups": source_groups,
        "legacy_exceptions": lines.filter(sku__isnull=True).count(),
        "includes_historical": start < date(2026, 8, 1),
        "store_traffic_rows": store_traffic_rows,
        "store_traffic_totals": store_traffic_totals,
        "conversion_orders": conversion_orders,
        "conversion_rate_pct": conversion_rate_pct,
    })


@login_required
def traffic_analysis(request):
    sales_dates = SalesOrderLine.objects.filter(is_counted=True).aggregate(
        earliest=Min("order__order_date"),
        latest=Max("order__order_date"),
    )
    traffic_dates = TrafficProductMetric.objects.aggregate(
        earliest=Min("period_start"),
        latest=Max("period_end"),
    )
    latest = max(
        value for value in (sales_dates["latest"], traffic_dates["latest"], date.today()) if value
    )
    earliest = min(
        value for value in (sales_dates["earliest"], traffic_dates["earliest"], latest) if value
    )
    month_options = _pareto_period_options(earliest, latest)["month"]
    valid_months = {item["value"] for item in month_options}
    selected_month = request.GET.get("month", latest.strftime("%Y-%m"))
    if selected_month not in valid_months:
        selected_month = month_options[-1]["value"]
    start, end = _pareto_period_bounds("month", selected_month)

    source_options = ("Shopee", "Tiktok")
    selected_sources = _valid_multi_values(request, "source", source_options)
    active_sources = selected_sources or list(source_options)
    filter_state = _traffic_analysis_filter_state(request)
    scoped_products = list(filter_state.pop("products_queryset"))
    product_by_id = {product.id: product for product in scoped_products}
    allowed_product_ids = set(product_by_id)
    name_to_ids = {}
    for product in scoped_products:
        name_to_ids.setdefault(product.name, []).append(product.id)

    sales_by_product = {}
    sales_lines = SalesOrderLine.objects.filter(
        is_counted=True,
        order__order_date__range=(start, end),
        order__source__in=active_sources,
    ).filter(
        Q(sku__product_variant__product_id__in=allowed_product_ids)
        | Q(sku__isnull=True, product_name_snapshot__in=name_to_ids)
    )
    for sale in sales_lines.values(
        "sku__product_variant__product_id", "product_name_snapshot"
    ).annotate(qty=Sum("quantity"), gross=Sum("total_gross_sales")):
        product_id = sale["sku__product_variant__product_id"]
        if not product_id:
            matches = name_to_ids.get(sale["product_name_snapshot"], ())
            product_id = matches[0] if len(matches) == 1 else None
        if product_id not in allowed_product_ids:
            continue
        current = sales_by_product.setdefault(product_id, {"qty": 0, "gross": Decimal("0")})
        current["qty"] += sale["qty"] or 0
        current["gross"] += sale["gross"] or Decimal("0")

    mapping_ids = {}
    for mapping in MarketplaceProductMapping.objects.filter(
        product_id__in=allowed_product_ids,
        source__in=active_sources,
        is_active=True,
    ).values("source", "marketplace_product_code", "product_id"):
        mapping_ids.setdefault(
            (mapping["source"], mapping["marketplace_product_code"]), set()
        ).add(mapping["product_id"])

    listing_visitors = {}
    traffic = TrafficProductMetric.objects.filter(
        period_start__lte=end,
        period_end__gte=start,
        source__in=active_sources,
    ).values(
        "product_id",
        "source",
        "marketplace_product_code_snapshot",
        "traffic_product_key",
        "visitors",
    )
    for metric in traffic:
        product_id = metric["product_id"]
        if not product_id:
            matches = mapping_ids.get(
                (metric["source"], metric["marketplace_product_code_snapshot"]), ()
            )
            product_id = next(iter(matches)) if len(matches) == 1 else None
        if product_id not in allowed_product_ids:
            continue
        listing = metric["marketplace_product_code_snapshot"] or metric["traffic_product_key"]
        key = (product_id, metric["source"], listing)
        listing_visitors[key] = max(listing_visitors.get(key, 0), metric["visitors"] or 0)

    visitors_by_product = {}
    for (product_id, _source, _listing), visitors in listing_visitors.items():
        visitors_by_product[product_id] = visitors_by_product.get(product_id, 0) + visitors

    rows = []
    for product_id in set(sales_by_product) | set(visitors_by_product):
        product = product_by_id[product_id]
        sales = sales_by_product.get(product_id, {})
        visitors = visitors_by_product.get(product_id, 0)
        qty = sales.get("qty") or 0
        gross = sales.get("gross") or Decimal("0")
        rows.append({
            "product": product,
            "visitors": visitors,
            "qty": qty,
            "gross": gross,
            "qty_per_1000": Decimal(qty) * Decimal("1000") / visitors if visitors else None,
            "gross_per_1000": gross * Decimal("1000") / visitors if visitors else None,
        })
    rows.sort(key=lambda row: (
        row["gross_per_1000"] is None,
        -(row["gross_per_1000"] or Decimal("0")),
        row["product"].name,
    ))

    totals = {
        "visitors": sum(row["visitors"] for row in rows),
        "qty": sum(row["qty"] for row in rows),
        "gross": sum((row["gross"] for row in rows), Decimal("0")),
    }
    totals["qty_per_1000"] = (
        Decimal(totals["qty"]) * Decimal("1000") / totals["visitors"]
        if totals["visitors"] else None
    )
    totals["gross_per_1000"] = (
        totals["gross"] * Decimal("1000") / totals["visitors"]
        if totals["visitors"] else None
    )
    return render(request, "sales/traffic_analysis.html", {
        "rows": rows,
        "totals": totals,
        "month_options": month_options,
        "selected_month": selected_month,
        "selected_month_label": date_format(start, "M Y"),
        "sources": source_options,
        "selected_sources": selected_sources,
        **filter_state,
    })


@login_required
def product_performance(request, pivot_only=False):
    all_counted = SalesOrderLine.objects.filter(is_counted=True)
    date_bounds = all_counted.aggregate(
        earliest=Min("order__order_date"),
        latest=Max("order__order_date"),
    )
    latest = date_bounds["latest"] or date.today()
    earliest = date_bounds["earliest"] or latest
    month_options = _pareto_period_options(earliest, latest)["month"]
    period_type = request.GET.get("period_type", "custom")
    if period_type not in {"custom", "month"}:
        period_type = "custom"
    period_value = request.GET.get("period", "")
    valid_months = {item["value"] for item in month_options}
    if period_type == "month":
        if period_value not in valid_months:
            period_value = month_options[-1]["value"]
        start, end = _pareto_period_bounds("month", period_value)
    else:
        start = _date(request.GET.get("date_from"), date(latest.year, 1, 1))
        end = _date(request.GET.get("date_to"), latest)
        if start > end:
            start, end = end, start
    filter_state = _product_performance_filter_state(request)
    product_statuses = filter_state["selected_product_statuses"]
    categories = filter_state["selected_categories"]
    products = filter_state["selected_products"]
    lines = _line_filters(
        request,
        product_statuses=product_statuses,
        categories=categories,
        products=products,
    ).filter(
        order__order_date__range=(start, end),
    ).exclude(product_name_snapshot="")
    sources = [item for item in request.GET.getlist("source") if item]
    source_groups = [item for item in request.GET.getlist("source_group") if item]
    rows = []
    traffic_totals = {"views": 0, "clicks": 0, "visitors": 0}
    if not pivot_only:
        sales_monthly = {
            row["month"]: row
            for row in lines.annotate(month=TruncMonth("order__order_date")).values("month").annotate(
                qty=Sum("quantity"),
                net=Sum("total_net_sales"),
                gross=Sum("total_gross_sales"),
                orders=Count("order_id", distinct=True),
            ).order_by("month")
        }
        traffic = TrafficProductMetric.objects.filter(period_start__lte=end, period_end__gte=start)
        if sources:
            traffic_sources = [source for source in sources if source in {"Shopee", "Tiktok"}]
            traffic = traffic.filter(source__in=traffic_sources) if traffic_sources else traffic.none()
        if source_groups and "Marketplace" not in source_groups:
            traffic = traffic.none()
        if product_statuses and not products:
            traffic = traffic.filter(product__status__name__in=product_statuses)
        if categories and not products:
            traffic = traffic.filter(Q(product__category__name__in=categories) | Q(category_snapshot__in=categories))
        if products:
            selected_product_ids = Product.objects.filter(name__in=products).values_list("id", flat=True)
            product_filter = Q(product_id__in=selected_product_ids)
            for mapping in MarketplaceProductMapping.objects.filter(product_id__in=selected_product_ids, is_active=True):
                product_filter |= Q(source=mapping.source, marketplace_product_code_snapshot=mapping.marketplace_product_code)
            traffic = traffic.filter(product_filter)
        listing_monthly = {}
        for metric in traffic.annotate(month=TruncMonth("period_start")).values(
            "month", "source", "marketplace_product_code_snapshot", "traffic_product_key", "views", "clicks", "visitors"
        ):
            listing_key = metric["marketplace_product_code_snapshot"] or metric["traffic_product_key"]
            key = (metric["month"], metric["source"], listing_key)
            current = listing_monthly.setdefault(key, {"views": 0, "clicks": 0, "visitors": 0})
            for field in current:
                current[field] = max(current[field], metric[field])
        traffic_monthly = {}
        for (month, _source, _listing), metric in listing_monthly.items():
            monthly = traffic_monthly.setdefault(month, {"views": 0, "clicks": 0, "visitors": 0})
            for field in monthly:
                monthly[field] += metric[field]
        months = sorted(set(sales_monthly) | set(traffic_monthly))
        previous_net = None
        for month in months:
            sale = sales_monthly.get(month, {})
            visit = traffic_monthly.get(month, {})
            gross = sale.get("gross") or 0
            net = sale.get("net") or 0
            orders = sale.get("orders") or 0
            visitors = visit.get("visitors") or 0
            discount = gross - net
            row = {
                "month": month,
                "label": f"{month.month}. {MONTH_NAMES[month.month - 1]}",
                "views": visit.get("views") or 0,
                "clicks": visit.get("clicks") or 0,
                "visitors": visitors,
                "qty": sale.get("qty") or 0,
                "net": net,
                "gross": gross,
                "orders": orders,
                "discount": discount,
                "discount_rate": discount / gross if gross else None,
                "growth": (net - previous_net) / previous_net if previous_net else None,
                "cvr": Decimal(orders) / Decimal(visitors) if visitors else None,
                "aov": net / orders if orders else None,
            }
            previous_net = net
            row["discount_rate_pct"] = row["discount_rate"] * 100 if row["discount_rate"] is not None else None
            row["growth_pct"] = row["growth"] * 100 if row["growth"] is not None else None
            row["cvr_pct"] = row["cvr"] * 100 if row["cvr"] is not None else None
            rows.append(row)
        max_traffic = max((row["views"] for row in rows), default=0)
        max_net = max((row["net"] for row in rows), default=0)
        for row in rows:
            row["traffic_bar"] = float(row["views"] / max_traffic * 100) if max_traffic else 0
            row["sales_bar"] = float(row["net"] / max_net * 100) if max_net else 0
        traffic_totals = {
            field: sum(month[field] for month in traffic_monthly.values())
            for field in ("views", "clicks", "visitors")
        }
    filter_options = _filter_options()
    filter_options.update({
        "product_statuses": filter_state["product_statuses"],
        "categories": filter_state["categories"],
        "products": filter_state["products"],
    })
    return render(request, "sales/product_performance.html", {
        "pivot_only": pivot_only,
        "rows": rows,
        "date_from": start,
        "date_to": end,
        "period_type": period_type,
        "period_value": period_value,
        "month_options": month_options,
        "totals": _totals(lines) if not pivot_only else {},
        "pivot": _product_performance_pivot(lines, request) if pivot_only else None,
        "traffic_totals": traffic_totals,
        "source_groups": ("Marketplace", "Other"),
        "selected_sources": sources,
        "selected_source_groups": source_groups,
        "selected_product_statuses": product_statuses,
        "selected_categories": categories,
        "selected_products": products,
        **filter_options,
    })


@login_required
def pareto(request):
    lines = _line_filters(request)
    all_counted = SalesOrderLine.objects.filter(is_counted=True)
    latest = all_counted.order_by("-order__order_date").values_list("order__order_date", flat=True).first() or date.today()
    earliest = all_counted.order_by("order__order_date").values_list("order__order_date", flat=True).first() or latest
    period_options = _pareto_period_options(earliest, latest)
    period_type = request.GET.get("period_type", "year")
    if period_type not in period_options:
        period_type = "year"
    valid_periods = {item["value"] for item in period_options[period_type]}
    period_value = request.GET.get("period", "")
    if period_value not in valid_periods:
        period_value = period_options[period_type][-1]["value"]
    period_start, period_end = _pareto_period_bounds(period_type, period_value)
    lines = lines.filter(order__order_date__range=(period_start, period_end))
    totals = _totals(lines)
    product_rows = list(
        lines.annotate(product_group=Case(
            When(product_name_snapshot="", then=Value("Unmapped Product")),
            default=F("product_name_snapshot"),
            output_field=CharField(),
        )).values("product_group").annotate(
            qty=Sum("quantity"),
            net=Sum("total_net_sales"),
            cogs=Sum("total_cogs"),
            margin=Sum("gpm"),
        ).order_by("-net", "product_group")
    )
    cumulative = Decimal("0")
    denominator = totals["net"] or Decimal("0")
    for row in product_rows:
        contribution = row["net"] / denominator if denominator else Decimal("0")
        cumulative += contribution
        row["product"] = row.pop("product_group")
        row["margin_ratio"] = row["net"] / row["cogs"] if row["cogs"] else None
        row["contribution"] = contribution
        row["cumulative"] = cumulative
        row["contribution_pct"] = contribution * 100
        row["cumulative_pct"] = cumulative * 100
        row["class"] = "A" if cumulative <= Decimal("0.80") else ("B" if cumulative <= Decimal("0.95") else "C")
    class_a_share = sum((row["contribution"] for row in product_rows if row["class"] == "A"), Decimal("0"))
    return render(request, "sales/pareto.html", {
        "rows": product_rows,
        "totals": totals,
        "class_a_share": class_a_share * 100,
        "period_type": period_type,
        "period_value": period_value,
        "period_start": period_start,
        "period_end": period_end,
        "period_options": period_options,
        "total_products": len(product_rows),
        **_filter_options(),
    })


@login_required
def transactions(request):
    lines = _line_filters(request).select_related("order", "sku").order_by("-order__order_date", "order__source_label", "order__order_number", "sku_code_snapshot")
    latest = lines.values_list("order__order_date", flat=True).first() or date.today()
    start = _date(request.GET.get("date_from"), date(latest.year, 1, 1))
    end = _date(request.GET.get("date_to"), latest)
    lines = lines.filter(order__order_date__range=(start, end))
    status = request.GET.get("status", "")
    query = request.GET.get("q", "").strip()
    if status:
        lines = lines.filter(current_status=status)
    if query:
        lines = lines.filter(Q(order__order_number__icontains=query) | Q(sku_code_snapshot__icontains=query) | Q(product_name_snapshot__icontains=query))
    if request.GET.get("export") == "xlsx":
        return _export_transactions(lines, start, end)
    page = Paginator(lines, 50).get_page(request.GET.get("page"))
    return render(request, "sales/transactions.html", {
        "page": page,
        "date_from": start,
        "date_to": end,
        "totals": _totals(lines),
        "statuses": SalesOrderLine.objects.exclude(current_status="").order_by().values_list("current_status", flat=True).distinct(),
        **_filter_options(),
    })


@login_required
def input_transaction(request):
    form = ManualSaleHeaderForm(request.POST or None)
    formset = ManualSaleLineFormSet(request.POST or None, prefix="products")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        line_data = [
            {
                "sku": row.cleaned_data["sku"],
                "quantity": row.cleaned_data["quantity"],
                "net_unit_price": row.cleaned_data["net_unit_price"],
            }
            for row in formset.forms
            if row.cleaned_data and not row.cleaned_data.get("DELETE")
        ]
        try:
            lines = create_manual_sales(actor=request.user, lines=line_data, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            special_lines = [line for line in lines if line.retail_price_special_case]
            if special_lines:
                special_skus = ", ".join(line.sku_code_snapshot for line in special_lines)
                messages.warning(
                    request,
                    f"SPECIAL CASE HARGA · Transaksi {lines[0].order.order_number} berhasil "
                    f"diposting dengan {len(lines)} Product. Snapshot Retail Price disesuaikan "
                    f"hanya untuk SKU {special_skus}; master tetap tidak berubah.",
                )
            else:
                messages.success(
                    request,
                    f"Transaksi manual {lines[0].order.order_number} berhasil diposting "
                    f"dengan {len(lines)} Product.",
                )
            return redirect("sales:input_transaction")

    sku_catalog = list(
        SKU.objects.filter(is_active=True, product_variant__product__is_active=True)
        .order_by("sku")
        .values("id", "product_variant__product_id", "current_retail_price")
    )
    return render(
        request,
        "sales/input_transaction.html",
        {"form": form, "formset": formset, "sku_catalog": sku_catalog},
    )
