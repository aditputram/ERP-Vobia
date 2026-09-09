from datetime import date
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Max, Sum
from django.utils import timezone

from sales.models import SalesOrderLine
from inventory.models import PhysicalReturnReceipt

from merchandising.models import MerchandisingMonthlySnapshot

from .calculations import current_month_metric_values, current_month_multiplier


ZERO = Decimal("0")


def official_planning_state(batch, run_date=None):
    """Resolve the current official planning month and canonical Sales cutoff."""
    latest_snapshot_month = (
        MerchandisingMonthlySnapshot.objects.filter(batch=batch)
        .aggregate(latest=Max("month"))["latest"]
    )
    if latest_snapshot_month is None:
        return None

    planning_year = latest_snapshot_month.year
    run_date = run_date or timezone.localdate()
    eligible_lines = SalesOrderLine.objects.filter(
        is_counted=True,
        order__order_date__year=planning_year,
    )
    latest_actual_date = eligible_lines.aggregate(latest=Max("order__order_date"))["latest"]

    if run_date.year == planning_year:
        current_month_number = run_date.month
    elif latest_actual_date:
        current_month_number = latest_actual_date.month
        run_date = latest_actual_date
    else:
        current_month_number = 1
        run_date = date(planning_year, 1, 1)

    cutoff_date = eligible_lines.filter(
        order__order_date__month=current_month_number,
    ).aggregate(cutoff=Max("order__order_date"))["cutoff"]
    return {
        "year": planning_year,
        "current_month_number": current_month_number,
        "cutoff_date": cutoff_date,
        "run_date": run_date,
        "day_factor": current_month_multiplier(run_date),
    }


def official_current_month_values(batch, sku_ids, state):
    """Return one official current-month metric mapping per requested SKU."""
    sku_ids = list(sku_ids)
    if not sku_ids or not state:
        return {}

    month_number = state["current_month_number"]
    year = state["year"]
    current_snapshots = {
        row.sku_id: row
        for row in MerchandisingMonthlySnapshot.objects.filter(
            batch=batch,
            sku_id__in=sku_ids,
            month=date(year, month_number, 1),
        )
    }
    if month_number == 1:
        prior_ending = {
            sku_id: snapshot.prior_year_ending_qty
            for sku_id, snapshot in current_snapshots.items()
        }
    else:
        prior_ending = {
            row.sku_id: row.ending_qty
            for row in MerchandisingMonthlySnapshot.objects.filter(
                batch=batch,
                sku_id__in=sku_ids,
                month=date(year, month_number - 1, 1),
            )
        }

    actuals = {
        row["sku_id"]: row
        for row in SalesOrderLine.objects.filter(
            is_counted=True,
            sku_id__in=sku_ids,
            order__order_date__year=year,
            order__order_date__month=month_number,
        )
        .values("sku_id")
        .annotate(
            actual_qty=Sum("quantity"),
            actual_gross=Sum("total_gross_sales"),
            actual_net=Sum("total_net_sales"),
        )
    }
    returns = {
        row["sales_line__sku_id"]: row["actual_return"]
        for row in PhysicalReturnReceipt.objects.filter(
            sales_line__is_counted=True,
            sales_line__sku_id__in=sku_ids,
            received_date__year=year,
            received_date__month=month_number,
            received_date__lte=state["run_date"],
        )
        .values("sales_line__sku_id")
        .annotate(
            actual_return=Sum(
                ExpressionWrapper(
                    F("quantity") * F("sales_line__net_unit_price"),
                    output_field=DecimalField(max_digits=24, decimal_places=4),
                )
            )
        )
    }

    values = {}
    for sku_id, snapshot in current_snapshots.items():
        actual = actuals.get(sku_id, {})
        values[sku_id] = current_month_metric_values(
            prior_ending_qty=prior_ending.get(sku_id, ZERO),
            incoming_qty=snapshot.incoming_qty,
            actual_qty=actual.get("actual_qty", ZERO),
            actual_gross=actual.get("actual_gross"),
            actual_net=actual.get("actual_net", ZERO),
            actual_return=returns.get(sku_id, ZERO),
            cutoff_date=state["cutoff_date"],
            cogs=snapshot.cogs_snapshot,
            retail_price=snapshot.retail_price_snapshot,
            run_date=state["run_date"],
        )
    return values
