import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError

from merchandising.models import ProjectionRule


ZERO = Decimal("0")
NO_INCOMING_STATUSES = {"Discontinue", "Seasonal New"}
NO_INCOMING_CATEGORIES = {"Packaging"}


def current_month_multiplier(cutoff_date):
    if cutoff_date.day <= 12:
        return 25
    if cutoff_date.day <= 17:
        return 26
    if cutoff_date.day <= 24:
        return 27
    return calendar.monthrange(cutoff_date.year, cutoff_date.month)[1]


def beginning_quantity(prior_ending_qty, current_month_incoming_qty):
    """Beginning month M = Ending month M-1 + Incoming month M."""
    return Decimal(prior_ending_qty) + Decimal(current_month_incoming_qty)


def current_month_projection(actual_qty, cutoff_date, beginning_qty, run_date=None):
    """Project actual-to-full-month and cap sales to non-negative sellable beginning."""
    actual_qty = Decimal(actual_qty)
    beginning_qty = Decimal(beginning_qty)
    run_date = run_date or cutoff_date
    if actual_qty < 0:
        raise ValidationError("Actual Qty tidak boleh negatif.")
    if cutoff_date.day <= 0:
        raise ValidationError("Cutoff date tidak valid.")
    recommendation = actual_qty / Decimal(cutoff_date.day) * Decimal(
        current_month_multiplier(run_date)
    )
    recommendation = recommendation.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return min(recommendation, max(beginning_qty, ZERO))


def current_month_metric_values(
    *,
    prior_ending_qty,
    incoming_qty,
    actual_qty,
    actual_net,
    cutoff_date,
    cogs,
    retail_price,
    run_date,
):
    """Build the official current-month merchandising metrics from ERP actuals."""
    prior_ending_qty = Decimal(prior_ending_qty or ZERO)
    incoming_qty = Decimal(incoming_qty or ZERO)
    actual_qty = Decimal(actual_qty or ZERO)
    actual_net = Decimal(actual_net or ZERO)
    cogs = Decimal(cogs or ZERO)
    retail_price = Decimal(retail_price or ZERO)

    beginning_qty = beginning_quantity(prior_ending_qty, incoming_qty)
    sales_qty = (
        current_month_projection(actual_qty, cutoff_date, beginning_qty, run_date=run_date)
        if cutoff_date
        else ZERO
    )
    average_net_price = actual_net / actual_qty if actual_qty else ZERO
    sales_cogs = sales_qty * cogs
    sales_gross = sales_qty * retail_price
    sales_net = (sales_qty * average_net_price).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    ending_qty = beginning_qty - sales_qty

    return {
        "previous_ending_qty": prior_ending_qty,
        "beginning_qty": beginning_qty,
        "beginning_cogs": beginning_qty * cogs,
        "beginning_gross": beginning_qty * retail_price,
        "sales_qty": sales_qty,
        "sales_cogs": sales_cogs,
        "sales_gross": sales_gross,
        "sales_net": sales_net,
        "ratio": beginning_qty / sales_qty if sales_qty else None,
        "ending_qty": ending_qty,
        "ending_cogs": ending_qty * cogs,
        "ending_gross": ending_qty * retail_price,
        "mos": ending_qty / sales_qty if sales_qty else None,
    }


def future_projection(method, baseline_qty, parameter, beginning_qty=None):
    baseline_qty = Decimal(baseline_qty)
    parameter = Decimal(parameter)
    if baseline_qty < 0 or parameter < 0:
        raise ValidationError("Baseline dan parameter tidak boleh negatif.")
    if method == ProjectionRule.Method.INCREASE_PERCENT:
        return baseline_qty * (Decimal("1") + parameter / Decimal("100"))
    if method == ProjectionRule.Method.DECREASE_PERCENT:
        return max(baseline_qty * (Decimal("1") - parameter / Decimal("100")), ZERO)
    if method == ProjectionRule.Method.TARGET_STOCK_RATIO:
        if parameter <= 0:
            raise ValidationError("Target Stock Ratio harus lebih besar dari nol.")
        if beginning_qty is None:
            raise ValidationError("Beginning Qty wajib untuk Target Stock Ratio.")
        beginning_qty = Decimal(beginning_qty)
        if beginning_qty < 0:
            raise ValidationError("Beginning Qty tidak boleh negatif.")
        return beginning_qty / parameter
    raise ValidationError("Projection method tidak dikenali.")


def apply_product_guardrail(recommendation, status_name, category_name, available_stock=None):
    recommendation = max(Decimal(recommendation), ZERO)
    no_incoming = status_name in NO_INCOMING_STATUSES or category_name in NO_INCOMING_CATEGORIES
    if no_incoming:
        if available_stock is None:
            raise ValidationError(
                f"Available stock wajib untuk guardrail {status_name or category_name}."
            )
        recommendation = min(recommendation, max(Decimal(available_stock), ZERO))
    return recommendation


def incoming_calculation(final_sales_projection, prior_ending_qty, target_stock_ratio=None):
    projection = Decimal(final_sales_projection)
    prior_ending = Decimal(prior_ending_qty)
    if projection < 0:
        raise ValidationError("Final Sales Projection tidak boleh negatif.")
    minimum = max(projection - prior_ending, ZERO)
    if target_stock_ratio is None:
        return {"minimum": minimum, "recommended": minimum, "desired_beginning": projection}
    ratio = Decimal(target_stock_ratio)
    if ratio <= 0:
        raise ValidationError("Target Stock Ratio Incoming harus lebih besar dari nol.")
    desired_beginning = projection * ratio
    recommended = max(desired_beginning - prior_ending, ZERO)
    return {
        "minimum": minimum,
        "recommended": recommended,
        "desired_beginning": desired_beginning,
    }


def select_effective_rule(rules, sku):
    matching = []
    for rule in rules:
        if rule.scope_type == ProjectionRule.ScopeType.PRODUCT and rule.product_id == sku.product_variant.product_id:
            matching.append(rule)
        elif rule.scope_type == ProjectionRule.ScopeType.CATEGORY and rule.category_id == sku.product_variant.product.category_id:
            matching.append(rule)
        elif (
            rule.scope_type == ProjectionRule.ScopeType.PRODUCT_STATUS
            and rule.product_status_id == sku.product_variant.product.status_id
        ):
            matching.append(rule)
    if not matching:
        return None, []
    matching.sort(key=lambda item: (item.priority, item.created_at), reverse=True)
    return matching[0], matching[1:]
