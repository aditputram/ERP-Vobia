from django.core.files.base import ContentFile
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from audit.services import record_audit

from .documents import build_combined_document
from .models import (
    Collection,
    DevelopmentProduct,
    DevelopmentProductDocumentRevision,
    MarketingRecommendation,
)


def can_approve_module(user, module):
    default_level = "none" if module == "rnd" else "approve"
    return user.is_superuser or (user.module_access or {}).get(module, default_level) == "approve"


def _actor_name(actor):
    return actor.get_full_name().strip() or actor.get_username()


@transaction.atomic
def submit_product_document(*, product, actor):
    product = DevelopmentProduct.objects.select_for_update().select_related("collection").get(pk=product.pk)
    if product.collection.status not in {Collection.Status.DRAFT, Collection.Status.DEVELOPMENT}:
        raise ValidationError("Product sudah dikunci setelah handover ke Marketing.")
    if product.document_status != DevelopmentProduct.DocumentStatus.DRAFT:
        raise ValidationError("Dokumen Product ini sudah diajukan.")

    submitted_at = timezone.now()
    document = build_combined_document(product=product, submitted_at=submitted_at)
    document_stem = slugify(f"{product.collection.code}-{product.name}") or str(product.id)
    filename = f"{document_stem}-rev-{product.document_revision:03d}-submitted.pdf"
    product.submitted_document.save(filename, ContentFile(document), save=False)
    product.document_status = DevelopmentProduct.DocumentStatus.SUBMITTED
    product.submitted_at = submitted_at
    product.submitted_by = actor
    product.save(
        update_fields=(
            "submitted_document",
            "document_status",
            "submitted_at",
            "submitted_by",
            "updated_at",
        )
    )
    revision = DevelopmentProductDocumentRevision(
        product=product,
        revision=product.document_revision,
        status=DevelopmentProductDocumentRevision.Status.SUBMITTED,
        submitted_at=submitted_at,
        submitted_by=actor,
    )
    revision.submitted_document.name = product.submitted_document.name
    revision.save()
    record_audit(
        actor=actor,
        action="rnd_product_document_submitted",
        entity_type="rnd.development_product",
        entity_id=product.id,
        after_values={
            "document_status": product.document_status,
            "revision": f"{product.document_revision:03d}",
            "submitted_at": submitted_at.isoformat(),
        },
    )
    return product


@transaction.atomic
def approve_product_document(*, product, actor):
    if not actor.is_superuser:
        raise PermissionDenied("Approval dokumen R&D hanya dapat dilakukan Super Admin.")
    product = DevelopmentProduct.objects.select_for_update().select_related("collection").get(pk=product.pk)
    if product.document_status != DevelopmentProduct.DocumentStatus.SUBMITTED:
        raise ValidationError("Dokumen Product belum diajukan atau sudah di-approve.")

    approved_at = timezone.now()
    document = build_combined_document(
        product=product,
        submitted_at=product.submitted_at,
        approved_at=approved_at,
        approved_by=_actor_name(actor),
    )
    document_stem = slugify(f"{product.collection.code}-{product.name}") or str(product.id)
    filename = f"{document_stem}-rev-{product.document_revision:03d}-approved.pdf"
    product.approved_document.save(filename, ContentFile(document), save=False)
    product.document_status = DevelopmentProduct.DocumentStatus.APPROVED
    product.status = DevelopmentProduct.Status.SAMPLING
    product.rnd_approved_at = approved_at
    product.rnd_approved_by = actor
    product.save(
        update_fields=(
            "approved_document",
            "document_status",
            "status",
            "rnd_approved_at",
            "rnd_approved_by",
            "updated_at",
        )
    )
    revision = DevelopmentProductDocumentRevision.objects.select_for_update().get(
        product=product,
        revision=product.document_revision,
    )
    revision.status = DevelopmentProductDocumentRevision.Status.APPROVED
    revision.approved_document.name = product.approved_document.name
    revision.approved_at = approved_at
    revision.approved_by = actor
    revision.save(
        update_fields=(
            "status",
            "approved_document",
            "approved_at",
            "approved_by",
            "updated_at",
        )
    )
    record_audit(
        actor=actor,
        action="rnd_product_document_approved",
        entity_type="rnd.development_product",
        entity_id=product.id,
        after_values={
            "document_status": product.document_status,
            "revision": f"{product.document_revision:03d}",
            "approved_at": approved_at.isoformat(),
            "approved_by": _actor_name(actor),
        },
    )
    return product


