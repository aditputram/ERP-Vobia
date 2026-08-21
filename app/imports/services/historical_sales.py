import csv
import hashlib
import uuid
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit
from master_data.models import SKU
from sales.models import SalesOrder, SalesOrderLine, SalesStatusHistory

from ..models import RawFile, SalesImportBatch, SalesImportIssue, StagedSalesRow


PARSER_VERSION = "sales-history-v1"
HISTORY_START = date(2026, 1, 1)
HISTORY_END = date(2026, 7, 31)
REQUIRED_HEADERS = {
    "Month", "Date", "Source", "Source Group", "No. Pesanan", "Status Pesanan",
    "SKU", "Status Produk", "Category", "Sub Category", "Nama Produk", "Nama Variasi",
    "Qty", "Harga Setelah Diskon", "Retail Price", "COGS", "Total Gross Sales",
    "Total Net Sales", "Total COGS", "Margin",
}


def _text(value):
    return str(value or "").strip()


def _money(value, *, required=False):
    text = _text(value).replace("Rp", "").replace(",", "").replace(" ", "")
    if not text:
        if required:
            raise InvalidOperation
        return None
    return Decimal(text)


def _quantity(value):
    parsed = _money(value, required=True)
    if parsed <= 0 or parsed != parsed.to_integral_value():
        raise InvalidOperation
    return int(parsed)


def _row_date(month_value, day_value):
    month_number = int(_text(month_value).split(".", maxsplit=1)[0])
    day_number = int(_text(day_value))
    return date(2026, month_number, day_number)


def _aware_noon(value):
    return timezone.make_aware(datetime.combine(value, time(12, 0)), timezone.get_current_timezone())


def _source_type(label):
    if label == SalesOrder.Source.SHOPEE:
        return SalesOrder.Source.SHOPEE
    if label == SalesOrder.Source.TIKTOK:
        return SalesOrder.Source.TIKTOK
    return SalesOrder.Source.OTHER


