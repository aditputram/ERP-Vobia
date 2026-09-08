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


def can_edit_module(user, module):
    default_level = "none" if module == "rnd" else "approve"
    return user.is_superuser or (user.module_access or {}).get(module, default_level) in {"edit", "approve"}


def _actor_name(actor):
    return actor.get_full_name().strip() or actor.get_username()


@transaction.atomic
def sync_collection_status(*, collection, actor=None):
    collection = Collection.objects.select_for_update().get(pk=collection.pk)
    products = collection.products.all()
    if collection.commercial_approved_at:
        status = Collection.Status.COMMERCIAL_APPROVED
    elif collection.handed_over_at:
        status = Collection.Status.MARKETING_REVIEW
    elif collection.development_started_at:
        status = (
            Collection.Status.FINAL_DEVELOPMENT
            if products.exists()
            and not products.exclude(
                development_stage=DevelopmentProduct.DevelopmentStage.FINAL
            ).exists()
            else Collection.Status.DEVELOPMENT
        )
    else:
        document_statuses = list(products.values_list("document_status", flat=True))
        if not document_statuses or set(document_statuses) == {DevelopmentProduct.DocumentStatus.DRAFT}:
            status = Collection.Status.DRAFT
        elif all(value == DevelopmentProduct.DocumentStatus.APPROVED for value in document_statuses):
            status = Collection.Status.READY_FOR_DEVELOPMENT
        else:
            status = Collection.Status.DOCUMENT_APPROVAL

    if collection.status != status:
        previous_status = collection.status
        collection.status = status
        collection.save(update_fields=("status", "updated_at"))
        if actor:
            record_audit(
                actor=actor,
                action="rnd_collection_status_changed",
                entity_type="rnd.collection",
                entity_id=collection.id,
                before_values={"status": previous_status},
                after_values={"status": status},
            )
    return collection


