from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum

from inventory.models import FIFOAllocation, FIFOOpeningSnapshot, FIFOLayer, InventoryException, InventoryMovement
from master_data.models import SKU


ZERO = Decimal("0")


def _for_warehouse(queryset, warehouse):
    if warehouse is None:
        return queryset
    if warehouse.code == "MAIN":
        return queryset.filter(Q(warehouse=warehouse) | Q(warehouse__isnull=True))
    return queryset.filter(warehouse=warehouse)


def filtered_skus(*, query="", status="", category="", product=(), size=()):
    rows = SKU.objects.filter(is_active=True).select_related(
        "product_variant__product__status",
        "product_variant__product__category",
        "product_variant__product__subcategory",
    )
    if query:
        rows = rows.filter(
            Q(sku__icontains=query)
            | Q(product_variant__product__name__icontains=query)
            | Q(product_variant__product__parent_sku__icontains=query)
        )
    if status:
        rows = rows.filter(product_variant__product__status_id=status)
    if category:
        rows = rows.filter(product_variant__product__category_id=category)
    if product:
        rows = rows.filter(product_variant__product_id__in=product)
    if size:
        rows = rows.filter(size__in=size)
    return rows.order_by("sku")


def inventory_summary_rows(skus, *, as_of_date=None, warehouse=None):
    skus = list(skus)
    sku_ids = [row.id for row in skus]
    opening_rows = FIFOOpeningSnapshot.objects.filter(sku_id__in=sku_ids)
    if warehouse is not None and warehouse.code != "MAIN":
        opening_rows = opening_rows.none()
    if as_of_date:
        opening_rows = opening_rows.filter(cutover_date__lte=as_of_date)
    openings = {
        row["sku_id"]: row
        for row in opening_rows
        .values("sku_id", "opening_qty", "frozen_unit_cogs", "cutover_date")
    }
    movements = InventoryMovement.objects.filter(sku_id__in=sku_ids).exclude(
        movement_type=InventoryMovement.MovementType.OPENING
    )
    movements = _for_warehouse(movements, warehouse)
    if as_of_date:
        movements = movements.filter(movement_date__lte=as_of_date)
    movement_totals = {
        row["sku_id"]: row
        for row in movements.values("sku_id").annotate(
            incoming_qty=Sum("quantity", filter=Q(direction=InventoryMovement.Direction.IN)),
            outgoing_qty=Sum("quantity", filter=Q(direction=InventoryMovement.Direction.OUT)),
            incoming_cost=Sum("allocated_cost", filter=Q(direction=InventoryMovement.Direction.IN)),
            outgoing_cost=Sum("allocated_cost", filter=Q(direction=InventoryMovement.Direction.OUT)),
        )
    }
    if warehouse is not None and warehouse.code == "REJECT":
        fifo = {sku_id: {"fifo_qty": ZERO, "fifo_value": ZERO} for sku_id in sku_ids}
    elif as_of_date:
        allocated_out = {
            row["outbound_movement__sku_id"]: row["allocated_qty"] or ZERO
            for row in FIFOAllocation.objects.filter(
                outbound_movement__sku_id__in=sku_ids,
                outbound_movement__movement_date__lte=as_of_date,
            )
            .values("outbound_movement__sku_id")
            .annotate(allocated_qty=Sum("allocated_qty"))
        }
        fifo = {}
        for sku_id in sku_ids:
            opening = openings.get(sku_id, {})
            opening_qty = opening.get("opening_qty", ZERO) or ZERO
            opening_layer_qty = max(opening_qty, ZERO)
            opening_value = opening_layer_qty * (opening.get("frozen_unit_cogs", ZERO) or ZERO)
            totals = movement_totals.get(sku_id, {})
            incoming_qty = totals.get("incoming_qty", ZERO) or ZERO
            incoming_cost = totals.get("incoming_cost", ZERO) or ZERO
            outgoing_cost = totals.get("outgoing_cost", ZERO) or ZERO
            fifo[sku_id] = {
                "fifo_qty": max(opening_layer_qty + incoming_qty - allocated_out.get(sku_id, ZERO), ZERO),
                "fifo_value": max(opening_value + incoming_cost - outgoing_cost, ZERO),
            }
    else:
        fifo_value_expression = ExpressionWrapper(
            F("remaining_qty") * F("unit_cost"),
            output_field=DecimalField(max_digits=24, decimal_places=4),
        )
        fifo = {
            row["sku_id"]: row
            for row in FIFOLayer.objects.filter(sku_id__in=sku_ids)
            .values("sku_id")
            .annotate(fifo_qty=Sum("remaining_qty"), fifo_value=Sum(fifo_value_expression))
        }
    open_exceptions = {} if warehouse is not None and warehouse.code == "REJECT" else {
        row["sku_id"]: row["count"]
        for row in InventoryException.objects.filter(
            sku_id__in=sku_ids, status=InventoryException.Status.OPEN
        )
        .values("sku_id")
        .annotate(count=Count("id"))
    }

    rows = []
    for sku in skus:
        opening = openings.get(sku.id, {})
        opening_qty = opening.get("opening_qty", ZERO) or ZERO
        totals = movement_totals.get(sku.id, {})
        incoming = totals.get("incoming_qty", ZERO) or ZERO
        outgoing = totals.get("outgoing_qty", ZERO) or ZERO
        balance = opening_qty + incoming - outgoing
        fifo_row = fifo.get(sku.id, {})
        fifo_qty = fifo_row.get("fifo_qty", ZERO) or ZERO
        exception_count = open_exceptions.get(sku.id, 0)
        if warehouse is not None and warehouse.code == "REJECT" and balance > 0:
            stock_status = "REJECTED"
        elif balance < 0:
            stock_status = "NEGATIVE"
        elif exception_count or balance != fifo_qty:
            stock_status = "EXCEPTION"
        elif balance == 0:
            stock_status = "ZERO"
        else:
            stock_status = "OK"
        rows.append(
            {
                "sku": sku,
                "opening_qty": opening_qty,
                "incoming_qty": incoming,
                "outgoing_qty": outgoing,
                "balance": balance,
                "fifo_qty": fifo_qty,
                "fifo_value": fifo_row.get("fifo_value", ZERO) or ZERO,
                "exception_count": exception_count,
                "stock_status": stock_status,
            }
        )
    return rows


