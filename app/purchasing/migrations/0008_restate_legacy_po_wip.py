import csv
import hashlib
import io
from datetime import date
from decimal import Decimal

from django.db import migrations


SOURCE_FINGERPRINT = "a5b06794bfd1c95ed72b01766d62ae2df3da94f95b220a89615d0bbd96f56447"
TARGET_FINGERPRINT = "841cf67cdf3e4201653cce80e12874acadb09b041a9b87e3b22d1fb3dbd467e1"
TARGET_FILE = "PO WIP (1) (1).xlsx"
TARGET_FILE_SHA256 = "411899595df3412aedb2523f4de30233c04608e5d7a515f39222a37da5f16c61"

TARGET_CSV = """po_number,sku,qty,supplier
PO-VOB-06/26-081,VOBP16.28,80,Haris
PO-VOB-06/26-081,VOBP16.30,66,Haris
PO-VOB-06/26-081,VOBP16.32,40,Haris
PO-VOB-06/26-081,VOBP16.34,17,Haris
PO-VOB-06/26-084,VOBTS66.L,1,4 Brother
PO-VOB-06/26-087,VOBO39.L,3,Harmoni
PO-VOB-06/26-087,VOBO39.M,9,Harmoni
PO-VOB-06/26-088,VOBO73.M,32,Bayu
PO-VOB-06/26-088,VOBO77.L,9,Bayu
PO-VOB-06/26-088,VOBO77.M,40,Bayu
PO-VOB-06/26-088,VOBO77.XL,12,Bayu
PO-VOB-06/26-090,VOBSH12.L,10,4 Brother
PO-VOB-06/26-090,VOBSH12.M,30,4 Brother
PO-VOB-06/26-090,VOBSH13.L,130,4 Brother
PO-VOB-06/26-090,VOBSH13.M,221,4 Brother
PO-VOB-06/26-093,VOBTS41.L,22,4 Brother
PO-VOB-06/26-093,VOBTS41.M,33,4 Brother
PO-VOB-06/26-093,VOBTS41.XL,14,4 Brother
PO-VOB-06/26-093,VOBTS57.L,64,4 Brother
PO-VOB-06/26-093,VOBTS57.M,77,4 Brother
PO-VOB-06/26-093,VOBTS57.XL,25,4 Brother
PO-VOB-06/26-096,VOBP8BT.28,184,SN988
PO-VOB-06/26-096,VOBP8BT.30,187,SN988
PO-VOB-06/26-096,VOBP8BT.32,90,SN988
PO-VOB-06/26-096,VOBP8BT.34,62,SN988
PO-VOB-06/26-097,VOBO57.L,133,SN988
PO-VOB-06/26-097,VOBO57.M,242,SN988
PO-VOB-06/26-097,VOBO57.XL,50,SN988
PO-VOB-06/26-098,VOBSH35.M,72,Herpro
PO-VOB-06/26-098,VOBSH35.XL,19,Herpro
PO-VOB-06/26-098,VOBSH36.L,84,Herpro
PO-VOB-06/26-098,VOBSH36.XL,40,Herpro
PO-VOB-06/26-098,VOBSH45.L,36,Herpro
PO-VOB-06/26-098,VOBSH45.M,80,Herpro
PO-VOB-06/26-098,VOBSH45.XL,37,Herpro
PO-VOB-06/26-102,VOBH21,50,Ato
PO-VOB-06/26-102,VOBH22,50,Ato
PO-VOB-06/26-102,VOBH23,50,Ato
PO-VOB-06/26-102,VOBH24,50,Ato
PO-VOB-06/26-102,VOBH25,50,Ato
PO-VOB-06/26-102,VOBH26,50,Ato
PO-VOB-06/26-102,VOBH27,50,Ato
PO-VOB-06/26-103,VOBO49.L,60,Harmoni
PO-VOB-06/26-103,VOBO49.M,72,Harmoni
PO-VOB-06/26-103,VOBO49.XL,41,Harmoni
PO-VOB-06/26-103,VOBO50.L,58,Harmoni
PO-VOB-06/26-103,VOBO50.M,195,Harmoni
PO-VOB-06/26-103,VOBO50.XL,43,Harmoni
PO-VOB-06/26-103,VOBO51.L,12,Harmoni
PO-VOB-06/26-103,VOBO51.M,46,Harmoni
PO-VOB-06/26-103,VOBO51.XL,12,Harmoni
PO-VOB-06/26-103,VOBO52.L,12,Harmoni
PO-VOB-06/26-103,VOBO52.M,23,Harmoni
PO-VOB-06/26-103,VOBO52.XL,1,Harmoni
PO-VOB-06/26-103,VOBO53.L,7,Harmoni
PO-VOB-06/26-103,VOBO53.M,16,Harmoni
PO-VOB-06/26-103,VOBO53.XL,3,Harmoni
PO-VOB-06/26-103,VOBO54.L,109,Harmoni
PO-VOB-06/26-103,VOBO54.M,175,Harmoni
PO-VOB-06/26-103,VOBO54.XL,12,Harmoni
PO-VOB-06/26-103,VOBO55.L,67,Harmoni
PO-VOB-06/26-103,VOBO55.M,112,Harmoni
PO-VOB-06/26-103,VOBO55.XL,25,Harmoni
PO-VOB-06/26-103,VOBO90.L,2,Harmoni
PO-VOB-06/26-103,VOBO90.M,43,Harmoni
PO-VOB-06/26-103,VOBO90.XL,19,Harmoni
PO-VOB-06/26-109,VOBSH10.L,39,Rabika
PO-VOB-06/26-109,VOBSH10.M,50,Rabika
PO-VOB-06/26-109,VOBSH10.XL,15,Rabika
PO-VOB-06/26-110,VOBO91.L,37,Harmoni
PO-VOB-06/26-110,VOBO91.M,50,Harmoni
PO-VOB-06/26-110,VOBO91.XL,14,Harmoni
PO-VOB-06/26-110,VOBO92.L,33,Harmoni
PO-VOB-06/26-110,VOBO92.M,53,Harmoni
PO-VOB-06/26-110,VOBO92.XL,16,Harmoni
PO-VOB-06/26-110,VOBO93.L,21,Harmoni
PO-VOB-06/26-110,VOBO93.M,30,Harmoni
PO-VOB-06/26-110,VOBO93.XL,12,Harmoni
PO-VOB-06/26-117,VOBO84.L,40,Yasin
PO-VOB-06/26-117,VOBO84.M,60,Yasin
PO-VOB-06/26-117,VOBO84.XL,20,Yasin
PO-VOB-06/26-117,VOBO85.L,70,Yasin
PO-VOB-06/26-117,VOBO85.M,100,Yasin
PO-VOB-06/26-117,VOBO85.XL,30,Yasin
PO-VOB-06/26-117,VOBO88.L,40,Yasin
PO-VOB-06/26-117,VOBO88.M,60,Yasin
PO-VOB-06/26-117,VOBO88.XL,20,Yasin
PO-VOB-06/26-117,VOBO89.L,40,Yasin
PO-VOB-06/26-117,VOBO89.M,60,Yasin
PO-VOB-06/26-117,VOBO89.XL,20,Yasin
"""