@transaction.atomic
def parse_historical_sales_batch(batch):
    batch = SalesImportBatch.objects.select_for_update().select_related("raw_file").get(pk=batch.pk)
    if batch.mode != SalesImportBatch.Mode.HISTORICAL:
        raise ValidationError("Batch bukan historical migration.")
    if batch.status == SalesImportBatch.Status.COMMITTED:
        raise ValidationError("Historical batch yang sudah committed tidak dapat diparsing ulang.")

    batch.staged_rows.all().delete()
    batch.issues.all().delete()
    path = Path(settings.PRIVATE_UPLOAD_ROOT) / batch.raw_file.storage_path
    staged = []
    pending_issues = []
    quality = Counter()
    dates = []
    keys = Counter()

    master = {
        item.sku: item
        for item in SKU.objects.select_related(
            "product_variant__product__status",
            "product_variant__product__category",
            "product_variant__product__subcategory",
        ).all()
    }

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_HEADERS - headers)
        if missing:
            SalesImportIssue.objects.create(
                batch=batch,
                severity=SalesImportIssue.Severity.ERROR,
                code="MISSING_HISTORICAL_HEADERS",
                field_name="header",
                message="Header historical wajib tidak ditemukan: " + ", ".join(missing),
                is_blocking=True,
            )
            batch.status = SalesImportBatch.Status.BLOCKED
            batch.blocking_issue_count = 1
            batch.previewed_at = timezone.now()
            batch.quality_summary = {"headers": sorted(headers)}
            batch.save()
            return batch

        for row_number, raw in enumerate(reader, start=2):
            if not any(_text(value) for value in raw.values()):
                quality["blank_rows_ignored"] += 1
                continue
            try:
                order_date = _row_date(raw["Month"], raw["Date"])
            except (TypeError, ValueError):
                quality["blank_rows_ignored"] += 1
                continue
            if order_date < HISTORY_START or order_date > HISTORY_END:
                quality["outside_history_period_rows"] += 1
                continue

            row_errors = []
            source_label = _text(raw["Source"])
            order_number = _text(raw["No. Pesanan"])
            status = _text(raw["Status Pesanan"])
            sku_code = _text(raw["SKU"])
            try:
                qty = _quantity(raw["Qty"])
            except (InvalidOperation, ValueError):
                qty = None
                row_errors.append(("INVALID_QUANTITY", "Qty harus berupa bilangan bulat positif."))
            try:
                net_unit = _money(raw["Harga Setelah Diskon"], required=True)
                total_net = _money(raw["Total Net Sales"], required=True)
            except InvalidOperation:
                net_unit = total_net = None
                row_errors.append(("INVALID_NET_SALES", "Harga net dan Total Net Sales wajib berupa angka."))

            for field, value in (("Source", source_label), ("No. Pesanan", order_number), ("Status Pesanan", status), ("SKU", sku_code)):
                if not value:
                    row_errors.append(("REQUIRED_VALUE_MISSING", f"{field} wajib diisi."))

            retail = _money(raw["Retail Price"])
            cogs = _money(raw["COGS"])
            gross = _money(raw["Total Gross Sales"])
            total_cogs = _money(raw["Total COGS"])
            margin = _money(raw["Margin"])
            sku = master.get(sku_code)
            is_final = status in {"Selesai", "Retur"}
            order_datetime = _aware_noon(order_date)
            dates.append(order_datetime)
            keys[(source_label, order_number, sku_code)] += 1

            row = StagedSalesRow(
                batch=batch,
                row_number=row_number,
                source_label=source_label,
                source_group=_text(raw["Source Group"]),
                order_number=order_number,
                source_status=status,
                normalized_status=status,
                is_final=is_final,
                is_pure_cancelled=False,
                order_datetime=order_datetime,
                sku_text=sku_code,
                sku=sku,
                quantity=qty,
                net_unit_price=net_unit,
                retail_price_snapshot=retail,
                sales_cogs_snapshot=cogs,
                total_gross_sales_snapshot=gross,
                total_net_sales_snapshot=total_net,
                total_cogs_snapshot=total_cogs,
                gpm_snapshot=margin,
                product_status_snapshot=_text(raw["Status Produk"]),
                category_snapshot=_text(raw["Category"]),
                subcategory_snapshot=_text(raw["Sub Category"]),
                product_name_snapshot=_text(raw["Nama Produk"]),
                variant_name_snapshot=_text(raw["Nama Variasi"]),
                proposed_action=StagedSalesRow.ProposedAction.BLOCKED if row_errors else StagedSalesRow.ProposedAction.NEW,
                selected_source_data={
                    "month": _text(raw["Month"]),
                    "date": _text(raw["Date"]),
                    "source": source_label,
                    "source_group": _text(raw["Source Group"]),
                    "order_number": order_number,
                    "status": status,
                    "sku": sku_code,
                    "import_scope": "historical_reporting_only",
                },
            )
            staged.append(row)
            for code, message in row_errors:
                pending_issues.append((row, "ERROR", code, message, True))
            if sku is None:
                pending_issues.append((row, "WARNING", "HISTORICAL_SKU_NOT_IN_CURRENT_MASTER", f"SKU {sku_code} tidak ada di current Bank Data; snapshot historis dipertahankan tanpa membuat SKU palsu.", False))
                quality["unmapped_legacy_rows"] += 1
            if any(value is None for value in (retail, cogs, gross, total_cogs, margin)):
                quality["incomplete_financial_snapshot_rows"] += 1

    duplicate_keys = {key for key, count in keys.items() if count > 1}
    for row in staged:
        if (row.source_label, row.order_number, row.sku_text) in duplicate_keys:
            row.proposed_action = StagedSalesRow.ProposedAction.BLOCKED
            pending_issues.append((row, "ERROR", "DUPLICATE_BUSINESS_KEY", f"Duplicate {row.business_key} dalam source historical.", True))

    StagedSalesRow.objects.bulk_create(staged, batch_size=2000)
    issues = [
        SalesImportIssue(
            batch=batch,
            staged_row=row,
            severity=severity,
            code=code,
            field_name="historical_source",
            message=message,
            is_blocking=blocking,
        )
        for row, severity, code, message, blocking in pending_issues
    ]
    SalesImportIssue.objects.bulk_create(issues, batch_size=2000)
    blocking_count = sum(issue.is_blocking for issue in issues)
    warning_count = sum(issue.severity == SalesImportIssue.Severity.WARNING for issue in issues)
    batch.status = SalesImportBatch.Status.BLOCKED if blocking_count else SalesImportBatch.Status.READY
    batch.parser_version = PARSER_VERSION
    batch.total_rows = len(staged)
    batch.new_rows = sum(row.proposed_action == StagedSalesRow.ProposedAction.NEW for row in staged)
    batch.blocking_issue_count = blocking_count
    batch.warning_count = warning_count
    batch.data_start = min(dates) if dates else None
    batch.data_end = max(dates) if dates else None
    batch.previewed_at = timezone.now()
    batch.quality_summary = {
        **quality,
        "duplicate_business_key_count": len(duplicate_keys),
        "historical_period_start": str(HISTORY_START),
        "historical_period_end": str(HISTORY_END),
        "inventory_posting": False,
        "source_workbook": "Vobia Sales 2026",
        "source_sheet": "Transaction",
    }
    batch.save()
    return batch


