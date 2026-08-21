from collections import Counter

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit

from ..models import SalesImportBatch, SalesImportIssue, StagedSalesRow


@transaction.atomic
def override_staged_order_as_pure_cancel(*, batch_id, order_number, actor, reason):
    """Apply an explicit live-marketplace cancellation without rewriting raw evidence."""

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Alasan override status marketplace wajib diisi.")

    batch = SalesImportBatch.objects.select_for_update().select_related("raw_file").get(pk=batch_id)
    if batch.status != SalesImportBatch.Status.READY:
        raise ValidationError("Override hanya boleh dilakukan pada batch yang siap direview dan belum committed.")

    rows = list(
        batch.staged_rows.select_for_update()
        .filter(order_number=order_number)
        .select_related("existing_line__order")
        .order_by("row_number")
    )
    if not rows:
        raise ValidationError("Order tidak ditemukan pada staging batch ini.")
    if any(row.shipped_datetime for row in rows):
        raise ValidationError("Order yang sudah dikirim tidak boleh menjadi Pure Cancel; gunakan workflow Retur.")
    if any(row.proposed_action in {StagedSalesRow.ProposedAction.BLOCKED, StagedSalesRow.ProposedAction.OUT_OF_SCOPE} for row in rows):
        raise ValidationError("Order blocked atau before cutover tidak boleh dioverride melalui workflow ini.")

    now = timezone.now()
    before_rows = []
    updated_rows = []
    for row in rows:
        before_rows.append(
            {
                "row_number": row.row_number,
                "source_status": row.source_status,
                "normalized_status": row.normalized_status,
                "proposed_action": row.proposed_action,
                "is_final": row.is_final,
                "is_pure_cancelled": row.is_pure_cancelled,
            }
        )
        source_data = dict(row.selected_source_data or {})
        source_data["manual_status_override"] = {
            "original_source_status": row.source_status,
            "overridden_source_status": "Batal",
            "verified_at": now.isoformat(),
            "verified_by": actor.username,
            "reason": reason,
            "raw_file_preserved": True,
        }
        row.selected_source_data = source_data
        row.source_status = "Batal"
        row.normalized_status = "Batal"
        row.is_final = True
        row.is_pure_cancelled = True
        row.proposed_action = StagedSalesRow.ProposedAction.PURE_CANCEL
        updated_rows.append(row)

    StagedSalesRow.objects.bulk_update(
        updated_rows,
        [
            "selected_source_data",
            "source_status",
            "normalized_status",
            "is_final",
            "is_pure_cancelled",
            "proposed_action",
        ],
    )

    SalesImportIssue.objects.filter(
        batch=batch,
        staged_row__in=rows,
        code="MANUAL_STATUS_OVERRIDE",
    ).delete()
    SalesImportIssue.objects.create(
        batch=batch,
        staged_row=rows[0],
        severity=SalesImportIssue.Severity.WARNING,
        code="MANUAL_STATUS_OVERRIDE",
        field_name="status",
        message=(
            f"Order {order_number} dioverride menjadi Batal berdasarkan verifikasi marketplace live. "
            "Status dari raw file tetap tersimpan di audit trail."
        ),
        is_blocking=False,
    )

    action_counts = Counter(batch.staged_rows.values_list("proposed_action", flat=True))
    batch.new_rows = action_counts[StagedSalesRow.ProposedAction.NEW]
    batch.status_update_rows = action_counts[StagedSalesRow.ProposedAction.STATUS_UPDATE]
    batch.unchanged_rows = action_counts[StagedSalesRow.ProposedAction.UNCHANGED]
    batch.ignored_cancel_rows = action_counts[StagedSalesRow.ProposedAction.PURE_CANCEL]
    batch.out_of_scope_rows = action_counts[StagedSalesRow.ProposedAction.OUT_OF_SCOPE]
    batch.warning_count = batch.issues.filter(severity=SalesImportIssue.Severity.WARNING).count()

    quality = dict(batch.quality_summary or {})
    quality["manual_status_override_rows"] = sum(
        1 for row in batch.staged_rows.all() if row.selected_source_data.get("manual_status_override")
    )
    overrides = list(quality.get("manual_status_overrides", []))
    overrides = [item for item in overrides if item.get("order_number") != order_number]
    overrides.append(
        {
            "order_number": order_number,
            "row_count": len(rows),
            "status": "Batal",
            "verified_at": now.isoformat(),
            "verified_by": actor.username,
            "reason": reason,
        }
    )
    quality["manual_status_overrides"] = overrides
    quality["nonfinal_rows"] = max(0, int(quality.get("nonfinal_rows", 0)) - len(rows))
    quality["pure_cancel_rows"] = int(quality.get("pure_cancel_rows", 0)) + len(rows)
    previous_status_key = f"status::{before_rows[0]['normalized_status']}"
    if previous_status_key in quality:
        quality[previous_status_key] = max(0, int(quality[previous_status_key]) - len(rows))
    batch.quality_summary = quality
    batch.save(
        update_fields=[
            "new_rows",
            "status_update_rows",
            "unchanged_rows",
            "ignored_cancel_rows",
            "out_of_scope_rows",
            "warning_count",
            "quality_summary",
        ]
    )

    record_audit(
        actor=actor,
        action="sales_import_status_overridden",
        entity_type="imports.salesimportbatch",
        entity_id=batch.id,
        reason=reason,
        before_values={"order_number": order_number, "rows": before_rows},
        after_values={
            "order_number": order_number,
            "row_count": len(rows),
            "source_status": "Batal",
            "normalized_status": "Batal",
            "proposed_action": StagedSalesRow.ProposedAction.PURE_CANCEL,
            "raw_file_preserved": True,
        },
        metadata={
            "source": batch.source,
            "filename": batch.raw_file.original_filename,
            "checksum_sha256": batch.raw_file.checksum_sha256,
        },
    )
    return batch