def inventory_parent_summary_rows(rows):
    grouped = {}
    for row in rows:
        product = row["sku"].product_variant.product
        parent_sku = product.parent_sku.strip()
        group_key = ("parent", parent_sku) if parent_sku else ("sku", row["sku"].id)
        group = grouped.setdefault(
            group_key,
            {
                "parent_sku": parent_sku or row["sku"].sku,
                "product": product,
                "sku_count": 0,
                "opening_qty": ZERO,
                "incoming_qty": ZERO,
                "outgoing_qty": ZERO,
                "balance": ZERO,
                "fifo_qty": ZERO,
                "fifo_value": ZERO,
                "exception_count": 0,
                "child_statuses": [],
            },
        )
        group["sku_count"] += 1
        for field in ("opening_qty", "incoming_qty", "outgoing_qty", "balance", "fifo_qty", "fifo_value"):
            group[field] += row[field]
        group["exception_count"] += row["exception_count"]
        group["child_statuses"].append(row["stock_status"])

    result = []
    for group in grouped.values():
        child_statuses = group.pop("child_statuses")
        if "NEGATIVE" in child_statuses:
            group["stock_status"] = "NEGATIVE"
        elif "EXCEPTION" in child_statuses:
            group["stock_status"] = "EXCEPTION"
        elif "REJECTED" in child_statuses:
            group["stock_status"] = "REJECTED"
        elif group["balance"] == 0:
            group["stock_status"] = "ZERO"
        else:
            group["stock_status"] = "OK"
        result.append(group)
    return sorted(result, key=lambda row: (row["parent_sku"], row["product"].name))


