import csv
import hashlib
import io
import re
from datetime import date, datetime, time
from decimal import Decimal

from django.db import migrations, models, transaction
from django.db.models import Sum
from django.utils import timezone


SOURCE_FILE = "PO WIP tambahan.xlsx"
SOURCE_FILE_SHA256 = "dc998d7a15bec33fd29323f88be9a8f7b58ae4d85958605e2cd6db8319456cea"
SOURCE_FINGERPRINT = "644e30969b3a1e15cfc6477fad37f11e6e7482b3cac009a02fda8c27eaa45223"
BASELINE_FINGERPRINT = "841cf67cdf3e4201653cce80e12874acadb09b041a9b87e3b22d1fb3dbd467e1"
EXISTING_PO_NUMBER = "PO-VOB-06/26-109"
IMPORT_DATE = date(2026, 9, 10)
PO_PATTERN = re.compile(r"^PO-VOB-(?P<month>\d{2})/(?P<year>\d{2})-(?P<sequence>\d{3})(?:[A-Z])?$")

SOURCE_CSV = """po_number,sku,qty,supplier
PO-VOB-06/26-111,VOBTS66.M,85,4 Brother
PO-VOB-06/26-111,VOBTS66.L,57,4 Brother
PO-VOB-06/26-111,VOBTS66.XL,22,4 Brother
PO-VOB-08/26-118,VOBTS68.M,63,4 Brother
PO-VOB-08/26-118,VOBTS68.L,44,4 Brother
PO-VOB-08/26-118,VOBTS68.XL,21,4 Brother
PO-VOB-06/26-108,VOBTS86.M,41,Rabika
PO-VOB-06/26-108,VOBTS86.L,46,Rabika
PO-VOB-06/26-108,VOBTS86.XL,1,Rabika
PO-VOB-06/26-108,VOBTS87.M,41,Rabika
PO-VOB-06/26-108,VOBTS87.L,39,Rabika
PO-VOB-06/26-094,VOBTS84.M,5,4 Brother
PO-VOB-06/26-094,VOBTS84.L,5,4 Brother
PO-VOB-06/26-094,VOBTS84.XL,3,4 Brother
PO-VOB-06/26-094,VOBTS83.L,1,4 Brother
PO-VOB-06/26-094,VOBTS83.M,1,4 Brother
PO-VOB-06/26-106,VOBP10.28,166,Arifin
PO-VOB-06/26-106,VOBP10.30,165,Arifin
PO-VOB-06/26-106,VOBP10.32,61,Arifin
PO-VOB-06/26-106,VOBP10.34,22,Arifin
PO-VOB-06/26-087A,VOBO58.M,12,Harmoni
PO-VOB-06/26-087A,VOBO39.XL,6,Harmoni
PO-VOB-06/26-087A,VOBO39.L,3,Harmoni
PO-VOB-06/26-087A,VOBO38.M,8,Harmoni
PO-VOB-06/26-095,VOBSH7B.M,87,Herpro
PO-VOB-06/26-095,VOBSH7B.L,58,Herpro
PO-VOB-06/26-095,VOBSH7B.XL,37,Herpro
PO-VOB-06/26-095,VOBSH7W.M,35,Herpro
PO-VOB-06/26-095,VOBSH7W.L,30,Herpro
PO-VOB-06/26-089,VOC19,56,Harmoni
PO-VOB-06/26-089,VOC21,6,Harmoni
PO-VOB-06/26-089,VOC18,6,Harmoni
PO-VOB-06/26-089,VOC30,4,Harmoni
PO-VOB-06/26-109,VOBSH29.L,106,Rabika
PO-VOB-06/26-109,VOBSH29.XL,33,Rabika
PO-VOB-06/26-109,VOBSH32.M,52,Rabika
PO-VOB-06/26-109,VOBSH32.L,24,Rabika
PO-VOB-06/26-109,VOBSH32.XL,15,Rabika
PO-VOB-04/26-060,VOBTS74.M,3,4 Brother
PO-VOB-04/26-060,VOBTS74.L,2,4 Brother
PO-VOB-04/26-060,VOBTS74.XL,1,4 Brother
PO-VOB-04/26-060,VOBTS62.M,5,4 Brother
PO-VOB-04/26-060,VOBTS62.L,3,4 Brother
PO-VOB-04/26-060,VOBTS62.XL,1,4 Brother
PO-VOB-04/26-060,VOBTS70.M,2,4 Brother
PO-VOB-04/26-060,VOBTS70.L,1,4 Brother
PO-VOB-04/26-060,VOBTS70.XL,3,4 Brother
PO-VOB-08/26-124,S95,372,Morph
PO-VOB-08/26-124,S96,72,Morph
PO-VOB-08/26-124,S106,360,Morph
PO-VOB-08/26-124,S119,360,Morph
"""

