from calendar import monthrange
from datetime import date, timedelta

from django.db.models import Max

from imports.models import SalesImportBatch
from sales.models import SalesOrder


FINAL_STATUSES = {"Selesai", "Retur"}


def summarize_import_requirements(requirements):
    """Collapse detailed monthly requirements into one export range per marketplace."""
    summaries = {}
    for item in requirements:
        source = item["source"]
        summary = summaries.setdefault(
            source,
            {
                "source": source,
                "period_start": item["period_start"],
                "period_end": item["period_end"],
            },
        )
        summary["period_start"] = min(summary["period_start"], item["period_start"])
        summary["period_end"] = max(summary["period_end"], item["period_end"])
    source_order = {SalesOrder.Source.SHOPEE: 0, SalesOrder.Source.TIKTOK: 1}
    return sorted(summaries.values(), key=lambda item: source_order.get(item["source"], 99))


def import_requirements(as_of_date=None):
    as_of_date = as_of_date or date.today()
    yesterday = as_of_date - timedelta(days=1)
    requirements = {}
    for source in (SalesOrder.Source.SHOPEE, SalesOrder.Source.TIKTOK):
        last_batch = SalesImportBatch.objects.filter(
            source=source,
            status=SalesImportBatch.Status.COMMITTED,
        ).order_by("-committed_at").first()
        last_cutoff = last_batch.data_end.date() if last_batch and last_batch.data_end else None
        if last_cutoff is None or last_cutoff < yesterday:
            start = (last_cutoff + timedelta(days=1)) if last_cutoff else yesterday.replace(day=1)
            key = (source, start.year, start.month)
            requirements[key] = {
                "source": source,
                "period_start": start,
                "period_end": yesterday,
                "reasons": ["Transaksi baru sampai kemarin"],
                "nonfinal_count": 0,
                "nonfinal_statuses": set(),
                "last_successful_import": last_batch.committed_at if last_batch else None,
                "last_cutoff": last_cutoff,
            }
    nonfinal = SalesOrder.objects.exclude(current_status__in=FINAL_STATUSES).filter(
        source__in=[SalesOrder.Source.SHOPEE, SalesOrder.Source.TIKTOK]
    )
    for order in nonfinal:
        month_start = order.order_date.replace(day=1)
        month_end = date(order.order_date.year, order.order_date.month, monthrange(order.order_date.year, order.order_date.month)[1])
        if month_end > yesterday:
            month_end = yesterday
        key = (order.source, order.order_date.year, order.order_date.month)
        last_batch = SalesImportBatch.objects.filter(
            source=order.source,
            status=SalesImportBatch.Status.COMMITTED,
        ).order_by("-committed_at").first()
        item = requirements.setdefault(
            key,
            {
                "source": order.source,
                "period_start": month_start,
                "period_end": month_end,
                "reasons": [],
                "nonfinal_count": 0,
                "nonfinal_statuses": set(),
                "last_successful_import": last_batch.committed_at if last_batch else None,
                "last_cutoff": last_batch.data_end.date() if last_batch and last_batch.data_end else None,
            },
        )
        if "Status pesanan belum final" not in item["reasons"]:
            item["reasons"].append("Status pesanan belum final")
        item["period_start"] = min(item["period_start"], month_start)
        item["period_end"] = max(item["period_end"], month_end)
        item["nonfinal_count"] += 1
        item["nonfinal_statuses"].add(order.current_status)
    result = sorted(requirements.values(), key=lambda item: (item["source"], item["period_start"]))
    for item in result:
        item["nonfinal_statuses"] = ", ".join(sorted(item["nonfinal_statuses"])) or "—"
        item["reason"] = " + ".join(item["reasons"])
    return result