@transaction.atomic
def request_product_document_revision(*, product, actor, note, target):
    if not actor.is_superuser:
        raise PermissionDenied("Revisi dokumen R&D hanya dapat diminta oleh Super Admin.")
    note = (note or "").strip()
    if not note:
        raise ValidationError("Notes revisi wajib diisi.")
    if len(note) > 2000:
        raise ValidationError("Notes revisi maksimal 2.000 karakter.")
    if target not in DevelopmentProductDocumentRevision.RevisionTarget.values:
        raise ValidationError("Pilih bagian dokumen yang harus direvisi.")
    product = DevelopmentProduct.objects.select_for_update().select_related("collection").get(pk=product.pk)
    if product.document_status not in {
        DevelopmentProduct.DocumentStatus.SUBMITTED,
        DevelopmentProduct.DocumentStatus.APPROVED,
        DevelopmentProduct.DocumentStatus.REVISION_REQUESTED,
    }:
        raise ValidationError("Dokumen Product belum diajukan atau sudah dalam proses revisi.")
    revision = DevelopmentProductDocumentRevision.objects.select_for_update().get(
        product=product,
        revision=product.document_revision,
    )
    if product.document_status == DevelopmentProduct.DocumentStatus.REVISION_REQUESTED:
        if revision.revision_note and revision.revision_target:
            raise ValidationError("Permintaan revisi sudah lengkap.")
        update_fields = ["updated_at"]
        if not revision.revision_note:
            revision.revision_note = note
            update_fields.append("revision_note")
        if not revision.revision_target:
            revision.revision_target = target
            update_fields.append("revision_target")
        revision.save(update_fields=update_fields)
        record_audit(
            actor=actor,
            action="rnd_product_document_revision_note_added",
            entity_type="rnd.development_product",
            entity_id=product.id,
            after_values={
                "revision": f"{product.document_revision:03d}",
                "revision_note": revision.revision_note,
                "revision_target": revision.revision_target,
            },
        )
        return product
    requested_at = timezone.now()
    revision.status = DevelopmentProductDocumentRevision.Status.REVISION_REQUESTED
    revision.revision_requested_at = requested_at
    revision.revision_note = note
    revision.revision_target = target
    revision.revision_requested_by = actor
    revision.save(
        update_fields=(
            "status",
            "revision_requested_at",
            "revision_note",
            "revision_target",
            "revision_requested_by",
            "updated_at",
        )
    )
    product.document_status = DevelopmentProduct.DocumentStatus.REVISION_REQUESTED
    product.status = DevelopmentProduct.Status.REVISION
    product.save(update_fields=("document_status", "status", "updated_at"))
    record_audit(
        actor=actor,
        action="rnd_product_document_revision_requested",
        entity_type="rnd.development_product",
        entity_id=product.id,
        after_values={
            "document_status": product.document_status,
            "revision": f"{product.document_revision:03d}",
            "revision_note": note,
            "revision_target": target,
            "requested_at": requested_at.isoformat(),
        },
    )
    return product


@transaction.atomic
def move_product_to_costing(*, product, actor):
    product = DevelopmentProduct.objects.select_for_update().select_related("collection").get(pk=product.pk)
    if product.collection.status not in {Collection.Status.DRAFT, Collection.Status.DEVELOPMENT}:
        raise ValidationError("Product sudah dikunci setelah handover ke Marketing.")
    if product.document_status != DevelopmentProduct.DocumentStatus.APPROVED:
        raise ValidationError("Dokumen Product wajib Approved sebelum masuk ke Costing.")
    if product.status != DevelopmentProduct.Status.SAMPLING:
        raise ValidationError("Hanya Product berstatus Sampling yang dapat dilanjutkan ke Costing.")
    product.status = DevelopmentProduct.Status.COSTING
    product.save(update_fields=("status", "updated_at"))
    record_audit(
        actor=actor,
        action="rnd_product_moved_to_costing",
        entity_type="rnd.development_product",
        entity_id=product.id,
        after_values={"status": product.status},
    )
    return product


@transaction.atomic
def finalize_product(*, product, actor):
    if not can_approve_module(actor, "rnd"):
        raise PermissionDenied("Final R&D memerlukan akses Approve R&D.")
    product = DevelopmentProduct.objects.select_for_update().select_related("collection").get(pk=product.pk)
    if product.collection.status not in {Collection.Status.DRAFT, Collection.Status.DEVELOPMENT}:
        raise ValidationError("Product sudah dikunci setelah handover ke Marketing.")
    if product.document_status != DevelopmentProduct.DocumentStatus.APPROVED:
        raise ValidationError("Dokumen Product wajib Approved sebelum ditetapkan Final R&D.")
    if product.status != DevelopmentProduct.Status.COSTING:
        raise ValidationError("Product wajib menyelesaikan tahap Costing sebelum Final R&D.")
    product.status = DevelopmentProduct.Status.FINAL_APPROVED
    product.save(update_fields=("status", "updated_at"))
    record_audit(
        actor=actor,
        action="rnd_product_finalized",
        entity_type="rnd.development_product",
        entity_id=product.id,
        after_values={"status": product.status},
    )
    return product