SUPPLIER_CODES = {
    "4 Brother": "VEN-4-BROTHER",
    "Arifin": "VEN-ARIFIN",
    "Harmoni": "VEN-HARMONI",
    "Herpro": "VEN-HERPRO",
    "Morph": "VEN-MORPH",
    "Rabika": "VEN-RABIKA",
}


def _rows():
    rows = list(csv.DictReader(io.StringIO(SOURCE_CSV)))
    for row in rows:
        row["qty"] = Decimal(row["qty"])
    return rows


def _fingerprint(rows):
    payload = "\n".join(
        sorted(f"{row['po_number']}|{row['sku']}|{int(row['qty'])}|{row['supplier']}" for row in rows)
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _legacy_fingerprint(lines):
    payload = "\n".join(
        sorted(
            f"{line.po.po_number}|{line.sku.sku}|{int(line.ordered_qty)}|{line.po.supplier.name}"
            for line in lines
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _po_month(po_number):
    match = PO_PATTERN.fullmatch(po_number)
    if not match:
        raise RuntimeError(f"Format No. PO tambahan tidak valid: {po_number}.")
    return date(2000 + int(match.group("year")), int(match.group("month")), 1)


def _find_or_create_supplier(Supplier, database, name):
    code = SUPPLIER_CODES[name]
    by_name = Supplier.objects.using(database).filter(name__iexact=name).first()
    by_code = Supplier.objects.using(database).filter(code=code).first()
    if by_name and by_code and by_name.pk != by_code.pk:
        raise RuntimeError(f"Vendor {name} bentrok antara nama dan kode {code}.")
    supplier = by_name or by_code
    if supplier:
        if supplier.name.casefold() != name.casefold():
            raise RuntimeError(f"Kode vendor {code} sudah dipakai oleh {supplier.name}.")
        if not supplier.is_active:
            supplier.is_active = True
            supplier.save(update_fields=["is_active"])
        return supplier, False
    return Supplier.objects.using(database).create(code=code, name=name, is_active=True), True


def _addition_fingerprint(PurchaseOrderLine, database, rows):
    keys = {(row["po_number"], row["sku"]) for row in rows}
    lines = PurchaseOrderLine.objects.using(database).filter(
        po__po_number__in={po_number for po_number, _sku in keys},
        sku__sku__in={sku for _po_number, sku in keys},
    ).select_related("po__supplier", "sku")
    actual = [
        {
            "po_number": line.po.po_number,
            "sku": line.sku.sku,
            "qty": line.ordered_qty,
            "supplier": line.po.supplier.name,
        }
        for line in lines
        if (line.po.po_number, line.sku.sku) in keys
    ]
    return _fingerprint(actual), len(actual)


def _append(apps, schema_editor):
    database = schema_editor.connection.alias
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    Supplier = apps.get_model("master_data", "Supplier")
    SKU = apps.get_model("master_data", "SKU")
    User = apps.get_model("accounts", "User")
    ProductionCogsFinalization = apps.get_model("production", "ProductionCogsFinalization")
    InventoryMovement = apps.get_model("inventory", "InventoryMovement")
    FIFOLayer = apps.get_model("inventory", "FIFOLayer")
    InboundReceipt = apps.get_model("inventory", "InboundReceipt")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    rows = _rows()
    current_fingerprint, current_count = _addition_fingerprint(PurchaseOrderLine, database, rows)
    if current_count == len(rows) and current_fingerprint == SOURCE_FINGERPRINT:
        return
    if current_count:
        raise RuntimeError("Migrasi PO WIP tambahan dibatalkan: sebagian No. PO + SKU sudah ada.")

    legacy_pos = PurchaseOrder.objects.using(database).filter(source="LEGACY_WIP")
    legacy_lines = list(
        PurchaseOrderLine.objects.using(database)
        .filter(po__source="LEGACY_WIP")
        .select_related("po__supplier", "sku")
    )
    if legacy_pos.count() != 14 or len(legacy_lines) != 90:
        raise RuntimeError("Baseline PO WIP live bukan 14 PO / 90 line.")
    if (legacy_pos.aggregate(total=Sum("lines__ordered_qty"))["total"] or Decimal("0")) != Decimal("4696"):
        raise RuntimeError("Baseline PO WIP live bukan 4.696 pcs.")
    if _legacy_fingerprint(legacy_lines) != BASELINE_FINGERPRINT:
        raise RuntimeError("Fingerprint baseline PO WIP live berubah; migrasi tambahan dibatalkan.")

    target_po_numbers = {row["po_number"] for row in rows}
    existing = {
        po.po_number: po
        for po in PurchaseOrder.objects.using(database)
        .filter(po_number__in=target_po_numbers)
        .select_related("supplier")
    }
    if set(existing) != {EXISTING_PO_NUMBER}:
        raise RuntimeError("Daftar No. PO tambahan bentrok dengan PO yang sudah ada.")
    existing_po = existing[EXISTING_PO_NUMBER]
    if (
        existing_po.source != "LEGACY_WIP"
        or existing_po.status != "RELEASED"
        or existing_po.supplier.name.casefold() != "rabika"
    ):
        raise RuntimeError("PO-VOB-06/26-109 tidak lagi cocok dengan baseline migrasi.")
    existing_po_lines = PurchaseOrderLine.objects.using(database).filter(po=existing_po)
    if existing_po_lines.count() != 3 or (
        existing_po_lines.aggregate(total=Sum("ordered_qty"))["total"] or Decimal("0")
    ) != Decimal("104"):
        raise RuntimeError("Komposisi awal PO-VOB-06/26-109 bukan 3 line / 104 pcs.")
    if ProductionCogsFinalization.objects.using(database).filter(
        production_order__po=existing_po
    ).exists():
        raise RuntimeError("PO-VOB-06/26-109 sudah finalisasi COGS; penambahan line dibatalkan.")

    sku_codes = {row["sku"] for row in rows}
    skus = {
        sku.sku: sku
        for sku in SKU.objects.using(database).filter(sku__in=sku_codes, is_active=True)
    }
    missing_skus = sorted(sku_codes - set(skus))
    if missing_skus:
        raise RuntimeError("SKU tambahan tidak ditemukan/aktif: " + ", ".join(missing_skus))
    missing_cogs = sorted(code for code, sku in skus.items() if sku.current_master_cogs is None)
    if missing_cogs:
        raise RuntimeError("Master COGS tambahan kosong: " + ", ".join(missing_cogs))

    actor = (
        User.objects.using(database).filter(username__iexact="aditya", is_active=True).first()
        or User.objects.using(database).filter(is_superuser=True, is_active=True).order_by("pk").first()
    )
    if actor is None:
        raise RuntimeError("Akun approver aktif tidak ditemukan.")

    suppliers = {}
    created_supplier_ids = []
    for supplier_name in sorted({row["supplier"] for row in rows}):
        supplier, created = _find_or_create_supplier(Supplier, database, supplier_name)
        suppliers[supplier_name] = supplier
        if created:
            created_supplier_ids.append(str(supplier.pk))

    evidence = f"{SOURCE_FILE} · SHA256 {SOURCE_FILE_SHA256}"
    imported_at = timezone.make_aware(datetime.combine(IMPORT_DATE, time(hour=12)))
    by_po = {}
    for row in rows:
        by_po.setdefault(row["po_number"], []).append(row)

    created_po_ids = []
    created_line_ids = []
    for po_number, po_rows in sorted(by_po.items()):
        supplier_names = {row["supplier"] for row in po_rows}
        if len(supplier_names) != 1:
            raise RuntimeError(f"{po_number} memiliki lebih dari satu vendor.")
        supplier_name = supplier_names.pop()
        po = existing_po if po_number == EXISTING_PO_NUMBER else None
        if po is None:
            issue_month = _po_month(po_number)
            po = PurchaseOrder.objects.using(database).create(
                po_number=po_number,
                sequence=None,
                issue_month=issue_month,
                supplier=suppliers[supplier_name],
                need_month=issue_month,
                required_arrival=None,
                source="SUPPLEMENTAL_WIP",
                status="RELEASED",
                notes=(
                    "Migrasi tambahan PO WIP per 10 September 2026. PO Qty = outstanding WIP; "
                    "tidak membuat stock, inbound, FIFO, atau QC historis. Wajib melalui Production Plan dan QC ERP."
                ),
                created_by=actor,
                released_by=actor,
                released_at=imported_at,
                migration_cutoff_date=None,
                migration_evidence_reference=evidence,
            )
            PurchaseOrder.objects.using(database).filter(pk=po.pk).update(created_at=imported_at)
            created_po_ids.append(str(po.pk))
        elif po.supplier_id != suppliers[supplier_name].pk:
            raise RuntimeError(f"Vendor {po_number} tidak cocok dengan file tambahan.")

        for row in po_rows:
            line = PurchaseOrderLine.objects.using(database).create(
                po=po,
                requirement=None,
                sku=skus[row["sku"]],
                ordered_qty=row["qty"],
                cogs_snapshot=skus[row["sku"]].current_master_cogs,
                received_before_cutover_qty=Decimal("0"),
                qc_passed_before_cutover_qty=Decimal("0"),
            )
            created_line_ids.append(str(line.pk))

    final_fingerprint, final_count = _addition_fingerprint(PurchaseOrderLine, database, rows)
    if final_count != 51 or final_fingerprint != SOURCE_FINGERPRINT:
        raise RuntimeError("Hasil migrasi tambahan tidak cocok dengan file sumber.")
    if InventoryMovement.objects.using(database).filter(source_reference__icontains=SOURCE_FILE).exists():
        raise RuntimeError("Migrasi tambahan tidak boleh membuat Inventory Movement.")
    if FIFOLayer.objects.using(database).filter(source_reference__icontains=SOURCE_FILE).exists():
        raise RuntimeError("Migrasi tambahan tidak boleh membuat FIFO Layer.")
    if InboundReceipt.objects.using(database).filter(po_line_id__in=created_line_ids).exists():
        raise RuntimeError("Migrasi tambahan tidak boleh membuat Inbound Receipt.")

    AuditEvent.objects.using(database).create(
        actor=actor,
        action="supplemental_po_wip_committed",
        entity_type="purchasing.purchaseorder",
        entity_id=SOURCE_FILE_SHA256,
        reason="Penambahan PO WIP dari file terbaru Adit; bukan restatement baseline 14 PO.",
        before_values={"migrated_purchase_orders": 14, "migrated_lines": 90, "migrated_po_qty": "4696"},
        after_values={
            "migrated_purchase_orders": 24,
            "migrated_lines": 141,
            "migrated_po_qty": "7348",
            "new_purchase_orders": 10,
            "appended_purchase_orders": 1,
            "added_lines": 51,
            "added_qty": "2652",
        },
        metadata={
            "source_filename": SOURCE_FILE,
            "source_file_sha256": SOURCE_FILE_SHA256,
            "source_fingerprint": SOURCE_FINGERPRINT,
            "created_po_ids": created_po_ids,
            "created_line_ids": created_line_ids,
            "created_supplier_ids": created_supplier_ids,
            "inventory_movements_changed": False,
            "fifo_layers_changed": False,
            "inbound_receipts_changed": False,
            "historical_qc_assumed": False,
        },
    )


def append_supplemental_po_wip(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with transaction.atomic(using=schema_editor.connection.alias):
        _append(apps, schema_editor)


def rollback_supplemental_po_wip(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    database = schema_editor.connection.alias
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    ProductionOrder = apps.get_model("production", "ProductionOrder")
    ProductionActivity = apps.get_model("production", "ProductionActivity")
    QCInspection = apps.get_model("inventory", "QCInspection")
    InboundReceipt = apps.get_model("inventory", "InboundReceipt")
    FIFOLayer = apps.get_model("inventory", "FIFOLayer")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    event = AuditEvent.objects.using(database).filter(
        action="supplemental_po_wip_committed", entity_id=SOURCE_FILE_SHA256
    ).order_by("-occurred_at").first()
    if event is None:
        return
    line_ids = event.metadata.get("created_line_ids", [])
    po_ids = event.metadata.get("created_po_ids", [])
    blocked = []
    checks = (
        (QCInspection, {"po_line_id__in": line_ids}),
        (InboundReceipt, {"po_line_id__in": line_ids}),
        (FIFOLayer, {"source_po_line_id__in": line_ids}),
        (ProductionActivity, {"po_line_id__in": line_ids}),
        (ProductionOrder, {"po_id__in": po_ids}),
    )
    for model, query in checks:
        if model.objects.using(database).filter(**query).exists():
            blocked.append(model._meta.label)
    if blocked:
        raise RuntimeError("Rollback PO WIP tambahan dibatalkan karena ada transaksi downstream: " + ", ".join(blocked))
    PurchaseOrderLine.objects.using(database).filter(pk__in=line_ids).delete()
    PurchaseOrder.objects.using(database).filter(pk__in=po_ids, source="SUPPLEMENTAL_WIP").delete()
    AuditEvent.objects.using(database).create(
        actor=event.actor,
        action="supplemental_po_wip_rolled_back",
        entity_type="purchasing.purchaseorder",
        entity_id=SOURCE_FILE_SHA256,
        reason="Rollback migrasi data PO WIP tambahan 0012.",
        before_values=event.after_values,
        after_values=event.before_values,
    )


_source_rows = _rows()
assert len(_source_rows) == 51
assert len({row["po_number"] for row in _source_rows}) == 11
assert len({(row["po_number"], row["sku"]) for row in _source_rows}) == 51
assert sum(row["qty"] for row in _source_rows) == Decimal("2652")
assert _fingerprint(_source_rows) == SOURCE_FINGERPRINT


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ("purchasing", "0011_cleanup_operation_uat_retry"),
        ("production", "0009_rejected_goods_delivery_activity"),
        ("inventory", "0017_reset_active_po_wip_before_qc"),
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="purchaseorder",
            name="source",
            field=models.CharField(
                choices=[
                    ("INCOMING_PLAN", "Approved Incoming Plan"),
                    ("MANUAL_NEW_PRODUCT", "Manual – New Product"),
                    ("LEGACY_WIP", "PO WIP · outstanding per 31 July 2026"),
                    ("SUPPLEMENTAL_WIP", "PO WIP · supplemental import"),
                ],
                default="INCOMING_PLAN",
                max_length=30,
            ),
        ),
        migrations.RunPython(append_supplemental_po_wip, rollback_supplemental_po_wip),
    ]
