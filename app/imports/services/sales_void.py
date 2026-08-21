from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit

from ..models import SalesImportBatch


@transaction.atomic
def void_sales_import(batch_id, actor, reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Alasan pembatalan batch Sales wajib diisi.")

    batch = SalesImportBatch.objects.select_for_update().select_related("raw_file").get(pk=batch_id)
    if batch.status == SalesImportBatch.Status.COMMITTED:
        raise ValidationError("Batch Sales yang sudah committed tidak boleh dibatalkan.")
    if batch.status == SalesImportBatch.Status.VOIDED:
        return batch

    previous_status = batch.status
    batch.status = SalesImportBatch.Status.VOIDED
    batch.voided_at = timezone.now()
    batch.voided_by = actor
    batch.void_reason = reason
    batch.save(update_fields=["status", "voided_at", "voided_by", "void_reason"])
    record_audit(
        actor=actor,
        action="sales_import_voided",
        entity_type="imports.salesimportbatch",
        entity_id=batch.id,
        reason=reason,
        before_values={"status": previous_status},
        after_values={
            "status": batch.status,
            "raw_file_preserved": True,
            "commit_locked": True,
        },
        metadata={
            "source": batch.source,
            "filename": batch.raw_file.original_filename,
            "checksum_sha256": batch.raw_file.checksum_sha256,
        },
    )
    return batch