@transaction.atomic
def delete_collection(*, collection, actor):
    if not can_approve_module(actor, "rnd"):
        raise PermissionDenied("Delete Collection memerlukan akses Approve R&D.")
    collection = Collection.objects.select_for_update().get(pk=collection.pk)
    if collection.status not in {Collection.Status.DRAFT, Collection.Status.DEVELOPMENT}:
        raise ValidationError("Collection yang sudah di-handover ke Marketing tidak dapat dihapus.")

    products = list(collection.products.prefetch_related("document_revisions"))
    if MarketingRecommendation.objects.filter(product__in=products).exists():
        raise ValidationError("Collection yang sudah memiliki rekomendasi Marketing tidak dapat dihapus.")

    files = {}
    for product in products:
        for field_name in (
            "product_cover",
            "mockup",
            "technical_drawing",
            "submitted_document",
            "approved_document",
        ):
            file = getattr(product, field_name)
            if file.name:
                files[(id(file.storage), file.name)] = (file.storage, file.name)
        for revision in product.document_revisions.all():
            for field_name in ("submitted_document", "approved_document"):
                file = getattr(revision, field_name)
                if file.name:
                    files[(id(file.storage), file.name)] = (file.storage, file.name)

    snapshot = {
        "code": collection.code,
        "name": collection.name,
        "status": collection.status,
        "product_count": len(products),
    }
    collection_id = collection.id
    collection.products.all().delete()
    collection.delete()
    record_audit(
        actor=actor,
        action="rnd_collection_deleted",
        entity_type="rnd.collection",
        entity_id=collection_id,
        before_values=snapshot,
        after_values={"deleted": True},
    )
    for storage, name in files.values():
        transaction.on_commit(lambda storage=storage, name=name: storage.delete(name), robust=True)
    return snapshot


@transaction.atomic
def handover_to_marketing(*, collection, actor):
    if not can_approve_module(actor, "rnd"):
        raise PermissionDenied("Handover Collection memerlukan akses Approve R&D.")
    collection = Collection.objects.select_for_update().get(pk=collection.pk)
    products = collection.products.all()
    if collection.status not in {Collection.Status.DRAFT, Collection.Status.DEVELOPMENT}:
        raise ValidationError("Collection ini tidak dapat di-handover ulang.")
    if not products.exists():
        raise ValidationError("Collection wajib memiliki minimal satu Product.")
    if products.exclude(
        status=DevelopmentProduct.Status.FINAL_APPROVED,
        document_status=DevelopmentProduct.DocumentStatus.APPROVED,
    ).exists():
        raise ValidationError("Seluruh Product wajib Final R&D sebelum handover ke Marketing.")
    collection.status = Collection.Status.MARKETING_REVIEW
    collection.handed_over_at = timezone.now()
    collection.handed_over_by = actor
    collection.save(update_fields=("status", "handed_over_at", "handed_over_by", "updated_at"))
    record_audit(
        actor=actor,
        action="rnd_collection_handed_to_marketing",
        entity_type="rnd.collection",
        entity_id=collection.id,
        after_values={"status": collection.status, "product_count": products.count()},
    )
    return collection


@transaction.atomic
def approve_collection_commercially(*, collection, actor):
    if not actor.is_superuser:
        raise PermissionDenied("Official Commercial Approval hanya dapat dilakukan Super Admin.")
    collection = Collection.objects.select_for_update().get(pk=collection.pk)
    if collection.status != Collection.Status.MARKETING_REVIEW:
        raise ValidationError("Collection tidak sedang berada pada Marketing Review.")
    products = list(collection.products.select_related("marketing_recommendation"))
    if not products:
        raise ValidationError("Collection tidak memiliki Product.")
    decisions = []
    for product in products:
        try:
            recommendation = product.marketing_recommendation
        except MarketingRecommendation.DoesNotExist:
            raise ValidationError("Seluruh Product wajib memiliki rekomendasi Marketing.")
        if recommendation.official_decision not in {
            recommendation.Decision.GO,
            recommendation.Decision.DROP,
        }:
            raise ValidationError("Official Decision seluruh Product harus GO atau DROP.")
        decisions.append(recommendation.official_decision)
    if MarketingRecommendation.Decision.GO not in decisions:
        raise ValidationError("Minimal satu Product wajib memiliki Official Decision GO.")
    collection.status = Collection.Status.COMMERCIAL_APPROVED
    collection.commercial_approved_at = timezone.now()
    collection.commercial_approved_by = actor
    collection.save(
        update_fields=(
            "status",
            "commercial_approved_at",
            "commercial_approved_by",
            "updated_at",
        )
    )
    record_audit(
        actor=actor,
        action="rnd_collection_commercially_approved",
        entity_type="rnd.collection",
        entity_id=collection.id,
        after_values={"status": collection.status, "decisions": decisions},
    )
    return collection
