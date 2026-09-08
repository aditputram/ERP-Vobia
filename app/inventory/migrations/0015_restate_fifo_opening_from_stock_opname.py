import csv
import hashlib
import io
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db import migrations
from django.utils import timezone


SOURCE_FILE = "VOBIA-Inventory-all-2026-09-08.numbers"
SOURCE_FILE_SHA256 = "197c477a2c6174286a0f86c6ec0d5155ed90c87be001084c5ea7d69e6ab142b1"
NORMALIZED_XLSX_SHA256 = "6948f4b08284c4f5935a6e4944137da644f6c5443a16ea6f617e150e8d53b0b5"
TARGET_FINGERPRINT = "d61247b1970af018718489a268e377ad4334883d5e54a4876ac76c9caacf884a"
CUTOVER_DATE = date(2026, 7, 31)
TAG = f"[OPENING-RESTATEMENT:{SOURCE_FILE_SHA256}]"

TARGET_CSV = """sku,qty
S100,0
S105,0
S106,0
S108,332
S109,383
S113,92
S115,78
S116,0
S117,25
S119,0
S15,69
S16,216
S17,206
S18,16
S19,0
S20,235
S22,0
S23,317
S36,147
S37,273
S4,0
S47,0
S49,0
S57,1146
S6,126
S65,245
S66,249
S67,229
S8,13
S82,48
S83,0
S84,266
S88,0
S90,247
S91,0
S92,32
S95,0
S96,180
S98,0
ST345,0
VH1,0
VH5,0
VOBH15,27
VOBH17,0
VOBH20,289
VOBO16.L,28
VOBO16.M,150
VOBO16.XL,23
VOBO17.L,28
VOBO17.M,97
VOBO17.XL,14
VOBO20.L,0
VOBO20.M,0
VOBO29C.L,42
VOBO29C.M,45
VOBO29C.XL,0
VOBO31.L,0
VOBO31.M,0
VOBO31.XL,0
VOBO32.XL,0
VOBO33.L,43
VOBO33.M,64
VOBO33.XL,17
VOBO34.L,46
VOBO34.M,59
VOBO34.XL,25
VOBO35.L,53
VOBO35.M,105
VOBO35.XL,0
VOBO36.L,63
VOBO36.M,127
VOBO36.XL,12
VOBO37.L,16
VOBO37.M,39
VOBO37.XL,30
VOBO38.L,97
VOBO38.M,87
VOBO38.XL,55
VOBO39.L,191
VOBO39.M,351
VOBO39.XL,73
VOBO40.L,0
VOBO40.M,0
VOBO40.XL,0
VOBO41.L,0
VOBO42.L,89
VOBO42.M,77
VOBO43.L,47
VOBO43.M,46
VOBO43.XL,28
VOBO44B.XL,0
VOBO47.L,0
VOBO47.M,0
VOBO49.L,9
VOBO49.M,132
VOBO49.XL,10
VOBO50.L,88
VOBO50.M,107
VOBO50.XL,48
VOBO51.L,25
VOBO51.M,42
VOBO51.XL,4
VOBO52.L,58
VOBO52.M,93
VOBO52.XL,27
VOBO53.L,36
VOBO53.M,56
VOBO53.XL,16
VOBO54.L,24
VOBO54.M,70
VOBO54.XL,89
VOBO55.L,4
VOBO55.M,32
VOBO55.XL,13
VOBO57.L,0
VOBO57.M,0
VOBO57.XL,0
VOBO58.L,90
VOBO58.M,135
VOBO58.XL,68
VOBO59.L,27
VOBO59.M,112
VOBO59.XL,70
VOBO60.L,112
VOBO60.M,85
VOBO60.XL,13
VOBO61.L,100
VOBO61.M,161
VOBO61.XL,52
VOBO62.L,9
VOBO62.M,0
VOBO63.M,0
VOBO63.XL,4
VOBO67.L,150
VOBO67.M,146
VOBO67.XL,65
VOBO68.L,27
VOBO68.M,48
VOBO68.XL,9
VOBO69.M,82
VOBO69.XL,10
VOBO70.L,8
VOBO70.M,0
VOBO70.XL,0
VOBO71.L,42
VOBO71.M,29
VOBO71.XL,16
VOBO72.L,47
VOBO72.M,12
VOBO72.XL,27
VOBO73.L,86
VOBO73.M,42
VOBO73.XL,25
VOBO74.L,35
VOBO74.M,25
VOBO74.XL,22
VOBO75.L,73
VOBO75.M,62
VOBO75.XL,53
VOBO76.L,58
VOBO76.M,48
VOBO76.XL,30
VOBO77.L,208
VOBO77.M,219
VOBO77.XL,120
VOBO78.L,71
VOBO78.M,77
VOBO78.XL,13
VOBO79.L,19
VOBO79.M,36
VOBO79.XL,21
VOBO82.L,0
VOBO82.M,10
VOBO90.L,37
VOBO90.M,44
VOBO90.XL,1
VOBO91.L,0
VOBO91.M,0
VOBO91.XL,0
VOBO92.L,0
VOBO92.M,0
VOBO92.XL,0
VOBO93.L,0
VOBO93.XL,0
VOBP10.28,128
VOBP10.30,44
VOBP10.32,44
VOBP10.34,0
VOBP11.28,40
VOBP11.30,39
VOBP11.32,22
VOBP11.34,31
VOBP12.28,69
VOBP12.30,28
VOBP12.32,18
VOBP12.34,30
VOBP13.28,116
VOBP13.30,98
VOBP13.32,91
VOBP13.34,33
VOBP14.28,54
VOBP14.30,34
VOBP14.32,18
VOBP14.34,10
VOBP15.28,90
VOBP15.30,72
VOBP15.32,28
VOBP15.34,0
VOBP16.28,0
VOBP16.30,0
VOBP17.28,19
VOBP17.30,0
VOBP17.32,12
VOBP17.34,16
VOBP18.28,6
VOBP18.30,0
VOBP18.32,13
VOBP18.34,4
VOBP19.28,22
VOBP19.30,0
VOBP19.32,0
VOBP19.34,8
VOBP20.28,15
VOBP20.30,16
VOBP20.32,11
VOBP20.34,11
VOBP21.28,10
VOBP21.30,14
VOBP21.32,8
VOBP21.34,6
VOBP25.28,112
VOBP25.30,99
VOBP25.32,48
VOBP25.34,17
VOBP26.28,0
VOBP26.30,14
VOBP26.32,7
VOBP26.34,6
VOBP2A.L,1
VOBP2A.M,0
VOBP2B.L,0
VOBP2B.M,0
VOBP2B.XL,0
VOBP2I.M,0
VOBP3A.30,0
VOBP3A.32,0
VOBP3A.34,1
VOBP3B.28,178
VOBP3B.30,96
VOBP3B.32,69
VOBP3B.34,14
VOBP3K.28,103
VOBP3K.30,47
VOBP3K.32,36
VOBP3K.34,32
VOBP4B.28,152
VOBP4B.30,135
VOBP4B.32,35
VOBP4B.34,30
VOBP4JB.28,41
VOBP4JB.30,86
VOBP4JB.32,37
VOBP4JB.34,10
VOBP5A.L,13
VOBP5A.M,41
VOBP5AR.M,0
VOBP5A.XL,12
VOBP5B.L,19
VOBP5B.M,42
VOBP5B.XL,60
VOBP5G.M,0
VOBP5G.XL,0
VOBP6B.L,35
VOBP6B.M,114
VOBP6B.XL,40
VOBP6G.L,73
VOBP6G.M,193
VOBP6G.XL,94
VOBP6H.L,0
VOBP6H.M,0
VOBP6H.XL,0
VOBP7BW.L,88
VOBP7BW.M,143
VOBP7BW.XL,42
VOBP8BT.28,0
VOBP8BT.30,0
VOBP8BT.32,0
VOBP8C.32,0
VOBP9K.2,1
VOBPBG1,9333
VOBSH10.L,2
VOBSH10.M,30
VOBSH10.XL,0
VOBSH11.L,44
VOBSH11.M,76
VOBSH11.XL,19
VOBSH12.L,66
VOBSH12.M,107
VOBSH12.XL,47
VOBSH13.L,48
VOBSH13.M,100
VOBSH13.XL,46
VOBSH15.L,72
VOBSH15.M,165
VOBSH15.XL,40
VOBSH16.L,40
VOBSH16.M,88
VOBSH16.XL,15
VOBSH17.L,65
VOBSH17.M,176
VOBSH17.XL,35
VOBSH18.L,101
VOBSH18.M,233
VOBSH18.XL,89
VOBSH19.L,164
VOBSH19.M,245
VOBSH19.XL,73
VOBSH2.0.L,54
VOBSH20.L,155
VOBSH2.0.M,58
VOBSH20.M,177
VOBSH2.0.XL,4
VOBSH20.XL,38
VOBSH24.L,0
VOBSH24.M,0
VOBSH24.XL,1
VOBSH25.L,8
VOBSH25.M,18
VOBSH25.XL,1
VOBSH26.L,41
VOBSH26.M,50
VOBSH26.XL,8
VOBSH27.L,41
VOBSH27.M,48
VOBSH27.XL,1
VOBSH28.L,85
VOBSH28.M,275
VOBSH28.XL,64
VOBSH29.L,59
VOBSH29.M,191
VOBSH29.XL,43
VOBSH30.L,0
VOBSH30.M,0
VOBSH30.XL,0
VOBSH31.L,33
VOBSH31.M,50
VOBSH31.XL,23
VOBSH32.L,35
VOBSH32.M,43
VOBSH32.XL,10
VOBSH33.L,1
VOBSH33.M,0
VOBSH34.L,35
VOBSH34.M,13
VOBSH34.XL,37
VOBSH35.L,54
VOBSH35.M,2
VOBSH35.XL,3
VOBSH36.L,26
VOBSH36.M,216
VOBSH36.XL,8
VOBSH37.L,81
VOBSH37.M,125
VOBSH37.XL,18
VOBSH38.L,113
VOBSH38.M,155
VOBSH38.XL,33
VOBSH39.L,57
VOBSH39.M,63
VOBSH39.XL,51
VOBSH40.L,44
VOBSH40.M,48
VOBSH40.XL,43
VOBSH41.L,3
VOBSH41.M,8
VOBSH41.XL,5
VOBSH42.L,0
VOBSH42.M,0
VOBSH43.L,0
VOBSH43.XL,0
VOBSH44.M,0
VOBSH44.XL,0
VOBSH45.L,20
VOBSH45.M,0
VOBSH45.XL,1
VOBSH46.L,57
VOBSH46.M,242
VOBSH46.XL,18
VOBSH47.L,7
VOBSH47.M,0
VOBSH47.XL,34
VOBSH48.L,46
VOBSH48.M,21
VOBSH48.XL,18
VOBSH49.L,0
VOBSH49.M,0
VOBSH49.XL,0
VOBSH50.L,0
VOBSH50.M,0
VOBSH50.XL,0
VOBSH51.L,0
VOBSH51.M,0
VOBSH51.XL,0
VOBSH52.L,64
VOBSH52.M,134
VOBSH52.XL,0
VOBSH53.L,24
VOBSH53.M,82
VOBSH53.XL,15
VOBSH55.L,88
VOBSH55.M,153
VOBSH55.XL,42
VOBSH57.L,0
VOBSH57.M,3
VOBSH57.XL,0
VOBSH60.L,0
VOBSH60.M,0
VOBSH60.XL,0
VOBSH61.L,0
VOBSH61.M,0
VOBSH61.XL,0
VOBSH62.L,0
VOBSH62.M,0
VOBSH62.XL,0
VOBSH63.L,289
VOBSH63.M,420
VOBSH63.XL,96
VOBSH64.L,56
VOBSH64.M,78
VOBSH64.XL,24
VOBSH65.L,0
VOBSH65.M,0
VOBSH65.XL,0
VOBSH66.L,179
VOBSH66.M,272
VOBSH66.XL,36
VOBSH67.L,0
VOBSH67.M,0
VOBSH67.XL,0
VOBSH68.L,63
VOBSH68.M,100
VOBSH68.XL,19
VOBSH69.L,59
VOBSH69.M,55
VOBSH69.XL,1
VOBSH6.L,237
VOBSH6.M,53
VOBSH6.XL,98
VOBSH70.L,90
VOBSH70.M,116
VOBSH70.XL,20
VOBSH71.L,102
VOBSH71.M,180
VOBSH71.XL,30
VOBSH7B.L,23
VOBSH7B.M,31
VOBSH7B.XL,15
VOBSH7W.L,73
VOBSH7W.M,32
VOBSH7W.XL,37
VOBTS41.L,16
VOBTS41.M,77
VOBTS41.XL,3
VOBTS50.L,32
VOBTS50.M,75
VOBTS50.XL,7
VOBTS51.L,53
VOBTS51.M,88
VOBTS51.XL,11
VOBTS57.L,86
VOBTS57.M,92
VOBTS57.XL,5
VOBTS61.L,103
VOBTS61.M,95
VOBTS61.XL,14
VOBTS62.L,134
VOBTS62.M,185
VOBTS62.XL,73
VOBTS63.L,98
VOBTS63.M,118
VOBTS63.XL,26
VOBTS64.L,51
VOBTS64.M,92
VOBTS64.XL,20
VOBTS65.L,33
VOBTS65.M,62
VOBTS65.XL,8
VOBTS66.L,5
VOBTS66.M,0
VOBTS66.XL,6
VOBTS67.L,117
VOBTS67.M,131
VOBTS67.XL,32
VOBTS68.L,0
VOBTS68.M,0
VOBTS68.XL,1
VOBTS69.L,78
VOBTS69.M,165
VOBTS69.XL,33
VOBTS70.L,53
VOBTS70.M,145
VOBTS70.XL,35
VOBTS71.L,84
VOBTS71.M,180
VOBTS71.XL,38
VOBTS72.L,64
VOBTS72.M,132
VOBTS72.XL,13
VOBTS73.L,143
VOBTS73.M,284
VOBTS73.XL,62
VOBTS74.L,62
VOBTS74.M,141
VOBTS74.XL,39
VOBTS75.L,37
VOBTS75.M,63
VOBTS75.XL,23
VOBTS76.L,0
VOBTS76.M,0
VOBTS77.L,51
VOBTS77.M,71
VOBTS77.XL,25
VOBTS82.L,48
VOBTS82.M,71
VOBTS82.XL,24
VOBTS83.L,45
VOBTS83.M,71
VOBTS83.XL,24
VOBTS84.L,43
VOBTS84.M,64
VOBTS84.XL,21
VOBTS85.L,47
VOBTS85.M,69
VOBTS85.XL,24
VOBTS86.L,0
VOBTS86.M,0
VOBTS86.XL,0
VOBTS87.L,0
VOBTS87.M,0
VOC1,60
VOC16,13
VOC17,4
VOC18,0
VOC19,68
VOC2,12
VOC20,30
VOC21,11
VOC25,63
VOC26,26
VOC27,6
VOC28,62
VOC29,21
VOC30,0
VS1,0
VS34C.M,97
VS35C.L,47
VS36C.XL,10
VS7,28
"""


