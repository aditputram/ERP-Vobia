from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit
from inventory.services.fifo import create_expected_return, post_sales_out
from inventory.models import FIFOOpeningImportBatch
from sales.models import SalesOrder, SalesOrderLine, SalesStatusHistory

from ..models import SalesImportBatch, StagedSalesRow


MONEY_2 = Decimal("0.01")
MONEY_4 = Decimal("0.0001")
RATE_8 = Decimal("0.00000001")


def _q(value, quantum):
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _status_summary(rows):
    rows = list(rows)
    current_statuses = {
        row.current_status if isinstance(row, SalesOrderLine) else row.normalized_status
        for row in rows
    }
    source_statuses = {row.source_status for row in rows}
    return {
        "current_status": next(iter(current_statuses)) if len(current_statuses) == 1 else "Campuran",
        "source_status": next(iter(source_statuses)) if len(source_statuses) == 1 else "Campuran",
        "is_final": all(row.is_final for row in rows),
        "is_pure_cancelled": all(row.is_pure_cancelled for row in rows),
    }


def _sync_order_status(order):
    summary = _status_summary(order.lines.all())
    for field, value in summary.items():
        setattr(order, field, value)
    order.save(
        update_fields=[
            "current_status",
            "source_status",
            "is_final",
            "is_pure_cancelled",
            "latest_batch_id",
            "updated_at",
        ]
    )