@transaction.atomic
def commit_historical_sales_batch(batch_id, actor):
    batch = SalesImportBatch.objects.select_for_update().get(pk=batch_id)
    if batch.mode != SalesImportBatch.Mode.HISTORICAL or not batch.can_approve:
        raise ValidationError("Historical batch belum siap untuk commit.")
    if SalesOrder.objects.filter(order_date__range=(HISTORY_START, HISTORY_END)).exists():
        raise ValidationError("Sudah ada order Januari–Juli. Jalankan rekonsiliasi sebelum historical migration ulang.")

    rows = list(batch.staged_rows.filter(proposed_action=StagedSalesRow.ProposedAction.NEW).order_by("row_number"))
    headers = {}
    for row in rows:
        key = (row.source_label, row.order_number)
        current = headers.get(key)
        signature = (row.order_datetime, row.normalized_status, row.source_status, row.is_final)
        if current and current != signature:
            raise ValidationError(f"Header order tidak konsisten: {row.source_label}|{row.order_number}")
        headers[key] = signature

    orders = [
        SalesOrder(
            source=_source_type(source_label),
            source_label=source_label,
            order_number=order_number,
            order_datetime=signature[0],
            order_date=timezone.localtime(signature[0]).date(),
            current_status=signature[1],
            source_status=signature[2],
            is_final=signature[3],
            is_pure_cancelled=False,
            import_origin=SalesOrder.ImportOrigin.HISTORICAL,
            affects_inventory=False,
            first_seen_batch_id=batch.id,
            latest_batch_id=batch.id,
        )
        for (source_label, order_number), signature in headers.items()
    ]
    SalesOrder.objects.bulk_create(orders, batch_size=2000)
    order_map = {
        (order.source_label, order.order_number): order
        for order in SalesOrder.objects.filter(first_seen_batch_id=batch.id)
    }

    histories = []
    lines = []
    for order in order_map.values():
        histories.append(SalesStatusHistory(
            order=order,
            previous_status="",
            normalized_status=order.current_status,
            source_status=order.source_status,
            import_batch_id=batch.id,
            changed_by=actor,
        ))
    for row in rows:
        order = order_map[(row.source_label, row.order_number)]
        gross = row.total_gross_sales_snapshot
        margin = row.gpm_snapshot
        rate = (margin / gross) if margin is not None and gross else None
        lines.append(SalesOrderLine(
            order=order,
            sku=row.sku,
            sku_code_snapshot=row.sku_text,
            product_status_snapshot=row.product_status_snapshot,
            category_snapshot=row.category_snapshot,
            subcategory_snapshot=row.subcategory_snapshot,
            product_name_snapshot=row.product_name_snapshot,
            variant_name_snapshot=row.variant_name_snapshot,
            quantity=row.quantity,
            net_unit_price=row.net_unit_price,
            retail_price_snapshot=row.retail_price_snapshot,
            sales_cogs_snapshot=row.sales_cogs_snapshot,
            total_gross_sales=gross,
            total_net_sales=row.total_net_sales_snapshot,
            total_cogs=row.total_cogs_snapshot,
            gpm=margin,
            gpm_rate=rate,
            is_counted=True,
        ))
    SalesStatusHistory.objects.bulk_create(histories, batch_size=2000)
    SalesOrderLine.objects.bulk_create(lines, batch_size=2000)

    now = timezone.now()
    batch.status = SalesImportBatch.Status.COMMITTED
    batch.approved_by = actor
    batch.approved_at = now
    batch.committed_at = now
    batch.save(update_fields=["status", "approved_by", "approved_at", "committed_at"])
    counts = {
        "orders_created": len(orders),
        "lines_created": len(lines),
        "inventory_movements_created": 0,
        "unmapped_legacy_rows": batch.quality_summary.get("unmapped_legacy_rows", 0),
    }
    record_audit(
        actor=actor,
        action="historical_sales_migration_committed",
        entity_type="imports.salesimportbatch",
        entity_id=batch.id,
        after_values=counts,
        metadata={
            "checksum_sha256": batch.raw_file.checksum_sha256,
            "parser_version": batch.parser_version,
            "period": "2026-01-01..2026-07-31",
            "affects_inventory": False,
        },
    )
    return batch, counts