def _target():
    rows = list(csv.DictReader(io.StringIO(TARGET_CSV)))
    values = {row["sku"].strip(): Decimal(row["qty"]) for row in rows}
    if len(values) != len(rows):
        raise RuntimeError("SKU target duplikat.")
    return values


def _target_fingerprint(values):
    payload = "\n".join(f"{sku}|{int(qty)}" for sku, qty in sorted(values.items()))
    return hashlib.sha256(payload.encode()).hexdigest()


def _ledger_fingerprint(Movement, database):
    fields = (
        "id",
        "movement_key",
        "movement_date",
        "movement_type",
        "direction",
        "sku_id",
        "warehouse_id",
        "quantity",
        "sales_line_id",
        "inbound_receipt_id",
        "return_receipt_id",
    )
    payload = "\n".join(
        "|".join("" if value is None else str(value) for value in row)
        for row in Movement.objects.using(database)
        .exclude(movement_type="OPENING")
        .order_by("movement_date", "posted_at", "movement_key")
        .values_list(*fields)
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _opening_fingerprint(snapshots, quantities=None):
    payload = "\n".join(
        f"{row.sku.sku}|{quantities.get(row.sku.sku, row.opening_qty) if quantities else row.opening_qty}|"
        f"{row.frozen_unit_cogs}|{row.cutover_date}"
        for row in sorted(snapshots, key=lambda item: item.sku.sku)
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _exception_state(rows):
    return [
        {
            "id": str(row.id),
            "code": row.code,
            "status": row.status,
            "sku_id": str(row.sku_id),
            "movement_id": str(row.movement_id) if row.movement_id else None,
            "quantity": str(row.quantity),
            "message": row.message,
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
            "resolved_by_id": str(row.resolved_by_id) if row.resolved_by_id else None,
            "resolution_movement_id": str(row.resolution_movement_id) if row.resolution_movement_id else None,
            "resolution_reason": row.resolution_reason,
        }
        for row in rows
    ]


def _rebuild_fifo(apps, database, quantities):
    Snapshot = apps.get_model("inventory", "FIFOOpeningSnapshot")
    Movement = apps.get_model("inventory", "InventoryMovement")
    Layer = apps.get_model("inventory", "FIFOLayer")
    Allocation = apps.get_model("inventory", "FIFOAllocation")
    ReturnReceipt = apps.get_model("inventory", "PhysicalReturnReceipt")

    snapshots = list(
        Snapshot.objects.using(database)
        .select_for_update()
        .select_related("sku")
        .order_by("sku__sku")
    )
    snapshot_by_sku = {row.sku.sku: row for row in snapshots}
    if set(quantities) != set(snapshot_by_sku):
        raise RuntimeError("Cakupan target FIFO tidak sama dengan seluruh snapshot.")
    if any(qty != qty.to_integral_value() for qty in quantities.values()):
        raise RuntimeError("Opening wajib bilangan bulat.")

    Allocation.objects.using(database).all().delete()

    opening_movements = {
        row.sku_id: row
        for row in Movement.objects.using(database)
        .select_for_update()
        .filter(movement_type="OPENING")
    }
    opening_layers = {
        row.sku_id: row
        for row in Layer.objects.using(database)
        .select_for_update()
        .filter(source_type="OPENING")
    }
    if set(opening_movements) != set(opening_layers):
        raise RuntimeError("Opening movement dan opening layer tidak seimbang.")

    for snapshot in snapshots:
        qty = quantities[snapshot.sku.sku]
        Snapshot.objects.using(database).filter(pk=snapshot.pk).update(opening_qty=qty)
        movement = opening_movements.get(snapshot.sku_id)
        layer = opening_layers.get(snapshot.sku_id)
        if qty > 0:
            if movement is None:
                movement = Movement.objects.using(database).create(
                    movement_key=f"OPENING|20260731|{snapshot.sku.sku}",
                    movement_date=CUTOVER_DATE,
                    movement_type="OPENING",
                    direction="IN",
                    sku_id=snapshot.sku_id,
                    quantity=qty,
                    allocated_cost=qty * snapshot.frozen_unit_cogs,
                    source_reference="FIFO Opening EOD 2026-07-31",
                    reason="Restatement stock opname fisik EOD 31 July 2026",
                    posted_by_id=snapshot.recorded_by_id,
                )
            else:
                Movement.objects.using(database).filter(pk=movement.pk).update(
                    quantity=qty,
                    allocated_cost=qty * snapshot.frozen_unit_cogs,
                )
            if layer is None:
                Layer.objects.using(database).create(
                    layer_key=f"OPENING|20260731|{snapshot.sku.sku}",
                    sku_id=snapshot.sku_id,
                    source_type="OPENING",
                    source_reference="Opening",
                    receipt_date=CUTOVER_DATE,
                    original_qty=qty,
                    remaining_qty=qty,
                    unit_cost=snapshot.frozen_unit_cogs,
                    opening_movement_id=movement.id,
                )
            else:
                Layer.objects.using(database).filter(pk=layer.pk).update(
                    original_qty=qty,
                    remaining_qty=qty,
                    unit_cost=snapshot.frozen_unit_cogs,
                    opening_movement_id=movement.id,
                )
        else:
            if layer is not None:
                Layer.objects.using(database).filter(pk=layer.pk).delete()
            if movement is not None:
                Movement.objects.using(database).filter(pk=movement.pk).delete()

    layers = list(
        Layer.objects.using(database)
        .select_for_update()
        .order_by("receipt_date", "created_at", "layer_key")
    )
    for layer in layers:
        layer.remaining_qty = layer.original_qty
    Layer.objects.using(database).bulk_update(layers, ["remaining_qty"])

    events = list(
        Movement.objects.using(database)
        .exclude(movement_type="OPENING")
        .order_by("movement_date", "posted_at", "movement_key")
    )
    event_order = {
        row.id: (row.movement_date, row.posted_at, row.movement_key)
        for row in events
    }
    layers_by_sku = defaultdict(list)
    layer_by_id = {}
    for layer in layers:
        layers_by_sku[layer.sku_id].append(layer)
        layer_by_id[layer.id] = layer

    returns = {
        row.id: row.sales_line_id
        for row in ReturnReceipt.objects.using(database).filter(
            id__in=[event.return_receipt_id for event in events if event.return_receipt_id]
        )
    }
    sales_movement_by_line = {
        row.sales_line_id: row
        for row in events
        if row.movement_type == "SALES_OUT" and row.sales_line_id
    }
    allocations = []
    allocations_by_movement = defaultdict(list)
    shortages = []

    for event in events:
        order = event_order[event.id]
        if event.direction == "OUT":
            needed = Decimal(event.quantity)
            total_cost = Decimal("0")
            for layer in layers_by_sku[event.sku_id]:
                if needed <= 0:
                    break
                source_order = event_order.get(layer.opening_movement_id)
                available = layer.receipt_date < event.movement_date or (
                    layer.receipt_date == event.movement_date
                    and (layer.source_type == "OPENING" or source_order is None or source_order <= order)
                )
                if not available or layer.remaining_qty <= 0:
                    continue
                qty = min(needed, layer.remaining_qty)
                allocation = Allocation(
                    outbound_movement_id=event.id,
                    layer_id=layer.id,
                    allocated_qty=qty,
                    unit_cost=layer.unit_cost,
                    allocated_cost=qty * layer.unit_cost,
                    returned_qty=Decimal("0"),
                )
                allocations.append(allocation)
                allocations_by_movement[event.id].append(allocation)
                layer.remaining_qty -= qty
                needed -= qty
                total_cost += allocation.allocated_cost
            event.allocated_cost = total_cost
            if needed > 0:
                shortages.append((event, needed))
        elif event.movement_type == "RETURN_IN":
            needed = Decimal(event.quantity)
            restored_cost = Decimal("0")
            sales_line_id = returns.get(event.return_receipt_id)
            source = sales_movement_by_line.get(sales_line_id)
            if source is None:
                raise RuntimeError(f"Return {event.movement_key} tidak memiliki Sales Out asal.")
            for allocation in allocations_by_movement[source.id]:
                if needed <= 0:
                    break
                restorable = allocation.allocated_qty - allocation.returned_qty
                qty = min(needed, restorable)
                if qty <= 0:
                    continue
                allocation.returned_qty += qty
                layer_by_id[allocation.layer_id].remaining_qty += qty
                needed -= qty
                restored_cost += qty * allocation.unit_cost
            if needed > 0:
                raise RuntimeError(f"Return {event.movement_key} melebihi alokasi Sales Out asal.")
            event.allocated_cost = restored_cost

    Allocation.objects.using(database).bulk_create(allocations)
    Layer.objects.using(database).bulk_update(layers, ["remaining_qty"])
    changed_movements = [
        row for row in events if row.direction == "OUT" or row.movement_type == "RETURN_IN"
    ]
    Movement.objects.using(database).bulk_update(changed_movements, ["allocated_cost"])
    return shortages


def restate_opening(apps, schema_editor):
    database = schema_editor.connection.alias
    Snapshot = apps.get_model("inventory", "FIFOOpeningSnapshot")
    Movement = apps.get_model("inventory", "InventoryMovement")
    Layer = apps.get_model("inventory", "FIFOLayer")
    Allocation = apps.get_model("inventory", "FIFOAllocation")
    Exception = apps.get_model("inventory", "InventoryException")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    supplied = _target()
    if _target_fingerprint(supplied) != TARGET_FINGERPRINT:
        raise RuntimeError("Fingerprint target stock opname tidak cocok.")

    snapshots = list(
        Snapshot.objects.using(database)
        .select_for_update()
        .select_related("sku")
        .order_by("sku__sku")
    )
    if not snapshots:
        return
    if len(snapshots) != 746:
        raise RuntimeError(f"Baseline FIFO bukan 746 SKU ({len(snapshots)}).")
    snapshot_by_sku = {row.sku.sku: row for row in snapshots}
    missing = sorted(set(supplied) - set(snapshot_by_sku))
    if missing:
        raise RuntimeError("SKU target tidak ada di FIFO Opening: " + ", ".join(missing[:20]))
    omitted_nonzero = [
        row.sku.sku for row in snapshots
        if row.sku.sku not in supplied and row.opening_qty != 0
    ]
    if omitted_nonzero:
        raise RuntimeError("SKU nonzero tidak tercakup file: " + ", ".join(omitted_nonzero[:20]))

    target = {
        row.sku.sku: supplied.get(row.sku.sku, Decimal("0"))
        for row in snapshots
    }
    if sum(target.values(), Decimal("0")) != Decimal("39078"):
        raise RuntimeError("Total target bukan 39.078 pcs.")
    if all(row.opening_qty == target[row.sku.sku] for row in snapshots):
        return

    ledger_fingerprint = _ledger_fingerprint(Movement, database)
    before_opening_fingerprint = _opening_fingerprint(snapshots)
    exception_rows = list(
        Exception.objects.using(database)
        .select_for_update()
        .filter(code__in=["FIFO_SHORT_QTY", "NEGATIVE_OPENING"])
        .order_by("created_at", "id")
    )
    before_exceptions = _exception_state(exception_rows)
    before_openings = [
        {"sku": row.sku.sku, "quantity": str(row.opening_qty)}
        for row in snapshots
    ]
    before_opening_movements = [
        {
            "id": str(row.id),
            "movement_key": row.movement_key,
            "movement_date": row.movement_date.isoformat(),
            "direction": row.direction,
            "sku_id": str(row.sku_id),
            "warehouse_id": str(row.warehouse_id) if row.warehouse_id else None,
            "quantity": str(row.quantity),
            "allocated_cost": str(row.allocated_cost),
            "source_reference": row.source_reference,
            "reason": row.reason,
            "evidence_reference": row.evidence_reference,
            "posted_by_id": str(row.posted_by_id),
            "posted_at": row.posted_at.isoformat(),
        }
        for row in Movement.objects.using(database).filter(movement_type="OPENING")
    ]
    before_opening_layers = [
        {
            "id": str(row.id),
            "layer_key": row.layer_key,
            "sku_id": str(row.sku_id),
            "source_reference": row.source_reference,
            "receipt_date": row.receipt_date.isoformat(),
            "original_qty": str(row.original_qty),
            "remaining_qty": str(row.remaining_qty),
            "unit_cost": str(row.unit_cost),
            "opening_movement_id": str(row.opening_movement_id),
            "created_at": row.created_at.isoformat(),
        }
        for row in Layer.objects.using(database).filter(source_type="OPENING")
    ]
    before_layer_remaining = [
        {"id": str(row.id), "remaining_qty": str(row.remaining_qty)}
        for row in Layer.objects.using(database).all()
    ]
    before_allocations = [
        {
            "id": str(row.id),
            "outbound_movement_id": str(row.outbound_movement_id),
            "layer_id": str(row.layer_id),
            "allocated_qty": str(row.allocated_qty),
            "unit_cost": str(row.unit_cost),
            "allocated_cost": str(row.allocated_cost),
            "returned_qty": str(row.returned_qty),
        }
        for row in Allocation.objects.using(database).all()
    ]
    before_movement_costs = [
        {"id": str(row.id), "allocated_cost": str(row.allocated_cost)}
        for row in Movement.objects.using(database).exclude(movement_type="OPENING")
    ]
    now = timezone.now()
    Exception.objects.using(database).filter(
        code__in=["FIFO_SHORT_QTY", "NEGATIVE_OPENING"],
        status="OPEN",
    ).update(
        status="RESOLVED",
        resolved_at=now,
        resolved_by_id=None,
        resolution_movement_id=None,
        resolution_reason=f"Superseded by {TAG}",
    )

    shortages = _rebuild_fifo(apps, database, target)
    Exception.objects.using(database).bulk_create(
        [
            Exception(
                code="FIFO_SHORT_QTY",
                status="OPEN",
                sku_id=movement.sku_id,
                movement_id=movement.id,
                quantity=quantity,
                message=f"{TAG} Sales Out melebihi FIFO layer tersedia sebanyak {quantity} unit.",
            )
            for movement, quantity in shortages
        ]
    )

    final_snapshots = list(
        Snapshot.objects.using(database).select_related("sku").order_by("sku__sku")
    )
    if sum((row.opening_qty for row in final_snapshots), Decimal("0")) != Decimal("39078"):
        raise RuntimeError("Hasil restatement opening bukan 39.078 pcs.")
    if _ledger_fingerprint(Movement, database) != ledger_fingerprint:
        raise RuntimeError("Movement operasional berubah; restatement dibatalkan.")
    if any(row.opening_qty < 0 for row in final_snapshots):
        raise RuntimeError("Hasil restatement masih memiliki opening negatif.")

    AuditEvent.objects.using(database).create(
        actor=None,
        action="fifo_opening_restatement_committed",
        entity_type="inventory.fifoopeningsnapshot",
        entity_id=SOURCE_FILE_SHA256,
        reason="Koreksi FIFO Opening EOD 31 July 2026 dari hasil stock opname tim Operation.",
        before_values={
            "openings": before_openings,
            "exceptions": before_exceptions,
            "opening_movements": before_opening_movements,
            "opening_layers": before_opening_layers,
            "layer_remaining": before_layer_remaining,
            "allocations": before_allocations,
            "movement_costs": before_movement_costs,
        },
        after_values={
            "snapshot_count": len(final_snapshots),
            "opening_total": "39078",
            "positive_openings": sum(1 for row in final_snapshots if row.opening_qty > 0),
            "zero_openings": sum(1 for row in final_snapshots if row.opening_qty == 0),
            "negative_openings": 0,
            "fifo_short_open": len(shortages),
        },
        metadata={
            "source_file": SOURCE_FILE,
            "source_file_sha256": SOURCE_FILE_SHA256,
            "normalized_xlsx_sha256": NORMALIZED_XLSX_SHA256,
            "target_rows": len(supplied),
            "target_fingerprint": TARGET_FINGERPRINT,
            "before_opening_fingerprint": before_opening_fingerprint,
            "after_opening_fingerprint": _opening_fingerprint(final_snapshots),
            "ledger_fingerprint": ledger_fingerprint,
            "non_opening_movements_changed": False,
        },
    )


def rollback_opening(apps, schema_editor):
    database = schema_editor.connection.alias
    Snapshot = apps.get_model("inventory", "FIFOOpeningSnapshot")
    Movement = apps.get_model("inventory", "InventoryMovement")
    Layer = apps.get_model("inventory", "FIFOLayer")
    Allocation = apps.get_model("inventory", "FIFOAllocation")
    Exception = apps.get_model("inventory", "InventoryException")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    event = (
        AuditEvent.objects.using(database)
        .filter(
            action="fifo_opening_restatement_committed",
            entity_id=SOURCE_FILE_SHA256,
        )
        .order_by("-occurred_at")
        .first()
    )
    if not event:
        return
    if _ledger_fingerprint(Movement, database) != event.metadata["ledger_fingerprint"]:
        raise RuntimeError("Movement setelah restatement sudah berubah; rollback otomatis dibatalkan.")

    snapshots = list(
        Snapshot.objects.using(database)
        .select_for_update()
        .select_related("sku")
        .order_by("sku__sku")
    )
    supplied = _target()
    current_target = {
        row.sku.sku: supplied.get(row.sku.sku, Decimal("0"))
        for row in snapshots
    }
    if any(row.opening_qty != current_target[row.sku.sku] for row in snapshots):
        raise RuntimeError("Opening live bukan target restatement; rollback dibatalkan.")

    before_quantities = {
        row["sku"]: Decimal(row["quantity"])
        for row in event.before_values["openings"]
    }
    Exception.objects.using(database).filter(
        code="FIFO_SHORT_QTY",
        message__startswith=TAG,
    ).delete()
    Allocation.objects.using(database).all().delete()
    Layer.objects.using(database).filter(source_type="OPENING").delete()
    Movement.objects.using(database).filter(movement_type="OPENING").delete()
    for snapshot in snapshots:
        Snapshot.objects.using(database).filter(pk=snapshot.pk).update(
            opening_qty=before_quantities[snapshot.sku.sku]
        )
    for row in event.before_values["opening_movements"]:
        movement = Movement.objects.using(database).create(
            id=row["id"],
            movement_key=row["movement_key"],
            movement_date=row["movement_date"],
            movement_type="OPENING",
            direction=row["direction"],
            sku_id=row["sku_id"],
            warehouse_id=row["warehouse_id"],
            quantity=Decimal(row["quantity"]),
            allocated_cost=Decimal(row["allocated_cost"]),
            source_reference=row["source_reference"],
            reason=row["reason"],
            evidence_reference=row["evidence_reference"],
            posted_by_id=row["posted_by_id"],
        )
        Movement.objects.using(database).filter(pk=movement.pk).update(
            posted_at=row["posted_at"]
        )
    for row in event.before_values["opening_layers"]:
        layer = Layer.objects.using(database).create(
            id=row["id"],
            layer_key=row["layer_key"],
            sku_id=row["sku_id"],
            source_type="OPENING",
            source_reference=row["source_reference"],
            receipt_date=row["receipt_date"],
            original_qty=Decimal(row["original_qty"]),
            remaining_qty=Decimal(row["remaining_qty"]),
            unit_cost=Decimal(row["unit_cost"]),
            opening_movement_id=row["opening_movement_id"],
        )
        Layer.objects.using(database).filter(pk=layer.pk).update(
            created_at=row["created_at"]
        )
    for row in event.before_values["layer_remaining"]:
        Layer.objects.using(database).filter(pk=row["id"]).update(
            remaining_qty=Decimal(row["remaining_qty"])
        )
    Allocation.objects.using(database).bulk_create(
        [
            Allocation(
                id=row["id"],
                outbound_movement_id=row["outbound_movement_id"],
                layer_id=row["layer_id"],
                allocated_qty=Decimal(row["allocated_qty"]),
                unit_cost=Decimal(row["unit_cost"]),
                allocated_cost=Decimal(row["allocated_cost"]),
                returned_qty=Decimal(row["returned_qty"]),
            )
            for row in event.before_values["allocations"]
        ]
    )
    for row in event.before_values["movement_costs"]:
        Movement.objects.using(database).filter(pk=row["id"]).update(
            allocated_cost=Decimal(row["allocated_cost"])
        )

    for row in event.before_values["exceptions"]:
        Exception.objects.using(database).filter(pk=row["id"]).update(
            status=row["status"],
            quantity=Decimal(row["quantity"]),
            message=row["message"],
            resolved_at=row["resolved_at"],
            resolved_by_id=row["resolved_by_id"],
            resolution_movement_id=row["resolution_movement_id"],
            resolution_reason=row["resolution_reason"],
        )
    restored = list(
        Snapshot.objects.using(database).select_related("sku").order_by("sku__sku")
    )
    if _opening_fingerprint(restored) != event.metadata["before_opening_fingerprint"]:
        raise RuntimeError("Fingerprint opening hasil rollback tidak cocok.")
    if Allocation.objects.using(database).count() != len(event.before_values["allocations"]):
        raise RuntimeError("Jumlah FIFO allocation hasil rollback tidak cocok.")

    AuditEvent.objects.using(database).create(
        actor=None,
        action="fifo_opening_restatement_rolled_back",
        entity_type="inventory.fifoopeningsnapshot",
        entity_id=SOURCE_FILE_SHA256,
        reason="Rollback koreksi FIFO Opening EOD 31 July 2026.",
        before_values={"target_fingerprint": TARGET_FINGERPRINT},
        after_values={"opening_fingerprint": event.metadata["before_opening_fingerprint"]},
    )


_values = _target()
assert len(_values) == 558
assert sum(_values.values(), Decimal("0")) == Decimal("39078")
assert min(_values.values()) == 0
assert _target_fingerprint(_values) == TARGET_FINGERPRINT


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ("inventory", "0014_physicalreturnreceipt_follow_up_status"),
        ("purchasing", "0008_restate_legacy_po_wip"),
        ("audit", "0001_initial"),
    ]

    operations = [migrations.RunPython(restate_opening, rollback_opening)]