@transaction.atomic
def approve_sales_import(batch_id, actor):
    if not settings.SALES_IMPORT_COMMIT_ENABLED:
        raise ValidationError(
            "Sales commit belum diaktifkan sampai Inventory Movement/FIFO posting terintegrasi."
        )
    if not FIFOOpeningImportBatch.objects.filter(status=FIFOOpeningImportBatch.Status.COMMITTED).exists():
        raise ValidationError(
            "FIFO Opening 1 Agustus belum selesai di-commit. Sales boleh dipreview, tetapi belum boleh membentuk movement stock."
        )
    batch = SalesImportBatch.objects.select_for_update().select_related("raw_file").get(pk=batch_id)
    if not batch.can_approve or batch.issues.filter(is_blocking=True).exists():
        raise ValidationError("Batch Sales belum siap atau masih memiliki blocking issue.")

    staged_rows = list(
        batch.staged_rows.exclude(
            proposed_action=StagedSalesRow.ProposedAction.OUT_OF_SCOPE
        ).select_related("sku", "existing_line__order", "existing_line__sku").order_by("row_number")
    )
    order_groups = {}
    for row in staged_rows:
        order_groups.setdefault(row.order_number, []).append(row)

    counts = {
        "orders_created": 0,
        "lines_created": 0,
        "status_updates": 0,
        "pure_cancellations_ignored": 0,
        "out_of_scope_ignored": batch.out_of_scope_rows,
        "unchanged": 0,
        "historical_status_audit_orders": 0,
        "historical_status_updates": 0,
    }

    for order_number, rows in order_groups.items():
        representative = rows[0]
        existing_order = SalesOrder.objects.select_for_update().filter(
            source=batch.source,
            order_number=order_number,
        ).first()
        is_historical_status_audit = bool(
            representative.selected_source_data.get("historical_status_audit")
        )

        if is_historical_status_audit:
            if (
                existing_order is None
                or existing_order.import_origin != SalesOrder.ImportOrigin.HISTORICAL
                or existing_order.affects_inventory
                or existing_order.order_date >= date(2026, 8, 1)
            ):
                raise ValidationError(
                    f"Historical Status Audit tidak aman untuk order {order_number}."
                )
            previous_status = existing_order.current_status
            before_values = {
                "current_status": existing_order.current_status,
                "source_status": existing_order.source_status,
                "is_final": existing_order.is_final,
            }
            changed_rows = 0
            for row in rows:
                if not row.selected_source_data.get("historical_status_update_allowed", True):
                    counts["unchanged"] += 1
                    continue
                line = row.existing_line
                if line is None:
                    counts["unchanged"] += 1
                    continue
                changed = (
                    line.current_status != row.normalized_status
                    or line.source_status != row.source_status
                    or line.is_final != row.is_final
                    or line.is_pure_cancelled != row.is_pure_cancelled
                )
                if not changed:
                    counts["unchanged"] += 1
                    continue
                line.current_status = row.normalized_status
                line.source_status = row.source_status
                line.is_final = row.is_final
                line.is_pure_cancelled = row.is_pure_cancelled
                line.save(
                    update_fields=[
                        "current_status",
                        "source_status",
                        "is_final",
                        "is_pure_cancelled",
                    ]
                )
                changed_rows += 1
            existing_order.latest_batch_id = batch.id
            _sync_order_status(existing_order)
            if previous_status != existing_order.current_status:
                SalesStatusHistory.objects.create(
                    order=existing_order,
                    previous_status=previous_status,
                    normalized_status=existing_order.current_status,
                    source_status=existing_order.source_status,
                    import_batch_id=batch.id,
                    changed_by=actor,
                )
            if changed_rows:
                record_audit(
                    actor=actor,
                    action="historical_status_audit_applied",
                    entity_type="sales.salesorder",
                    entity_id=existing_order.id,
                    reason="Status raw marketplace pra-cutover diverifikasi per SKU terhadap order historical.",
                    before_values=before_values,
                    after_values={
                        "current_status": existing_order.current_status,
                        "source_status": existing_order.source_status,
                        "is_final": existing_order.is_final,
                        "changed_sku_rows": changed_rows,
                        "financial_snapshot_changed": False,
                        "inventory_posting": False,
                        "import_batch_id": str(batch.id),
                    },
                )
                counts["status_updates"] += changed_rows
                counts["historical_status_updates"] += changed_rows
            counts["historical_status_audit_orders"] += 1
            continue

        active_rows = [row for row in rows if not row.is_pure_cancelled]
        if existing_order is None and not active_rows:
            counts["pure_cancellations_ignored"] += len(rows)
            continue

        order_created = existing_order is None
        previous_status = existing_order.current_status if existing_order else ""
        if order_created:
            initial = _status_summary(active_rows)
            order = SalesOrder.objects.create(
                source=batch.source,
                source_label=batch.source,
                order_number=order_number,
                order_datetime=representative.order_datetime,
                shipped_datetime=next(
                    (row.shipped_datetime for row in rows if row.shipped_datetime),
                    None,
                ),
                order_date=timezone.localtime(representative.order_datetime).date(),
                import_origin=SalesOrder.ImportOrigin.OPERATIONAL,
                affects_inventory=True,
                first_seen_batch_id=batch.id,
                latest_batch_id=batch.id,
                **initial,
            )
            counts["orders_created"] += 1
        else:
            order = existing_order
            if not order.source_label:
                order.source_label = batch.source
            if not order.shipped_datetime:
                order.shipped_datetime = next(
                    (row.shipped_datetime for row in rows if row.shipped_datetime),
                    None,
                )
            order.latest_batch_id = batch.id
            order.save(update_fields=["source_label", "shipped_datetime", "latest_batch_id", "updated_at"])

        for row in rows:
            if row.existing_line:
                line = row.existing_line
                changed = (
                    line.current_status != row.normalized_status
                    or line.source_status != row.source_status
                    or line.is_final != row.is_final
                    or line.is_pure_cancelled != row.is_pure_cancelled
                    or (row.is_pure_cancelled and line.is_counted)
                )
                if not changed:
                    counts["unchanged"] += 1
                    continue
                before_line_status = {
                    "current_status": line.current_status,
                    "source_status": line.source_status,
                    "is_final": line.is_final,
                    "is_pure_cancelled": line.is_pure_cancelled,
                    "is_counted": line.is_counted,
                }
                line.current_status = row.normalized_status
                line.source_status = row.source_status
                line.is_final = row.is_final
                line.is_pure_cancelled = row.is_pure_cancelled
                if row.is_pure_cancelled:
                    line.is_counted = False
                line.save(
                    update_fields=[
                        "current_status",
                        "source_status",
                        "is_final",
                        "is_pure_cancelled",
                        "is_counted",
                    ]
                )
                counts["status_updates"] += 1
                record_audit(
                    actor=actor,
                    action="sales_line_status_updated",
                    entity_type="sales.salesorderline",
                    entity_id=line.id,
                    before_values=before_line_status,
                    after_values={
                        "current_status": line.current_status,
                        "source_status": line.source_status,
                        "is_final": line.is_final,
                        "is_pure_cancelled": line.is_pure_cancelled,
                        "is_counted": line.is_counted,
                        "import_batch_id": str(batch.id),
                    },
                )
                continue
            if row.is_pure_cancelled:
                counts["pure_cancellations_ignored"] += 1
                continue
            if not all(
                (
                    row.sku_id,
                    row.quantity,
                    row.net_unit_price is not None,
                    row.retail_price_snapshot is not None,
                    row.sales_cogs_snapshot is not None,
                )
            ):
                raise ValidationError(f"Staged row {row.row_number} tidak lengkap.")
            if row.net_unit_price > row.retail_price_snapshot:
                raise ValidationError(
                    f"Staged row {row.row_number}: harga net per unit tidak boleh lebih besar "
                    "dari Retail Price snapshot."
                )

            quantity = Decimal(row.quantity)
            gross = _q(quantity * row.retail_price_snapshot, MONEY_2)
            net = _q(quantity * row.net_unit_price, MONEY_4)
            total_cogs = _q(quantity * row.sales_cogs_snapshot, MONEY_4)
            gpm = _q(net - total_cogs, MONEY_4)
            gpm_rate = _q(gpm / gross, RATE_8) if gross else None
            line = SalesOrderLine.objects.create(
                order=order,
                sku=row.sku,
                sku_code_snapshot=row.sku_text,
                product_status_snapshot=row.product_status_snapshot,
                category_snapshot=row.category_snapshot,
                subcategory_snapshot=row.subcategory_snapshot,
                product_name_snapshot=row.product_name_snapshot,
                variant_name_snapshot=row.variant_name_snapshot,
                current_status=row.normalized_status,
                source_status=row.source_status,
                is_final=row.is_final,
                is_pure_cancelled=row.is_pure_cancelled,
                quantity=row.quantity,
                net_unit_price=_q(row.net_unit_price, MONEY_4),
                retail_price_snapshot=_q(row.retail_price_snapshot, MONEY_2),
                sales_cogs_snapshot=_q(row.sales_cogs_snapshot, MONEY_4),
                total_gross_sales=gross,
                total_net_sales=net,
                total_cogs=total_cogs,
                gpm=gpm,
                gpm_rate=gpm_rate,
                is_counted=True,
            )
            if row.selected_source_data.get("retail_price_rule") == "transaction_special_case":
                record_audit(
                    actor=actor,
                    action="retail_price_snapshot_special_case",
                    entity_type="sales.salesorderline",
                    entity_id=line.id,
                    reason="Net Unit Price marketplace melebihi Retail Price master saat import.",
                    before_values={
                        "master_retail_price": row.selected_source_data.get("master_retail_price", ""),
                    },
                    after_values={
                        "retail_price_snapshot": str(row.retail_price_snapshot),
                        "master_retail_price_changed": False,
                        "import_batch_id": str(batch.id),
                    },
                )
            counts["lines_created"] += 1

        _sync_order_status(order)
        if order_created or previous_status != order.current_status:
            SalesStatusHistory.objects.create(
                order=order,
                previous_status=previous_status,
                normalized_status=order.current_status,
                source_status=order.source_status,
                import_batch_id=batch.id,
                changed_by=actor,
            )

        if order.affects_inventory:
            for line in order.lines.filter(is_counted=True).select_related("order", "sku"):
                if not (order.shipped_datetime or line.is_final):
                    continue
                post_sales_out(line, actor)
                if line.current_status == "Retur":
                    create_expected_return(line)

    now = timezone.now()
    batch.status = SalesImportBatch.Status.COMMITTED
    batch.approved_by = actor
    batch.approved_at = now
    batch.committed_at = now
    batch.save(update_fields=["status", "approved_by", "approved_at", "committed_at"])
    record_audit(
        actor=actor,
        action="sales_import_committed",
        entity_type="imports.salesimportbatch",
        entity_id=batch.id,
        after_values=counts,
        metadata={
            "source": batch.source,
            "checksum_sha256": batch.raw_file.checksum_sha256,
            "parser_version": batch.parser_version,
        },
    )
    return batch, counts
