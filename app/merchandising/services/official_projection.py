from collections import defaultdict
from datetime import date
from decimal import Decimal

from dashboard.models import CampaignProduct
from django.db.models import DecimalField, ExpressionWrapper, F, Max, Min, Sum
from django.utils import timezone

from sales.models import SalesOrderLine
from inventory.models import FIFOOpeningSnapshot, InventoryMovement, PhysicalReturnReceipt
from master_data.models import SKU

from merchandising.models import MerchandisingMonthlySnapshot

from .calculations import current_month_metric_values, current_month_multiplier


ZERO = Decimal("0")


def _regular_sellable_start_dates(sku_ids, month_start, cutoff_date):
    """First current-month date each regular SKU had positive physical stock."""
    opening = {
        row["sku_id"]: row["opening_qty"]
        for row in FIFOOpeningSnapshot.objects.filter(
            sku_id__in=sku_ids,
            cutover_date__lt=month_start,
        ).values("sku_id", "opening_qty")
    }
    balances = defaultdict(lambda: ZERO, opening)
    known_skus = set(opening)
    before_month = (
        InventoryMovement.objects.filter(
            sku_id__in=sku_ids,
            movement_date__lt=month_start,
        )
        .exclude(movement_type=InventoryMovement.MovementType.OPENING)
        .values("sku_id", "direction")
        .annotate(total=Sum("quantity"))
    )
    for row in before_month:
        known_skus.add(row["sku_id"])
        balances[row["sku_id"]] += (
            row["total"]
            if row["direction"] == InventoryMovement.Direction.IN
            else -row["total"]
        )

    daily = defaultdict(lambda: defaultdict(lambda: {"in": ZERO, "out": ZERO}))
    current_month = (
        InventoryMovement.objects.filter(
            sku_id__in=sku_ids,
            movement_date__gte=month_start,
            movement_date__lte=cutoff_date,
        )
        .exclude(movement_type=InventoryMovement.MovementType.OPENING)
        .values("sku_id", "movement_date", "direction")
        .annotate(total=Sum("quantity"))
        .order_by("movement_date")
    )
    for row in current_month:
        known_skus.add(row["sku_id"])
        key = "in" if row["direction"] == InventoryMovement.Direction.IN else "out"
        daily[row["sku_id"]][row["movement_date"]][key] += row["total"]

    starts = {}
    for sku_id in sku_ids:
        if sku_id not in known_skus:
            # Historical/test data can predate the Inventory ledger. Preserve the
            # established calendar-day denominator when no physical evidence exists.
            starts[sku_id] = month_start
            continue
        if balances[sku_id] > 0:
            starts[sku_id] = month_start
            continue
        starts[sku_id] = None
        for movement_date, totals in sorted(daily[sku_id].items()):
            balances[sku_id] += totals["in"]
            if balances[sku_id] > 0:
                starts[sku_id] = movement_date
                break
            balances[sku_id] -= totals["out"]
    return starts


def _seasonal_launch_dates(product_ids):
    launches = {}
    rows = CampaignProduct.objects.filter(product_id__in=product_ids).select_related("campaign")
    for row in rows:
        launch_date = row.campaign.actual_launch_date or row.campaign.launch_date
        if launch_date and (
            row.product_id not in launches or launch_date < launches[row.product_id]
        ):
            launches[row.product_id] = launch_date
    return launches