@transaction.atomic
def submit_product_document(*, product, actor):
    product = DevelopmentProduct.objects.select_for_update().select_related("collection").get(pk=product.pk)
    if (
        product.collection.development_started_at
        or product.collection.handed_over_at
        or product.collection.commercial_approved_at
    ):
        raise ValidationError("Product sudah dikunci setelah Development dimulai atau handover ke Marketing.")
    if product.document_status not in {
        DevelopmentProduct.DocumentStatus.DRAFT,
        DevelopmentProduct.DocumentStatus.REJECTED,
    }:
        raise ValidationError("Dokumen Product ini sudah diajukan.")

    previous_document_name = product.submitted_document.name
    previous_document_storage = product.submitted_document.storage
    submitted_at = timezone.now()
    document = build_combined_document(product=product, submitted_at=submitted_at)
    document_stem = slugify(f"{product.collection.code}-{product.name}") or str(product.id)
    filename = f"{document_stem}-rev-{product.document_revision:03d}-submitted.pdf"
    product.submitted_document.save(filename, ContentFile(document), save=False)
    product.document_status = DevelopmentProduct.DocumentStatus.SUBMITTED
    product.submitted_at = submitted_at
    product.submitted_by = actor
    product.approved_document = ""
    product.rnd_approved_at = None
    product.rnd_approved_by = None
    product.save(
        update_fields=(
            "submitted_document",
            "document_status",
            "submitted_at",
            "submitted_by",
            "approved_document",
            "rnd_approved_at",
            "rnd_approved_by",
            "updated_at",
        )
    )
    revision, _ = DevelopmentProductDocumentRevision.objects.select_for_update().get_or_create(
        product=product,
        revision=product.document_revision,
        defaults={
            "status": DevelopmentProductDocumentRevision.Status.SUBMITTED,
            "submitted_document": product.submitted_document.name,
            "submitted_at": submitted_at,
            "submitted_by": actor,
        },
    )
    revision.status = DevelopmentProductDocumentRevision.Status.SUBMITTED
    revision.submitted_document.name = product.submitted_document.name
    revision.submitted_at = submitted_at
    revision.submitted_by = actor
    revision.approved_document = ""
    revision.approved_at = None
    revision.approved_by = None
    revision.revision_requested_at = None
    revision.revision_note = ""
    revision.revision_target = ""
    revision.revision_requested_by = None
    revision.save(
        update_fields=(
            "status",
            "submitted_document",
            "submitted_at",
            "submitted_by",
            "approved_document",
            "approved_at",
            "approved_by",
            "revision_requested_at",
            "revision_note",
            "revision_target",
            "revision_requested_by",
            "updated_at",
        )
    )
    if previous_document_name and previous_document_name != product.submitted_document.name:
        transaction.on_commit(
            lambda: previous_document_storage.delete(previous_document_name),
            robust=True,
        )
    sync_collection_status(collection=product.collection, actor=actor)
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
    product.rnd_approved_at = approved_at
    product.rnd_approved_by = actor
    product.save(
        update_fields=(
            "approved_document",
            "document_status",
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
    sync_collection_status(collection=product.collection, actor=actor)
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
def reject_product_document(*, product, actor):
    if not actor.is_superuser:
        raise PermissionDenied("Reject dokumen R&D hanya dapat dilakukan Super Admin.")
    product = DevelopmentProduct.objects.select_for_update().select_related("collection").get(pk=product.pk)
    if product.document_status != DevelopmentProduct.DocumentStatus.SUBMITTED:
        raise ValidationError("Dokumen Product belum diajukan atau statusnya sudah berubah.")

    revision = DevelopmentProductDocumentRevision.objects.select_for_update().get(
        product=product,
        revision=product.document_revision,
    )
    revision.status = DevelopmentProductDocumentRevision.Status.REJECTED
    revision.save(update_fields=("status", "updated_at"))
    product.document_status = DevelopmentProduct.DocumentStatus.REJECTED
    product.save(update_fields=("document_status", "updated_at"))
    sync_collection_status(collection=product.collection, actor=actor)
    record_audit(
        actor=actor,
        action="rnd_product_document_rejected",
        entity_type="rnd.development_product",
        entity_id=product.id,
        before_values={"document_status": DevelopmentProduct.DocumentStatus.SUBMITTED},
        after_values={
            "document_status": product.document_status,
            "revision": f"{product.document_revision:03d}",
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
    if product.collection.development_started_at:
        raise ValidationError("Revisi dokumen ditutup setelah Collection masuk ke Development.")
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
    sync_collection_status(collection=product.collection, actor=actor)
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
def start_collection_development(*, collection, actor):
    if not can_edit_module(actor, "rnd"):
        raise PermissionDenied("Lanjut Development memerlukan akses Edit atau Approve R&D.")
    collection = Collection.objects.select_for_update().get(pk=collection.pk)
    if collection.development_started_at:
        return collection
    if collection.handed_over_at or collection.commercial_approved_at:
        raise ValidationError("Collection sudah dikunci setelah handover ke Marketing.")
    products = list(collection.products.select_for_update())
    if not products:
        raise ValidationError("Collection wajib memiliki minimal satu Product.")
    if any(
        product.document_status != DevelopmentProduct.DocumentStatus.APPROVED
        for product in products
    ):
        raise ValidationError("Seluruh dokumen Product wajib Approved sebelum lanjut Development.")

    started_at = timezone.now()
    collection.development_started_at = started_at
    collection.development_started_by = actor
    collection.status = Collection.Status.DEVELOPMENT
    collection.save(
        update_fields=(
            "development_started_at",
            "development_started_by",
            "status",
            "updated_at",
        )
    )
    for product in products:
        if product.status == DevelopmentProduct.Status.FINAL_APPROVED:
            product.development_stage = DevelopmentProduct.DevelopmentStage.FINAL
            product.prototype_number = max(product.prototype_number, 1)
        else:
            product.development_stage = DevelopmentProduct.DevelopmentStage.MATERIAL_PURCHASE
            product.prototype_number = 0
        product.updated_at = started_at
    DevelopmentProduct.objects.bulk_update(
        products,
        ("development_stage", "prototype_number", "updated_at"),
    )
    collection = sync_collection_status(collection=collection, actor=actor)
    record_audit(
        actor=actor,
        action="rnd_collection_development_started",
        entity_type="rnd.collection",
        entity_id=collection.id,
        after_values={
            "development_started_at": started_at.isoformat(),
            "product_count": len(products),
        },
    )
    return collection


@transaction.atomic
def transition_product_development(*, product, actor, action):
    product = DevelopmentProduct.objects.select_for_update().select_related("collection").get(pk=product.pk)
    if not product.collection.development_started_at:
        raise ValidationError("Collection belum masuk ke tab Development.")
    if product.collection.status != Collection.Status.DEVELOPMENT:
        raise ValidationError("Collection sudah dikunci setelah handover ke Marketing.")
    if product.document_status != DevelopmentProduct.DocumentStatus.APPROVED:
        raise ValidationError("Dokumen Product wajib tetap Approved selama Development.")

    approval_actions = {"prototype_final", "prototype_resampling"}
    if action in approval_actions:
        if not can_approve_module(actor, "rnd"):
            raise PermissionDenied("Keputusan Prototype memerlukan akses Approve R&D.")
    elif not can_edit_module(actor, "rnd"):
        raise PermissionDenied("Update Development memerlukan akses Edit atau Approve R&D.")

    transitions = {
        "material_completed": (
            DevelopmentProduct.DevelopmentStage.MATERIAL_PURCHASE,
            DevelopmentProduct.DevelopmentStage.SAMPLING,
        ),
        "sampling_completed": (
            DevelopmentProduct.DevelopmentStage.SAMPLING,
            DevelopmentProduct.DevelopmentStage.PROTOTYPE,
        ),
        "prototype_resampling": (
            DevelopmentProduct.DevelopmentStage.PROTOTYPE,
            DevelopmentProduct.DevelopmentStage.RESAMPLING,
        ),
        "resampling_completed": (
            DevelopmentProduct.DevelopmentStage.RESAMPLING,
            DevelopmentProduct.DevelopmentStage.PROTOTYPE,
        ),
        "prototype_final": (
            DevelopmentProduct.DevelopmentStage.PROTOTYPE,
            DevelopmentProduct.DevelopmentStage.FINAL,
        ),
    }
    transition = transitions.get(action)
    if not transition:
        raise ValidationError("Aksi Development tidak dikenali.")
    expected_stage, next_stage = transition
    if product.development_stage != expected_stage:
        raise ValidationError("Tahap Product sudah berubah. Muat ulang halaman sebelum melanjutkan.")

    before_stage = product.development_stage
    if action in {"sampling_completed", "resampling_completed"}:
        product.prototype_number += 1
    product.development_stage = next_stage
    update_fields = ["development_stage", "prototype_number", "updated_at"]
    if action == "material_completed":
        product.status = DevelopmentProduct.Status.SAMPLING
        update_fields.append("status")
    elif action == "prototype_resampling":
        product.status = DevelopmentProduct.Status.REVISION
        update_fields.append("status")
    elif action == "resampling_completed":
        product.status = DevelopmentProduct.Status.SAMPLING
        update_fields.append("status")
    elif action == "prototype_final":
        product.status = DevelopmentProduct.Status.FINAL_APPROVED
        update_fields.append("status")
    product.save(update_fields=update_fields)
    sync_collection_status(collection=product.collection, actor=actor)
    record_audit(
        actor=actor,
        action=f"rnd_product_development_{action}",
        entity_type="rnd.development_product",
        entity_id=product.id,
        before_values={"development_stage": before_stage},
        after_values={
            "development_stage": product.development_stage,
            "prototype_number": product.prototype_number,
        },
    )
    return product


@transaction.atomic
def delete_rejected_product(*, product, actor):
    if not can_edit_module(actor, "rnd"):
        raise PermissionDenied("Delete Product memerlukan akses Edit atau Approve R&D.")
    product = (
        DevelopmentProduct.objects.select_for_update()
        .select_related("collection")
        .prefetch_related("document_revisions")
        .get(pk=product.pk)
    )
    if product.document_status != DevelopmentProduct.DocumentStatus.REJECTED:
        raise ValidationError("Product hanya dapat dihapus setelah dokumennya di-reject.")
    if (
        product.collection.development_started_at
        or product.collection.handed_over_at
        or product.collection.commercial_approved_at
    ):
        raise ValidationError("Product sudah dikunci setelah Development dimulai atau handover ke Marketing.")
    if MarketingRecommendation.objects.filter(product=product).exists():
        raise ValidationError("Product yang sudah memiliki rekomendasi Marketing tidak dapat dihapus.")

    files = {}
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

    collection = product.collection
    product_id = product.id
    snapshot = {
        "collection_id": str(collection.id),
        "working_code": product.working_code,
        "name": product.name,
        "document_status": product.document_status,
        "revision": f"{product.document_revision:03d}",
    }
    product.delete()
    sync_collection_status(collection=collection, actor=actor)
    record_audit(
        actor=actor,
        action="rnd_rejected_product_deleted",
        entity_type="rnd.development_product",
        entity_id=product_id,
        before_values=snapshot,
        after_values={"deleted": True},
    )
    for storage, name in files.values():
        transaction.on_commit(lambda storage=storage, name=name: storage.delete(name), robust=True)
    return snapshot


@transaction.atomic
def delete_collection(*, collection, actor):
    if not can_approve_module(actor, "rnd"):
        raise PermissionDenied("Delete Collection memerlukan akses Approve R&D.")
    collection = Collection.objects.select_for_update().get(pk=collection.pk)
    if collection.handed_over_at or collection.commercial_approved_at:
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
    if collection.handed_over_at or collection.commercial_approved_at:
        raise ValidationError("Collection ini tidak dapat di-handover ulang.")
    if not products.exists():
        raise ValidationError("Collection wajib memiliki minimal satu Product.")
    if not collection.development_started_at:
        raise ValidationError("Collection wajib menyelesaikan alur Development sebelum handover ke Marketing.")
    if products.exclude(
        status=DevelopmentProduct.Status.FINAL_APPROVED,
        document_status=DevelopmentProduct.DocumentStatus.APPROVED,
        development_stage=DevelopmentProduct.DevelopmentStage.FINAL,
    ).exists():
        raise ValidationError("Seluruh Product wajib Final Development sebelum handover ke Marketing.")
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
def publish_marketing_preview(*, collection, actor):
    if not can_approve_module(actor, "rnd"):
        raise PermissionDenied("Tampilkan ke Marketing memerlukan akses Approve R&D.")
    collection = Collection.objects.select_for_update().get(pk=collection.pk)
    if collection.marketing_previewed_at:
        return collection
    approved_products = collection.products.filter(
        document_status=DevelopmentProduct.DocumentStatus.APPROVED
    )
    if not approved_products.exists():
        raise ValidationError("Minimal satu dokumen Product wajib Approved sebelum ditampilkan ke Marketing.")
    collection.marketing_previewed_at = timezone.now()
    collection.marketing_previewed_by = actor
    collection.save(
        update_fields=("marketing_previewed_at", "marketing_previewed_by", "updated_at")
    )
    record_audit(
        actor=actor,
        action="rnd_collection_marketing_preview_published",
        entity_type="rnd.collection",
        entity_id=collection.id,
        after_values={
            "approved_product_count": approved_products.count(),
            "marketing_previewed_at": collection.marketing_previewed_at.isoformat(),
        },
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
