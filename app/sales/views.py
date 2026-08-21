from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, CharField, Count, F, Q, Sum, Value, When
from django.db.models.functions import TruncMonth
from django.shortcuts import render
from django.utils.formats import date_format

from traffic.models import TrafficProductMetric

from .models import SalesOrderLine


MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _date(value, fallback):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


def _source_group(source):
    return "Marketplace" if source in {"Shopee", "Tiktok"} else "Other"


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


def _line_filters(request, queryset=None):
    qs = queryset or SalesOrderLine.objects.filter(is_counted=True)
    sources = [item for item in request.GET.getlist("source") if item]
    source_groups = [item for item in request.GET.getlist("source_group") if item]
    categories = [item for item in request.GET.getlist("category") if item]
    subcategory = request.GET.get("subcategory", "")
    products = [item for item in request.GET.getlist("product") if item]
    if sources:
        qs = qs.filter(order__source_label__in=sources)
    if source_groups:
        group_filter = Q()
        if "Marketplace" in source_groups:
            group_filter |= Q(order__source__in=["Shopee", "Tiktok"])
        if "Other" in source_groups:
            group_filter |= Q(order__source="Other")
        qs = qs.filter(group_filter)
    if categories:
        qs = qs.filter(category_snapshot__in=categories)
    if subcategory:
        qs = qs.filter(subcategory_snapshot=subcategory)
    if products:
        qs = qs.filter(product_name_snapshot__in=products)
    return qs


def _filter_options():
    lines = SalesOrderLine.objects.filter(is_counted=True)
    return {
        "sources": lines.order_by().values_list("order__source_label", flat=True).distinct(),
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


@login_required
def dashboard(request):
    all_lines = SalesOrderLine.objects.filter(is_counted=True)
    latest = all_lines.order_by("-order__order_date").values_list("order__order_date", flat=True).first() or date.today()
    earliest = all_lines.order_by("order__order_date").values_list("order__order_date", flat=True).first() or latest
    start = _date(request.GET.get("date_from"), latest.replace(day=1))
    end = _date(request.GET.get("date_to"), latest)
    if start > end:
        start, end = end, start
    lines = SalesOrderLine.objects.filter(is_counted=True, order__order_date__range=(start, end))
    totals = _totals(lines)
    status_rows = list(lines.values("order__current_status").annotate(orders=Count("order_id", distinct=True)).order_by("-orders"))
    source_rows = []
    for row in lines.values("order__source_label").annotate(qty=Sum("quantity"), net=Sum("total_net_sales"), orders=Count("order_id", distinct=True)).order_by("-net"):
        row["source_group"] = _source_group(row["order__source_label"])
        source_rows.append(row)
    daily_aggregates = {
        row["day"]: row
        for row in lines.annotate(day=F("order__order_date"))
        .values("day")
        .annotate(qty=Sum("quantity"), net=Sum("total_net_sales"), gross=Sum("total_gross_sales"), orders=Count("order_id", distinct=True))
        .order_by("day")
    }
    daily = []
    current_day = start
    while current_day <= end:
        aggregate = daily_aggregates.get(current_day, {})
        daily.append({
            "day": current_day,
            "qty": aggregate.get("qty") or 0,
            "net": aggregate.get("net") or 0,
            "gross": aggregate.get("gross") or 0,
            "orders": aggregate.get("orders") or 0,
        })
        current_day += timedelta(days=1)
    max_gross = max((row["gross"] for row in daily), default=0)
    for row in daily:
        row["label"] = date_format(row["day"], "d M Y" if start.year != end.year else "d M")
        row["bar"] = float((row["gross"] or 0) / max_gross * 100) if max_gross else 0

    monthly_end = latest.replace(day=1)
    monthly_start = max(earliest.replace(day=1), _shift_month(monthly_end, -11))
    monthly_lines = all_lines.filter(order__order_date__gte=_shift_month(monthly_start, -1))
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
        "latest": latest,
        "totals": totals,
        "status_rows": status_rows,
        "source_rows": source_rows,
        "daily": daily,
        "monthly_gross": monthly_gross,
        "mtd_gross": mtd_gross,
        "mtd_cutoff_day": latest.day,
        "monthly_period_label": monthly_period_label,
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
    lines = _line_filters(request).filter(
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
    categories = [item for item in request.GET.getlist("category") if item]
    products = [item for item in request.GET.getlist("product") if item]
    if sources:
        traffic_sources = [source for source in sources if source in {"Shopee", "Tiktok"}]
        traffic = traffic.filter(source__in=traffic_sources) if traffic_sources else traffic.none()
    if source_groups and "Marketplace" not in source_groups:
        traffic = traffic.none()
    if categories:
        traffic = traffic.filter(Q(product__category__name__in=categories) | Q(category_snapshot__in=categories))
    if products:
        product_filter = Q()
        for product in products:
            keyword = product.rsplit(" - ", maxsplit=1)[-1].strip()
            product_filter |= Q(product__name=product) | Q(product_name_snapshot__icontains=keyword)
        traffic = traffic.filter(product_filter)
    traffic_monthly = {
        row["month"]: row
        for row in traffic.annotate(month=TruncMonth("period_start")).values("month").annotate(
            views=Sum("views"), clicks=Sum("clicks"), visitors=Sum("visitors")
        ).order_by("month")
    }
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
    return render(request, "sales/product_performance.html", {
        "rows": rows,
        "date_from": start,
        "date_to": end,
        "totals": _totals(lines),
        "traffic_totals": traffic.aggregate(views=Sum("views"), clicks=Sum("clicks"), visitors=Sum("visitors")),
        "source_groups": ("Marketplace", "Other"),
        "selected_sources": sources,
        "selected_source_groups": source_groups,
        "selected_categories": categories,
        "selected_products": products,
        **_filter_options(),
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
    page = Paginator(lines, 50).get_page(request.GET.get("page"))
    return render(request, "sales/transactions.html", {
        "page": page,
        "date_from": start,
        "date_to": end,
        "totals": _totals(lines),
        "statuses": SalesOrderLine.objects.order_by().values_list("order__current_status", flat=True).distinct(),
        **_filter_options(),
    })
