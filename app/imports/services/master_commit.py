import hashlib

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from audit.services import record_audit
from master_data.models import (
    Category,
    MarketplaceProductMapping,
    Product,
    ProductStatus,
    ProductVariant,
    SKU,
    SKUValueHistory,
    Subcategory,
)

from ..models import MasterImportBatch


def _code(value, max_length=50):
    base = slugify(value).replace("-", "_").upper() or "UNSPECIFIED"
    if len(base) <= max_length:
        return base
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8].upper()
    return f"{base[: max_length - 9]}_{digest}"


def _product_code(product_key):
    if len(product_key) <= 160:
        return product_key
    digest = hashlib.sha256(product_key.encode("utf-8")).hexdigest().upper()
    return f"PRODUCT::{digest}"


def _json_value(value):
    if value is None:
        return None
    return str(value)


def _changes(existing, row, variant):
    if existing is None:
        return {"created": True}
    product = existing.product_variant.product
    current = {
        "parent_sku": product.parent_sku,
        "article": product.article,
        "category": product.category.name,
        "subcategory": product.subcategory.name if product.subcategory else "",
        "variant": existing.product_variant.name,
        "size": existing.size,
        "status": product.status.name,
        "retail_price": _json_value(existing.current_retail_price),
        "master_cogs": _json_value(existing.current_master_cogs),
    }
    incoming = {
        "parent_sku": row.parent_sku,
        "article": row.article,
        "category": row.category,
        "subcategory": row.subcategory,
        "variant": variant,
        "size": row.sub_variant,
        "status": row.product_status,
        "retail_price": _json_value(row.retail_price),
        "master_cogs": _json_value(row.cogs),
    }
    return {
        field: {"before": current[field], "after": incoming[field]}
        for field in current
        if current[field] != incoming[field]
    }


def _get_status(name):
    code = _code(name)
    status, _ = ProductStatus.objects.get_or_create(code=code, defaults={"name": name})
    if status.name != name:
        status.name = name
        status.save(update_fields=["name", "updated_at"])
    return status


def _get_category(name):
    code = _code(name)
    category, _ = Category.objects.get_or_create(code=code, defaults={"name": name})
    if category.name != name:
        category.name = name
        category.save(update_fields=["name", "updated_at"])
    return category


def _get_subcategory(category, name):
    if not name:
        return None
    code = _code(name)
    subcategory, _ = Subcategory.objects.get_or_create(
        category=category,
        code=code,
        defaults={"name": name},
    )
    if subcategory.name != name:
        subcategory.name = name
        subcategory.save(update_fields=["name", "updated_at"])
    return subcategory


def _ensure_mapping(source, code, product):
    if not code:
        return False
    if not MarketplaceProductMapping.objects.filter(
        source=source,
        marketplace_product_code=code,
        product=product,
        valid_from__isnull=True,
        is_active=True,
    ).exists():
        MarketplaceProductMapping.objects.create(
            source=source,
            marketplace_product_code=code,
            product=product,
        )
        return True
    return False


@transaction.atomic
def approve_master_import(batch_id, actor):
    batch = MasterImportBatch.objects.select_for_update().get(pk=batch_id)
    if not batch.can_approve:
        raise ValidationError("Batch belum siap di-approve atau masih memiliki blocking issue.")
    if batch.issues.filter(is_blocking=True).exists():
        raise ValidationError("Blocking issue masih ada; commit dibatalkan.")

    rows = list(
        batch.staged_rows.select_related(
            "existing_sku__product_variant__product__status",
            "existing_sku__product_variant__product__category",
            "existing_sku__product_variant__product__subcategory",
        ).order_by("row_number")
    )
    committed_counts = {"created": 0, "updated": 0, "unchanged": 0}

    for row in rows:
        status = _get_status(row.product_status)
        category = _get_category(row.category)
        subcategory = _get_subcategory(category, row.subcategory)
        product_code = _product_code(row.product_key)
        product, _ = Product.objects.update_or_create(
            code=product_code,
            defaults={
                "parent_sku": row.parent_sku,
                "article": row.article,
                "name": row.article,
                "status": status,
                "category": category,
                "subcategory": subcategory,
                "is_active": True,
            },
        )
        product.full_clean()

        variant_name = row.variant or "Default"
        variant, _ = ProductVariant.objects.get_or_create(
            product=product,
            name=variant_name,
            color=row.variant,
            defaults={"is_active": True},
        )
        existing = SKU.objects.filter(sku=row.sku).select_related(
            "product_variant__product__status",
            "product_variant__product__category",
            "product_variant__product__subcategory",
        ).first()
        changes = _changes(existing, row, variant_name)

        is_new = existing is None
        if is_new:
            sku = SKU(sku=row.sku)
        else:
            sku = existing
        sku.product_variant = variant
        sku.size = row.sub_variant
        sku.current_retail_price = row.retail_price
        sku.current_master_cogs = row.cogs
        sku.is_active = True
        sku.full_clean()
        sku.save()

        mappings_added = []
        if _ensure_mapping(MarketplaceProductMapping.Source.SHOPEE, row.shopee_code, product):
            mappings_added.append({"source": "Shopee", "code": row.shopee_code})
        if _ensure_mapping(MarketplaceProductMapping.Source.TIKTOK, row.tiktok_code, product):
            mappings_added.append({"source": "Tiktok", "code": row.tiktok_code})
        if mappings_added and not is_new:
            changes["marketplace_mappings_added"] = mappings_added

        if is_new:
            committed_counts["created"] += 1
        elif changes:
            committed_counts["updated"] += 1
        else:
            committed_counts["unchanged"] += 1

        if changes:
            SKUValueHistory.objects.create(
                sku=sku,
                retail_price=row.retail_price,
                master_cogs=row.cogs,
                product_status=status,
                source_batch_id=batch.id,
                changed_by=actor,
                changes=changes,
            )

    now = timezone.now()
    batch.status = MasterImportBatch.Status.COMMITTED
    batch.approved_by = actor
    batch.approved_at = now
    batch.committed_at = now
    batch.save(update_fields=["status", "approved_by", "approved_at", "committed_at"])

    record_audit(
        actor=actor,
        action="master_import_committed",
        entity_type="imports.masterimportbatch",
        entity_id=batch.pk,
        after_values=committed_counts,
        metadata={
            "checksum_sha256": batch.raw_file.checksum_sha256,
            "row_count": batch.total_rows,
            "parser_version": batch.parser_version,
        },
    )
    return batch, committed_counts


@transaction.atomic
def cancel_master_import(batch_id, actor):
    batch = MasterImportBatch.objects.select_for_update().select_related("raw_file").get(
        pk=batch_id
    )
    if batch.status == MasterImportBatch.Status.COMMITTED:
        raise ValidationError("Import yang sudah committed tidak dapat dibatalkan.")
    if batch.status == MasterImportBatch.Status.REJECTED:
        return batch
    if batch.status not in {MasterImportBatch.Status.READY, MasterImportBatch.Status.BLOCKED}:
        raise ValidationError("Import belum siap untuk dibatalkan.")

    previous_status = batch.status
    batch.status = MasterImportBatch.Status.REJECTED
    batch.save(update_fields=["status"])
    record_audit(
        actor=actor,
        action="master_import_cancelled",
        entity_type="imports.masterimportbatch",
        entity_id=batch.pk,
        before_values={"status": previous_status},
        after_values={
            "status": batch.status,
            "raw_file_preserved": True,
            "commit_locked": True,
        },
        metadata={
            "filename": batch.raw_file.original_filename,
            "checksum_sha256": batch.raw_file.checksum_sha256,
        },
    )
    return batch
