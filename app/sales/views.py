from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
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
from master_data.models import Category, MarketplaceProductMapping, Product, ProductStatus, SKU, Subcategory
from merchandising.services.planning_activity import (
    filter_products_by_planning_activity,
    planning_activity_snapshot,
)
from merchandising.services.builder import historical_sales_qty_for_skus, official_values_for_skus
from traffic.models import TrafficProductMetric

from .forms import ManualSaleForm
from .models import SalesOrder, SalesOrderLine, SalesPlan, SalesPlanSKU, SalesPlanningScenario
from .services.manual import create_manual_sale


MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

SALES_PROJECTION_METHODS = (
    ("INCREASE_PERCENT", "Increase by %"),
    ("DECREASE_PERCENT", "Decrease by %"),
    ("SAME_AS_LAST_MONTH", "Sama dengan Bulan Lalu"),
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
            _excel_text(order.current_status),
            _excel_text(order.source_status),
            order.is_final,
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
    submitted_skus = {key.removeprefix("target_qty_") for key in request.POST if key.startswith("target_qty_")}
    if submitted_skus != {str(row["sku"].id) for row in preview["sku_rows"]}:
        raise ValidationError("Pilihan Product sudah berubah. Buat Preview ulang sebelum menyimpan target.")
    targets = []
    for row in preview["sku_rows"]:
        raw_qty = request.POST.get(f"target_qty_{row['sku'].id}")
        try:
            target_qty = int(raw_qty)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Target Sales Qty {row['sku'].sku} harus berupa angka bulat.") from exc
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
                "skus": [str(row["sku"].id) for row in targets],
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
    if {key.removeprefix("qty_") for key in request.POST if key.startswith("qty_")} != {str(target.id) for target in targets}:
        raise ValidationError("Isi Draft telah berubah. Muat ulang sebelum menyimpan agar target lain tidak tertimpa.")
    values = []
    for target in targets:
        try:
            qty = int(request.POST.get(f"qty_{target.id}") or "0")
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ValidationError(f"Target {target.sku.sku} harus berupa angka yang valid.") from exc
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
                "skus": len(values),
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
        .annotate(projection_count=Count("projections__sku_targets", distinct=True))
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
        "selected_draft_grain": "parent_sku" if request.GET.get("draft_grain") == "parent_sku" else "sku",
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
    status_rows = list(lines.values("order__current_status").annotate(orders=Count("order_id", distinct=True)).order_by("-orders"))
    source_rows = []
    for row in lines.values("order__source", "order__source_label").annotate(qty=Sum("quantity"), net=Sum("total_net_sales"), orders=Count("order_id", distinct=True)).order_by("-net"):
        row["source_group"] = _source_group(row["order__source"])
        source_rows.append(row)
    period_trend = _dashboard_period_trend(lines, start, end, period_trend_grain)

    monthly_end = latest.replace(day=1)
    monthly_start = max(earliest.replace(day=1), _shift_month(monthly_end, -11))
    monthly_lines = filtered_all_lines.filter(order__order_date__gte=_shift_month(monthly_start, -1))
    monthly_gross = _monthly_gross_chart(monthly_lines, monthly_start, monthly_end)
    mtd_gross = _monthly_gross_chart(
        monthly_lines.filter(order__order_date__day__lte=latest.day),
        monthly_start,
        monthly_end,
    )
    monthly_period_label = f"{date_format(monthly_start, 'M Y')} – {date_format(monthly_end, 'M Y')}"
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
        "mtd_cutoff_day": latest.day,
        "monthly_period_label": monthly_period_label,
        "source_groups": ("Marketplace", "Other"),
        "source_options": source_options,
        "selected_sources": sources,
        "selected_source_groups": source_groups,
        "legacy_exceptions": lines.filter(sku__isnull=True).count(),
        "includes_historical": start < date(2026, 8, 1),
    })


@login_required
def product_performance(request):
    latest = SalesOrderLine.objects.filter(is_counted=True).order_by("-order__order_date").values_list("order__order_date", flat=True).first() or date.today()
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
    sources = [item for item in request.GET.getlist("source") if item]
    source_groups = [item for item in request.GET.getlist("source_group") if item]
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
    rows = []
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
    filter_options = _filter_options()
    filter_options.update({
        "product_statuses": filter_state["product_statuses"],
        "categories": filter_state["categories"],
        "products": filter_state["products"],
    })
    traffic_totals = {
        field: sum(month[field] for month in traffic_monthly.values())
        for field in ("views", "clicks", "visitors")
    }
    return render(request, "sales/product_performance.html", {
        "rows": rows,
        "date_from": start,
        "date_to": end,
        "totals": _totals(lines),
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
        lines = lines.filter(order__current_status=status)
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
        "statuses": SalesOrderLine.objects.order_by().values_list("order__current_status", flat=True).distinct(),
        **_filter_options(),
    })


@login_required
def input_transaction(request):
    form = ManualSaleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            line = create_manual_sale(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            if getattr(line, "retail_price_special_case", False):
                master_retail = f"{line.master_retail_price_at_entry:,.0f}".replace(",", ".")
                retail_snapshot = f"{line.retail_price_snapshot:,.0f}".replace(",", ".")
                messages.warning(
                    request,
                    f"SPECIAL CASE HARGA · Transaksi {line.business_key} berhasil diposting. "
                    f"Retail Price master tetap Rp {master_retail}; snapshot transaksi ini saja "
                    f"disesuaikan menjadi Rp {retail_snapshot}.",
                )
            else:
                messages.success(request, f"Transaksi manual {line.business_key} berhasil diposting.")
            return redirect("sales:input_transaction")

    return render(request, "sales/input_transaction.html", {"form": form})