def _target_rows():
    rows = list(csv.DictReader(io.StringIO(TARGET_CSV)))
    for row in rows:
        row["qty"] = Decimal(row["qty"])
    return rows


def _fingerprint(lines):
    payload = "\n".join(
        sorted(
            f"{line.po.po_number}|{line.sku.sku}|{int(line.ordered_qty)}|{line.po.supplier.name}"
            for line in lines
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _assert_no_downstream(apps, database, po_ids, line_ids):
    checks = (
        (apps.get_model("inventory", "QCInspection"), {"po_line_id__in": line_ids}),
        (apps.get_model("inventory", "QCFollowUp"), {"po_line_id__in": line_ids}),
        (apps.get_model("inventory", "InboundReceipt"), {"po_line_id__in": line_ids}),
        (apps.get_model("inventory", "FIFOLayer"), {"source_po_line_id__in": line_ids}),
        (apps.get_model("merchandising", "IncomingCarryover"), {"po_line_id__in": line_ids}),
        (apps.get_model("production", "ProductionActivity"), {"po_line_id__in": line_ids}),
        (
            apps.get_model("production", "ProductionCogsFinalization"),
            {"production_order__po_id__in": po_ids},
        ),
    )
    blocked = [model._meta.label for model, query in checks if model.objects.using(database).filter(**query).exists()]
    if blocked:
        raise RuntimeError("PO WIP restatement dibatalkan karena ada transaksi downstream: " + ", ".join(blocked))


def _snapshot(pos, lines):
    return {
        "purchase_orders": [
            {
                "id": str(po.id),
                "po_number": po.po_number,
                "supplier_id": str(po.supplier_id),
                "need_month": po.need_month.isoformat(),
                "issue_month": po.issue_month.isoformat() if po.issue_month else None,
                "notes": po.notes,
                "migration_evidence_reference": po.migration_evidence_reference,
            }
            for po in pos
        ],
        "lines": [
            {
                "id": str(line.id),
                "po_id": str(line.po_id),
                "sku_id": str(line.sku_id),
                "ordered_qty": str(line.ordered_qty),
                "cogs_snapshot": str(line.cogs_snapshot) if line.cogs_snapshot is not None else None,
                "received_before_cutover_qty": str(line.received_before_cutover_qty),
                "qc_passed_before_cutover_qty": str(line.qc_passed_before_cutover_qty),
            }
            for line in lines
        ],
    }


def restate_legacy_po_wip(apps, schema_editor):
    database = schema_editor.connection.alias
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    Supplier = apps.get_model("master_data", "Supplier")
    SKU = apps.get_model("master_data", "SKU")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    pos = list(
        PurchaseOrder.objects.using(database)
        .filter(source="LEGACY_WIP")
        .select_related("supplier")
        .order_by("po_number")
    )
    if not pos:
        return
    lines = list(
        PurchaseOrderLine.objects.using(database)
        .filter(po__source="LEGACY_WIP")
        .select_related("po__supplier", "sku")
        .order_by("po__po_number", "sku__sku")
    )
    current_fingerprint = _fingerprint(lines)
    if current_fingerprint == TARGET_FINGERPRINT:
        return
    if current_fingerprint != SOURCE_FINGERPRINT:
        raise RuntimeError(f"Baseline PO WIP live tidak cocok; fingerprint {current_fingerprint}. Migrasi dibatalkan.")
    if len(pos) != 14 or len(lines) != 90 or sum(line.ordered_qty for line in lines) != Decimal("4946"):
        raise RuntimeError("Baseline PO WIP live bukan 14 PO / 90 line / 4.946 pcs. Migrasi dibatalkan.")

    po_ids = [po.id for po in pos]
    line_ids = [line.id for line in lines]
    _assert_no_downstream(apps, database, po_ids, line_ids)
    before = _snapshot(pos, lines)

    target = _target_rows()
    target_skus = {row["sku"] for row in target}
    skus = {sku.sku: sku for sku in SKU.objects.using(database).filter(sku__in=target_skus)}
    missing_skus = sorted(target_skus - set(skus))
    if missing_skus:
        raise RuntimeError("SKU target tidak ditemukan: " + ", ".join(missing_skus))

    supplier_names = {row["supplier"] for row in target}
    suppliers = {
        supplier.name.casefold(): supplier
        for supplier in Supplier.objects.using(database).filter(name__in=supplier_names)
    }
    for name in sorted(supplier_names):
        if name.casefold() not in suppliers:
            code = name.upper().replace(" ", "_")
            supplier = Supplier.objects.using(database).filter(code=code).first()
            if supplier:
                supplier.name = name
                supplier.is_active = True
                supplier.save(update_fields=["name", "is_active"])
            else:
                supplier = Supplier.objects.using(database).create(code=code, name=name, is_active=True)
            suppliers[name.casefold()] = supplier
        elif not suppliers[name.casefold()].is_active:
            suppliers[name.casefold()].is_active = True
            suppliers[name.casefold()].save(update_fields=["is_active"])

    po_by_number = {po.po_number: po for po in pos}
    renamed = po_by_number.pop("PO-VOB-04/26-061")
    renamed.po_number = "PO-VOB-06/26-109"
    renamed.need_month = date(2026, 6, 1)
    renamed.issue_month = date(2026, 6, 1)
    renamed.save(update_fields=["po_number", "need_month", "issue_month"])
    po_by_number[renamed.po_number] = renamed

    evidence = f"{TARGET_FILE} · SHA256 {TARGET_FILE_SHA256}"
    target_by_po = {}
    for row in target:
        target_by_po.setdefault(row["po_number"], []).append(row)
    if set(po_by_number) != set(target_by_po):
        raise RuntimeError("Daftar nomor PO target tidak cocok dengan baseline live.")

    for po_number, po_rows in target_by_po.items():
        po = po_by_number[po_number]
        supplier_names_for_po = {row["supplier"] for row in po_rows}
        if len(supplier_names_for_po) != 1:
            raise RuntimeError(f"{po_number} memiliki lebih dari satu vendor.")
        supplier_name = supplier_names_for_po.pop()
        po.supplier_id = suppliers[supplier_name.casefold()].id
        po.migration_evidence_reference = evidence
        po.notes = (
            "Restatement outstanding PO WIP per 31 July 2026 dari file terbaru. PO Qty = WIP; "
            "Received before cutover = 0; QC Passed before cutover = WIP; Required Arrival kosong; "
            "COGS snapshot line lama dipertahankan dan line baru memakai master COGS saat restatement."
        )
        po.save(update_fields=["supplier", "migration_evidence_reference", "notes"])

        existing = {
            line.sku.sku: line
            for line in PurchaseOrderLine.objects.using(database).filter(po=po).select_related("sku")
        }
        target_by_sku = {row["sku"]: row for row in po_rows}
        PurchaseOrderLine.objects.using(database).filter(
            po=po, sku__sku__in=(set(existing) - set(target_by_sku))
        ).delete()
        for sku_code, row in target_by_sku.items():
            line = existing.get(sku_code)
            if line:
                line.ordered_qty = row["qty"]
                line.qc_passed_before_cutover_qty = row["qty"]
                line.save(update_fields=["ordered_qty", "qc_passed_before_cutover_qty"])
            else:
                sku = skus[sku_code]
                if sku.current_master_cogs is None:
                    raise RuntimeError(f"Master COGS {sku_code} kosong; migrasi dibatalkan.")
                PurchaseOrderLine.objects.using(database).create(
                    po=po,
                    sku=sku,
                    ordered_qty=row["qty"],
                    cogs_snapshot=sku.current_master_cogs,
                    received_before_cutover_qty=Decimal("0"),
                    qc_passed_before_cutover_qty=row["qty"],
                )

    final_lines = list(
        PurchaseOrderLine.objects.using(database)
        .filter(po__source="LEGACY_WIP")
        .select_related("po__supplier", "sku")
    )
    if _fingerprint(final_lines) != TARGET_FINGERPRINT:
        raise RuntimeError("Hasil restatement PO WIP tidak cocok dengan file target.")

    AuditEvent.objects.using(database).create(
        actor=None,
        action="legacy_wip_restatement_committed",
        entity_type="purchasing.purchaseorder",
        entity_id=TARGET_FILE_SHA256,
        reason="Koreksi backend berdasarkan file PO WIP terbaru dari Adit.",
        before_values=before,
        after_values={"purchase_orders": 14, "lines": 90, "outstanding_qty": "4696"},
        metadata={
            "source_fingerprint": SOURCE_FINGERPRINT,
            "target_fingerprint": TARGET_FINGERPRINT,
            "target_filename": TARGET_FILE,
            "target_file_sha256": TARGET_FILE_SHA256,
            "inventory_movements_changed": False,
            "fifo_layers_changed": False,
        },
    )


def rollback_legacy_po_wip(apps, schema_editor):
    database = schema_editor.connection.alias
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    pos = list(
        PurchaseOrder.objects.using(database)
        .filter(source="LEGACY_WIP")
        .select_related("supplier")
    )
    if not pos:
        return
    lines = list(
        PurchaseOrderLine.objects.using(database)
        .filter(po__source="LEGACY_WIP")
        .select_related("po__supplier", "sku")
    )
    if _fingerprint(lines) == SOURCE_FINGERPRINT:
        return
    if _fingerprint(lines) != TARGET_FINGERPRINT:
        raise RuntimeError("PO WIP bukan target restatement; rollback dibatalkan.")
    _assert_no_downstream(apps, database, [po.id for po in pos], [line.id for line in lines])

    event = (
        AuditEvent.objects.using(database)
        .filter(action="legacy_wip_restatement_committed", entity_id=TARGET_FILE_SHA256)
        .order_by("-occurred_at")
        .first()
    )
    if not event or not event.before_values:
        raise RuntimeError("Snapshot rollback PO WIP tidak ditemukan.")

    PurchaseOrderLine.objects.using(database).filter(po__source="LEGACY_WIP").delete()
    for row in event.before_values["purchase_orders"]:
        PurchaseOrder.objects.using(database).filter(pk=row["id"]).update(
            po_number=row["po_number"],
            supplier_id=row["supplier_id"],
            need_month=date.fromisoformat(row["need_month"]),
            issue_month=date.fromisoformat(row["issue_month"]) if row["issue_month"] else None,
            notes=row["notes"],
            migration_evidence_reference=row["migration_evidence_reference"],
        )
    for row in event.before_values["lines"]:
        PurchaseOrderLine.objects.using(database).create(
            id=row["id"],
            po_id=row["po_id"],
            sku_id=row["sku_id"],
            ordered_qty=Decimal(row["ordered_qty"]),
            cogs_snapshot=Decimal(row["cogs_snapshot"]) if row["cogs_snapshot"] is not None else None,
            received_before_cutover_qty=Decimal(row["received_before_cutover_qty"]),
            qc_passed_before_cutover_qty=Decimal(row["qc_passed_before_cutover_qty"]),
        )
    restored = list(
        PurchaseOrderLine.objects.using(database)
        .filter(po__source="LEGACY_WIP")
        .select_related("po__supplier", "sku")
    )
    if _fingerprint(restored) != SOURCE_FINGERPRINT:
        raise RuntimeError("Hasil rollback PO WIP tidak cocok dengan snapshot sumber.")
    AuditEvent.objects.using(database).create(
        actor=None,
        action="legacy_wip_restatement_rolled_back",
        entity_type="purchasing.purchaseorder",
        entity_id=TARGET_FILE_SHA256,
        reason="Rollback migrasi data PO WIP 0008.",
        before_values={"target_fingerprint": TARGET_FINGERPRINT},
        after_values={"source_fingerprint": SOURCE_FINGERPRINT},
    )


_rows = _target_rows()
assert len(_rows) == 90
assert len({row["po_number"] for row in _rows}) == 14
assert len({(row["po_number"], row["sku"]) for row in _rows}) == 90
assert sum(row["qty"] for row in _rows) == Decimal("4696")


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0007_po_number_uses_issue_month"),
        ("audit", "0001_initial"),
        ("inventory", "0014_physicalreturnreceipt_follow_up_status"),
        ("merchandising", "0008_alter_projectionscenario_status"),
        ("production", "0009_rejected_goods_delivery_activity"),
    ]

    operations = [migrations.RunPython(restate_legacy_po_wip, rollback_legacy_po_wip)]