def movement_ledger_rows(skus, *, date_from=None, date_to=None, movement_type="", warehouse=None):
    skus = list(skus)
    sku_ids = [row.id for row in skus]
    running = defaultdict(lambda: ZERO)
    running_value = defaultdict(lambda: ZERO)
    ledger = []
    opening_rows = FIFOOpeningSnapshot.objects.filter(sku_id__in=sku_ids).select_related(
        "sku__product_variant__product"
    )
    if warehouse is not None and warehouse.code != "MAIN":
        opening_rows = opening_rows.none()
    for opening in opening_rows:
        running[opening.sku_id] = opening.opening_qty
        running_value[opening.sku_id] = max(opening.opening_qty, ZERO) * opening.frozen_unit_cogs
        ledger.append(
            {
                "date": opening.cutover_date,
                "posted_at": opening.recorded_at,
                "type": "OPENING",
                "type_label": "Opening EOD 31 July",
                "direction": "IN" if opening.opening_qty >= 0 else "OUT",
                "sku": opening.sku,
                "quantity": abs(opening.opening_qty),
                "signed_quantity": opening.opening_qty,
                "allocated_cost": opening.opening_qty * opening.frozen_unit_cogs,
                "reference": "FIFO Opening EOD 2026-07-31",
                "key": f"OPENING|20260731|{opening.sku.sku}",
                "running_balance": opening.opening_qty,
                "running_value": running_value[opening.sku_id],
                "value_delta": running_value[opening.sku_id],
                "warehouse_name": warehouse.name if warehouse else "Main Warehouse",
            }
        )
    movements = InventoryMovement.objects.filter(sku_id__in=sku_ids).exclude(
        movement_type=InventoryMovement.MovementType.OPENING
    ).select_related("sku__product_variant__product", "warehouse", "posted_by")
    movements = _for_warehouse(movements, warehouse)
    if date_to:
        movements = movements.filter(movement_date__lte=date_to)
    for movement in movements.order_by("movement_date", "posted_at", "movement_key"):
        signed = movement.quantity if movement.direction == InventoryMovement.Direction.IN else -movement.quantity
        running[movement.sku_id] += signed
        signed_cost = movement.allocated_cost if movement.direction == InventoryMovement.Direction.IN else -movement.allocated_cost
        running_value[movement.sku_id] += signed_cost
        ledger.append(
            {
                "date": movement.movement_date,
                "posted_at": movement.posted_at,
                "type": movement.movement_type,
                "type_label": movement.get_movement_type_display(),
                "direction": movement.direction,
                "sku": movement.sku,
                "quantity": movement.quantity,
                "signed_quantity": signed,
                "allocated_cost": movement.allocated_cost,
                "reference": movement.source_reference,
                "key": movement.movement_key,
                "running_balance": running[movement.sku_id],
                "running_value": running_value[movement.sku_id],
                "value_delta": signed_cost,
                "warehouse_name": movement.warehouse.name if movement.warehouse else "Main Warehouse",
            }
        )
    ledger.sort(key=lambda row: (row["date"], row["posted_at"], row["key"]))
    if date_from:
        ledger = [row for row in ledger if row["date"] >= date_from]
    if date_to:
        ledger = [row for row in ledger if row["date"] <= date_to]
    if movement_type:
        ledger = [row for row in ledger if row["type"] == movement_type]
    return ledger


def parent_movement_ledger_rows(rows):
    grouped = {}
    for row in rows:
        product = row["sku"].product_variant.product
        parent_sku = product.parent_sku or product.code
        key = (row["date"], product.id, row["type"], row["reference"])
        group = grouped.setdefault(key, {
            "date": row["date"],
            "posted_at": row["posted_at"],
            "type": row["type"],
            "type_label": row["type_label"],
            "product_id": product.id,
            "parent_sku": parent_sku,
            "quantity": ZERO,
            "signed_quantity": ZERO,
            "allocated_cost": ZERO,
            "value_delta": ZERO,
            "reference": row["reference"],
            "warehouse_name": row["warehouse_name"],
            "sku_ids": set(),
        })
        group["posted_at"] = min(group["posted_at"], row["posted_at"])
        group["quantity"] += row["quantity"]
        group["signed_quantity"] += row["signed_quantity"]
        group["allocated_cost"] += row["allocated_cost"]
        group["value_delta"] += row["value_delta"]
        group["sku_ids"].add(row["sku"].id)

    running_qty = defaultdict(lambda: ZERO)
    running_value = defaultdict(lambda: ZERO)
    result = []
    for row in sorted(grouped.values(), key=lambda item: (item["date"], item["posted_at"], item["type"], item["reference"], item["parent_sku"])):
        running_qty[row["product_id"]] += row["signed_quantity"]
        running_value[row["product_id"]] += row["value_delta"]
        row["direction"] = "IN" if row["signed_quantity"] >= 0 else "OUT"
        row["running_balance"] = running_qty[row["product_id"]]
        row["running_value"] = running_value[row["product_id"]]
        row["key"] = f"SUMMARY|{row['date']:%Y%m%d}|{row['parent_sku']}|{row['type']}|{len(row['sku_ids'])}SKU"
        result.append(row)
    return result