def create_historical_sales_import(source_path, actor):
    source_path = Path(source_path)
    if source_path.suffix.lower() != ".csv" or not source_path.is_file():
        raise ValidationError("Historical source harus berupa file CSV yang valid.")
    digest = hashlib.sha256()
    with source_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    checksum = digest.hexdigest()
    duplicate = RawFile.objects.filter(dataset_type=RawFile.DatasetType.SALES_HISTORICAL, checksum_sha256=checksum).first()
    if duplicate:
        existing = duplicate.sales_batches.order_by("-created_at").first()
        if existing:
            return existing
        raise ValidationError("File historical identik sudah tersimpan sebagai raw evidence.")

    current = timezone.localdate()
    relative = Path("sales") / "historical" / str(current.year) / f"{current.month:02d}" / f"{uuid.uuid4()}.csv"
    destination = Path(settings.PRIVATE_UPLOAD_ROOT) / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("rb") as source, destination.open("wb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            target.write(chunk)
    try:
        with transaction.atomic():
            raw = RawFile.objects.create(
                dataset_type=RawFile.DatasetType.SALES_HISTORICAL,
                original_filename=source_path.name,
                storage_path=str(relative),
                checksum_sha256=checksum,
                byte_size=source_path.stat().st_size,
                detected_format="csv",
                uploaded_by=actor,
                source_metadata={
                    "source": "Vobia Sales 2026",
                    "spreadsheet_id": "14iBjrGwlLtOKpk8O6cefDyOZAYztkX_fb22wToI41-k",
                    "sheet": "Transaction",
                    "mode": "historical_reporting_only",
                },
            )
            batch = SalesImportBatch.objects.create(
                raw_file=raw,
                source=SalesImportBatch.Source.CANONICAL,
                mode=SalesImportBatch.Mode.HISTORICAL,
                parser_version=PARSER_VERSION,
            )
            record_audit(
                actor=actor,
                action="historical_sales_migration_uploaded",
                entity_type="imports.salesimportbatch",
                entity_id=batch.id,
                metadata={"filename": source_path.name, "checksum_sha256": checksum},
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return parse_historical_sales_batch(batch)