def _selling_contexts(skus, year, month_number, cutoff_date, first_sales=None):
    if not cutoff_date:
        return {}
    month_start = date(year, month_number, 1)
    regular_starts = _regular_sellable_start_dates(
        [sku.id for sku in skus], month_start, cutoff_date
    )
    launches = _seasonal_launch_dates(
        {sku.product_variant.product_id for sku in skus}
    )
    previously_sold_product_ids = set(
        SalesOrderLine.objects.filter(
            is_counted=True,
            sku__product_variant__product_id__in={
                sku.product_variant.product_id for sku in skus
            },
            order__order_date__lt=month_start,
        ).values_list("sku__product_variant__product_id", flat=True)
    )
    contexts = {}
    first_sales = first_sales or {}
    for sku in skus:
        product = sku.product_variant.product
        is_seasonal_new = (
            product.status.name.strip().casefold() == "seasonal new"
            and product.id not in previously_sold_product_ids
        )
        if is_seasonal_new:
            launch_date = launches.get(product.id)
            start_date = max(month_start, launch_date) if launch_date else None
            reason = "Seasonal New · Launching Date"
        else:
            launch_date = None
            start_date = regular_starts.get(sku.id, month_start)
            reason = "Stok awal kosong" if start_date and start_date > month_start else "Reguler"
            first_sale_date = first_sales.get(sku.id)
            if first_sale_date and (not start_date or first_sale_date < start_date):
                start_date = first_sale_date
                reason = "Stock ledger perlu dicek · first sale fallback"
        selling_days = (
            (cutoff_date - start_date).days + 1
            if start_date and start_date <= cutoff_date
            else 0
        )
        contexts[sku.id] = {
            "selling_start_date": start_date,
            "selling_days": selling_days,
            "selling_start_reason": reason,
            "launch_date_missing": is_seasonal_new and launch_date is None,
        }
    return contexts


def received_return_values(sku_ids, year, through_date=None):
    """Return received net value keyed by SKU and receipt month."""
    rows = PhysicalReturnReceipt.objects.filter(
        sales_line__is_counted=True,
        sales_line__sku_id__in=sku_ids,
        received_date__year=year,
    )
    if through_date:
        rows = rows.filter(received_date__lte=through_date)
    return {
        (row["sales_line__sku_id"], row["received_date__month"]): row["actual_return"]
        for row in rows.values("sales_line__sku_id", "received_date__month").annotate(
            actual_return=Sum(
                ExpressionWrapper(
                    F("quantity") * F("sales_line__net_unit_price"),
                    output_field=DecimalField(max_digits=24, decimal_places=4),
                )
            )
        )
    }


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
    skus = list(
        SKU.objects.filter(id__in=sku_ids).select_related(
            "product_variant__product__status"
        )
    )
    sku_by_id = {sku.id: sku for sku in skus}
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

    actual_lines = SalesOrderLine.objects.filter(
        is_counted=True,
        sku_id__in=sku_ids,
        order__order_date__year=year,
        order__order_date__month=month_number,
    )
    if state["cutoff_date"]:
        actual_lines = actual_lines.filter(order__order_date__lte=state["cutoff_date"])
    actuals = {
        row["sku_id"]: row
        for row in actual_lines
        .values("sku_id")
        .annotate(
            actual_qty=Sum("quantity"),
            actual_gross=Sum("total_gross_sales"),
            actual_net=Sum("total_net_sales"),
            actual_cogs=Sum("total_cogs"),
            first_sale_date=Min("order__order_date"),
        )
    }
    selling_contexts = _selling_contexts(
        skus,
        year,
        month_number,
        state["cutoff_date"],
        {sku_id: row["first_sale_date"] for sku_id, row in actuals.items()},
    )
    returns = received_return_values(sku_ids, year, through_date=state["run_date"])

    values = {}
    for sku_id, snapshot in current_snapshots.items():
        actual = actuals.get(sku_id, {})
        selling_context = selling_contexts.get(sku_id, {})
        values[sku_id] = current_month_metric_values(
            prior_ending_qty=prior_ending.get(sku_id, ZERO),
            incoming_qty=snapshot.incoming_qty,
            actual_qty=actual.get("actual_qty", ZERO),
            actual_gross=actual.get("actual_gross"),
            actual_net=actual.get("actual_net", ZERO),
            actual_return=returns.get((sku_id, month_number), ZERO),
            actual_cogs=actual.get("actual_cogs"),
            cutoff_date=state["cutoff_date"],
            cogs=snapshot.cogs_snapshot,
            retail_price=snapshot.retail_price_snapshot,
            run_date=state["run_date"],
            selling_days=selling_context.get("selling_days"),
        )
        sku = sku_by_id.get(sku_id)
        product = sku.product_variant.product if sku else None
        values[sku_id].update(
            {
                **selling_context,
                "sku": sku.sku if sku else "",
                "product_id": product.id if product else None,
                "product_name": product.name if product else snapshot.product_snapshot,
                "article": product.article if product else "",
                "product_status": product.status.name if product else snapshot.status_snapshot,
            }
        )
    return values
