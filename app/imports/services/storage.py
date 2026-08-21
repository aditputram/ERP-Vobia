import hashlib
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit

from ..models import MasterImportBatch, RawFile
from .master_parser import parse_master_batch
from .sales_parser import parse_sales_batch
from ..models import SalesImportBatch


class DuplicateRawFile(Exception):
    def __init__(self, raw_file):
        self.raw_file = raw_file
        super().__init__("File yang sama sudah pernah diunggah.")


def _checksum(uploaded_file):
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    uploaded_file.seek(0)
    return digest.hexdigest()


def create_master_import(uploaded_file, actor):
    extension = Path(uploaded_file.name).suffix.lower().lstrip(".")
    checksum = _checksum(uploaded_file)
    duplicate = RawFile.objects.filter(
        dataset_type=RawFile.DatasetType.MASTER_PRODUCT,
        checksum_sha256=checksum,
    ).first()
    if duplicate:
        raise DuplicateRawFile(duplicate)

    current_date = timezone.localdate()
    relative_path = Path("master_product") / str(current_date.year) / f"{current_date.month:02d}" / f"{uuid.uuid4()}.{extension}"
    absolute_path = Path(settings.PRIVATE_UPLOAD_ROOT) / relative_path
    absolute_path.parent.mkdir(parents=True, exist_ok=True)

    with absolute_path.open("wb") as destination:
        for chunk in uploaded_file.chunks():
            destination.write(chunk)
    uploaded_file.seek(0)

    try:
        with transaction.atomic():
            raw_file = RawFile.objects.create(
                dataset_type=RawFile.DatasetType.MASTER_PRODUCT,
                original_filename=Path(uploaded_file.name).name,
                storage_path=str(relative_path),
                checksum_sha256=checksum,
                byte_size=uploaded_file.size,
                detected_format=extension,
                uploaded_by=actor,
                source_metadata={
                    "canonical_workbook": "Bank Data All Source 26",
                    "spreadsheet_id": "1rf9-CDDYj0ks9AU3eJn_O6WMFc1S4649ksaGV9Fj364",
                    "canonical_tab": "VOBIA",
                },
            )
            batch = MasterImportBatch.objects.create(raw_file=raw_file)
            record_audit(
                actor=actor,
                action="master_import_uploaded",
                entity_type="imports.masterimportbatch",
                entity_id=batch.pk,
                metadata={
                    "filename": raw_file.original_filename,
                    "checksum_sha256": checksum,
                    "byte_size": raw_file.byte_size,
                },
            )
    except Exception:
        absolute_path.unlink(missing_ok=True)
        raise

    return parse_master_batch(batch)


def create_sales_import(uploaded_file, source, actor):
    extension = Path(uploaded_file.name).suffix.lower().lstrip(".")
    checksum = _checksum(uploaded_file)
    dataset_type = (
        RawFile.DatasetType.SALES_SHOPEE
        if source == SalesImportBatch.Source.SHOPEE
        else RawFile.DatasetType.SALES_TIKTOK
    )
    duplicate = RawFile.objects.filter(
        dataset_type=dataset_type,
        checksum_sha256=checksum,
    ).first()
    if duplicate:
        raise DuplicateRawFile(duplicate)

    current_date = timezone.localdate()
    relative_path = (
        Path("sales")
        / source.lower()
        / str(current_date.year)
        / f"{current_date.month:02d}"
        / f"{uuid.uuid4()}.{extension}"
    )
    absolute_path = Path(settings.PRIVATE_UPLOAD_ROOT) / relative_path
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    with absolute_path.open("wb") as destination:
        for chunk in uploaded_file.chunks():
            destination.write(chunk)
    uploaded_file.seek(0)

    try:
        with transaction.atomic():
            raw_file = RawFile.objects.create(
                dataset_type=dataset_type,
                original_filename=Path(uploaded_file.name).name,
                storage_path=str(relative_path),
                checksum_sha256=checksum,
                byte_size=uploaded_file.size,
                detected_format=extension,
                uploaded_by=actor,
                source_metadata={"source": source, "dataset": "sales_transaction"},
            )
            batch = SalesImportBatch.objects.create(raw_file=raw_file, source=source)
            record_audit(
                actor=actor,
                action="sales_import_uploaded",
                entity_type="imports.salesimportbatch",
                entity_id=batch.pk,
                metadata={
                    "source": source,
                    "filename": raw_file.original_filename,
                    "checksum_sha256": checksum,
                    "byte_size": raw_file.byte_size,
                },
            )
    except Exception:
        absolute_path.unlink(missing_ok=True)
        raise
    return parse_sales_batch(batch)
