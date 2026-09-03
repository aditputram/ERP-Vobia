from io import BytesIO
from mimetypes import guess_type
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from django.views.decorators.clickjacking import xframe_options_sameorigin

from audit.services import record_audit

from .forms import (
    CollectionForm,
    DevelopmentProductForm,
    DevelopmentProductMaterialFormSet,
    MarketingRecommendationForm,
    OfficialDecisionForm,
)
from .documents import build_combined_document
from .models import (
    Collection,
    DevelopmentProduct,
    DevelopmentProductDocumentRevision,
    MarketingRecommendation,
)
from .services import (
    approve_collection_commercially,
    approve_product_document,
    can_approve_module,
    finalize_product,
    handover_to_marketing,
    move_product_to_costing,
    request_product_document_revision,
    submit_product_document,
)


def _validation_message(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


@login_required
def dashboard(request):
    request.session["active_module"] = "rnd"
    collections = Collection.objects.annotate(
        product_count=Count("products"),
        final_count=Count("products", filter=Q(products__status=DevelopmentProduct.Status.FINAL_APPROVED)),
    )
    return render(
        request,
        "rnd/dashboard.html",
        {
            "collections": collections,
            "collection_count": collections.count(),
            "development_count": collections.filter(
                status__in=(Collection.Status.DRAFT, Collection.Status.DEVELOPMENT)
            ).count(),
            "marketing_review_count": collections.filter(status=Collection.Status.MARKETING_REVIEW).count(),
        },
    )


@login_required
@transaction.atomic
def collection_create(request):
    form = CollectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        collection = form.save(commit=False)
        collection.created_by = request.user
        collection.save()
        record_audit(
            actor=request.user,
            action="rnd_collection_created",
            entity_type="rnd.collection",
            entity_id=collection.id,
            after_values={"code": collection.code, "name": collection.name, "status": collection.status},
        )
        messages.success(request, "Collection R&D berhasil dibuat.")
        return redirect("rnd:collection_detail", collection_id=collection.id)
    return render(request, "rnd/collection_form.html", {"form": form})


@login_required
@transaction.atomic
def collection_detail(request, collection_id):
    request.session["active_module"] = "rnd"
    collection = get_object_or_404(Collection.objects.select_related("handed_over_by"), id=collection_id)
    editable = collection.status in {Collection.Status.DRAFT, Collection.Status.DEVELOPMENT}
    product = DevelopmentProduct(collection=collection)
    product_form = DevelopmentProductForm(request.POST or None, request.FILES or None, instance=product)
    material_formset = DevelopmentProductMaterialFormSet(
        request.POST or None,
        instance=product,
        prefix="materials",
    )
    if request.method == "POST":
        if not editable:
            return HttpResponseForbidden("Collection sudah dikunci setelah handover ke Marketing.")
        if product_form.is_valid() and material_formset.is_valid():
            product = product_form.save(commit=False)
            product.collection = collection
            product.working_code = f"RND-{str(uuid4()).upper()}"
            product.save()
            material_formset.instance = product
            material_formset.save()
            if collection.status == Collection.Status.DRAFT:
                collection.status = Collection.Status.DEVELOPMENT
                collection.save(update_fields=("status", "updated_at"))
            record_audit(
                actor=request.user,
                action="rnd_product_created",
                entity_type="rnd.development_product",
                entity_id=product.id,
                after_values={"collection_id": str(collection.id), "product_name": product.name},
            )
            messages.success(request, "Product berhasil ditambahkan ke Collection.")
            return redirect("rnd:collection_detail", collection_id=collection.id)
    products = collection.products.select_related("rnd_approved_by").annotate(
        material_count=Count("materials")
    )
    return render(
        request,
        "rnd/collection_detail.html",
        {
            "collection": collection,
            "products": products,
            "product_form": product_form,
            "material_formset": material_formset,
            "editable": editable,
            "all_final": products.exists()
            and not products.exclude(status=DevelopmentProduct.Status.FINAL_APPROVED).exists(),
            "can_handover": can_approve_module(request.user, "rnd"),
        },
    )


@login_required
@transaction.atomic
def product_detail(request, product_id):
    request.session["active_module"] = "rnd"
    product = get_object_or_404(DevelopmentProduct.objects.select_related("collection"), id=product_id)
    editable = (
        product.collection.status in {Collection.Status.DRAFT, Collection.Status.DEVELOPMENT}
        and product.document_status
        in {
            DevelopmentProduct.DocumentStatus.DRAFT,
            DevelopmentProduct.DocumentStatus.REVISION_REQUESTED,
        }
    )
    revision_requested = product.document_status == DevelopmentProduct.DocumentStatus.REVISION_REQUESTED
    revision_request = (
        product.document_revisions.filter(revision=product.document_revision).first()
        if revision_requested
        else None
    )
    form = DevelopmentProductForm(request.POST or None, request.FILES or None, instance=product)
    material_formset = DevelopmentProductMaterialFormSet(
        request.POST or None,
        instance=product,
        prefix="materials",
    )
    if request.method == "POST":
        if not editable:
            return HttpResponseForbidden("Product sudah dikunci setelah handover ke Marketing.")
        if product.status == DevelopmentProduct.Status.FINAL_APPROVED and not can_approve_module(request.user, "rnd"):
            return HttpResponseForbidden("Product Final R&D hanya dapat diubah oleh approver.")
        form_is_valid = form.is_valid()
        if revision_requested and form_is_valid:
            if not revision_request or not revision_request.revision_target:
                form.add_error(None, "Target revisi belum dipilih oleh approver.")
                form_is_valid = False
            else:
                target = revision_request.revision_target
                if target in {
                    DevelopmentProductDocumentRevision.RevisionTarget.MOCKUP,
                    DevelopmentProductDocumentRevision.RevisionTarget.BOTH,
                } and "mockup" not in request.FILES:
                    form.add_error("mockup", "Upload MDR / Mockup baru sesuai permintaan revisi.")
                    form_is_valid = False
                if target in {
                    DevelopmentProductDocumentRevision.RevisionTarget.TECHNICAL_DRAWING,
                    DevelopmentProductDocumentRevision.RevisionTarget.BOTH,
                } and "technical_drawing" not in request.FILES:
                    form.add_error(
                        "technical_drawing",
                        "Upload Technical Drawing baru sesuai permintaan revisi.",
                    )
                    form_is_valid = False
        if form_is_valid and material_formset.is_valid():
            updated = form.save(commit=False)
            updated.rnd_approved_at = None
            updated.rnd_approved_by = None
            if revision_requested:
                updated.document_revision += 1
                updated.document_status = DevelopmentProduct.DocumentStatus.DRAFT
                updated.status = DevelopmentProduct.Status.REVISION
                updated.submitted_document = ""
                updated.approved_document = ""
                updated.submitted_at = None
                updated.submitted_by = None
            updated.save()
            material_formset.save()
            record_audit(
                actor=request.user,
                action="rnd_product_updated",
                entity_type="rnd.development_product",
                entity_id=updated.id,
                after_values={
                    "status": updated.status,
                    "product_name": updated.name,
                    "revision": f"{updated.document_revision:03d}",
                },
            )
            if revision_requested:
                messages.success(
                    request,
                    f"Draft revisi {updated.document_revision:03d} berhasil dibuat.",
                )
            else:
                messages.success(request, "Product R&D berhasil diperbarui.")
            return redirect("rnd:product_detail", product_id=updated.id)

    revision_records = list(
        product.document_revisions.select_related(
            "submitted_by",
            "approved_by",
            "revision_requested_by",
        )
    )
    revisions_by_number = {item.revision: item for item in revision_records}
    revision_numbers = sorted({product.document_revision, *revisions_by_number.keys()})
    try:
        requested_revision = int(request.GET.get("revision", product.document_revision))
    except (TypeError, ValueError):
        requested_revision = product.document_revision
    selected_revision = (
        requested_revision if requested_revision in revision_numbers else product.document_revision
    )
    selected_revision_record = revisions_by_number.get(selected_revision)
    if selected_revision_record:
        viewer_url = reverse(
            "rnd:product_revision_file",
            args=(product.id, selected_revision),
        )
        viewer_version = selected_revision_record.updated_at
    elif selected_revision == product.document_revision and product.mockup and product.technical_drawing:
        viewer_url = reverse("rnd:product_file", args=(product.id, "combined-preview"))
        viewer_version = product.updated_at
    else:
        viewer_url = ""
        viewer_version = None
    if viewer_url and viewer_version:
        viewer_url = f"{viewer_url}?v={int(viewer_version.timestamp())}"
    return render(
        request,
        "rnd/product_detail.html",
        {
            "product": product,
            "form": form,
            "material_formset": material_formset,
            "editable": editable,
            "show_edit": request.method == "POST" or request.GET.get("edit") == "1",
            "can_approve": can_approve_module(request.user, "rnd"),
            "can_officially_approve": request.user.is_superuser,
            "revision_requested": revision_requested,
            "revision_request": revision_request,
            "revision_options": [
                {
                    "number": number,
                    "label": f"{number:03d}",
                    "selected": number == selected_revision,
                }
                for number in revision_numbers
            ],
            "selected_revision": selected_revision,
            "selected_revision_record": selected_revision_record,
            "selected_is_current": selected_revision == product.document_revision,
            "viewer_url": viewer_url,
        },
    )


@login_required
@xframe_options_sameorigin
def product_file(request, product_id, file_kind):
    product = get_object_or_404(DevelopmentProduct.objects.select_related("collection"), id=product_id)
    if request.path.startswith("/marketing/") and product.collection.status not in {
        Collection.Status.MARKETING_REVIEW,
        Collection.Status.COMMERCIAL_APPROVED,
    }:
        raise Http404
    if file_kind == "combined-preview":
        try:
            document = build_combined_document(product=product)
        except ValidationError as exc:
            raise Http404 from exc
        response = FileResponse(
            BytesIO(document),
            as_attachment=False,
            filename=f"{slugify(product.collection.code)}-{slugify(product.name)}-preview.pdf",
            content_type="application/pdf",
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response
    field = {
        "product-cover": product.product_cover,
        "mockup": product.mockup,
        "technical-drawing": product.technical_drawing,
        "submitted-document": product.submitted_document,
        "approved-document": product.approved_document,
    }.get(file_kind)
    if not field:
        raise Http404
    response = FileResponse(
        field.open("rb"),
        as_attachment=False,
        filename=field.name.rsplit("/", 1)[-1],
        content_type=guess_type(field.name)[0] or "application/octet-stream",
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@xframe_options_sameorigin
def product_revision_file(request, product_id, revision):
    document_revision = get_object_or_404(
        DevelopmentProductDocumentRevision.objects.select_related("product"),
        product_id=product_id,
        revision=revision,
    )
    field = document_revision.approved_document or document_revision.submitted_document
    if not field:
        raise Http404
    response = FileResponse(
        field.open("rb"),
        as_attachment=False,
        filename=field.name.rsplit("/", 1)[-1],
        content_type="application/pdf",
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
def product_submit_approval(request, product_id):
    product = get_object_or_404(DevelopmentProduct, id=product_id)
    try:
        submit_product_document(product=product, actor=request.user)
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    else:
        messages.success(request, "MDR dan Technical Drawing berhasil digabung dan diajukan.")
    return redirect("rnd:product_detail", product_id=product.id)


@login_required
@require_POST
def product_approve(request, product_id):
    product = get_object_or_404(DevelopmentProduct, id=product_id)
    try:
        approve_product_document(product=product, actor=request.user)
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    except PermissionDenied:
        return HttpResponseForbidden("Approval dokumen R&D hanya dapat dilakukan Super Admin.")
    else:
        messages.success(request, "Dokumen Product sudah di-approve. Status Product masuk ke Sampling.")
    return redirect("rnd:product_detail", product_id=product.id)


@login_required
@require_POST
def product_request_revision(request, product_id):
    product = get_object_or_404(DevelopmentProduct, id=product_id)
    try:
        request_product_document_revision(
            product=product,
            actor=request.user,
            note=request.POST.get("revision_note"),
            target=request.POST.get("revision_target"),
        )
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    except PermissionDenied:
        return HttpResponseForbidden("Revisi dokumen R&D hanya dapat diminta oleh Super Admin.")
    else:
        messages.success(
            request,
            f"Revisi {product.document_revision:03d} diminta. Upload MDR dan Technical Drawing baru.",
        )
    return redirect("rnd:product_detail", product_id=product.id)


@login_required
@require_POST
def product_move_to_costing(request, product_id):
    product = get_object_or_404(DevelopmentProduct, id=product_id)
    try:
        move_product_to_costing(product=product, actor=request.user)
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    else:
        messages.success(request, "Status Product berhasil dilanjutkan ke Costing.")
    return redirect("rnd:product_detail", product_id=product.id)


@login_required
@require_POST
def product_finalize(request, product_id):
    product = get_object_or_404(DevelopmentProduct, id=product_id)
    try:
        finalize_product(product=product, actor=request.user)
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    except PermissionDenied:
        return HttpResponseForbidden("Final R&D memerlukan akses Approve R&D.")
    else:
        messages.success(request, "Product sudah ditetapkan Final R&D.")
    return redirect("rnd:product_detail", product_id=product.id)


@login_required
@require_POST
def collection_handover(request, collection_id):
    collection = get_object_or_404(Collection, id=collection_id)
    try:
        handover_to_marketing(collection=collection, actor=request.user)
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    except PermissionDenied:
        return HttpResponseForbidden("Handover Collection memerlukan akses Approve R&D.")
    else:
        messages.success(request, "Collection berhasil di-handover ke Marketing Upcoming Collection.")
    return redirect("rnd:collection_detail", collection_id=collection.id)


@login_required
def upcoming_collection_list(request):
    request.session["active_module"] = "marketing"
    collections = Collection.objects.filter(
        status__in=(Collection.Status.MARKETING_REVIEW, Collection.Status.COMMERCIAL_APPROVED)
    ).annotate(product_count=Count("products"))
    return render(request, "rnd/upcoming_list.html", {"collections": collections})


@login_required
def upcoming_collection_detail(request, collection_id):
    request.session["active_module"] = "marketing"
    collection = get_object_or_404(
        Collection.objects.filter(
            status__in=(Collection.Status.MARKETING_REVIEW, Collection.Status.COMMERCIAL_APPROVED)
        ).select_related("handed_over_by", "commercial_approved_by"),
        id=collection_id,
    )
    products = collection.products.select_related("marketing_recommendation").all()
    rows = []
    for product in products:
        try:
            recommendation = product.marketing_recommendation
        except MarketingRecommendation.DoesNotExist:
            recommendation = None
        rows.append({"product": product, "recommendation": recommendation})
    return render(
        request,
        "rnd/upcoming_detail.html",
        {"collection": collection, "rows": rows},
    )


@login_required
@transaction.atomic
def upcoming_collection_recommend(request, product_id):
    request.session["active_module"] = "marketing"
    product = get_object_or_404(DevelopmentProduct.objects.select_related("collection"), id=product_id)
    if product.collection.status != Collection.Status.MARKETING_REVIEW:
        return HttpResponseForbidden("Rekomendasi sudah dikunci setelah Commercial Approval.")
    try:
        recommendation = product.marketing_recommendation
    except MarketingRecommendation.DoesNotExist:
        recommendation = MarketingRecommendation(product=product)
    form = MarketingRecommendationForm(request.POST or None, instance=recommendation)
    if request.method == "POST" and form.is_valid():
        recommendation = form.save(commit=False)
        recommendation.product = product
        recommendation.recommended_by = request.user
        recommendation.recommended_at = timezone.now()
        recommendation.official_decision = ""
        recommendation.official_quantity = None
        recommendation.official_note = ""
        recommendation.approved_by = None
        recommendation.approved_at = None
        recommendation.save()
        record_audit(
            actor=request.user,
            action="rnd_marketing_recommendation_saved",
            entity_type="rnd.marketing_recommendation",
            entity_id=recommendation.id,
            after_values={
                "product_id": str(product.id),
                "recommendation": recommendation.recommendation,
                "recommended_quantity": recommendation.recommended_quantity,
            },
        )
        messages.success(request, "Rekomendasi Marketing berhasil disimpan.")
        return redirect("dashboard:upcoming_collection_detail", collection_id=product.collection_id)
    return render(request, "rnd/recommendation_form.html", {"product": product, "form": form})


@login_required
@transaction.atomic
def upcoming_collection_official_approve(request, product_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Official Decision hanya dapat dilakukan Super Admin.")
    request.session["active_module"] = "marketing"
    product = get_object_or_404(DevelopmentProduct.objects.select_related("collection"), id=product_id)
    if product.collection.status != Collection.Status.MARKETING_REVIEW:
        return HttpResponseForbidden("Official Decision sudah dikunci.")
    recommendation = get_object_or_404(MarketingRecommendation, product=product)
    form = OfficialDecisionForm(request.POST or None, instance=recommendation)
    if request.method == "POST" and form.is_valid():
        recommendation = form.save(commit=False)
        recommendation.approved_by = request.user
        recommendation.approved_at = timezone.now()
        recommendation.save()
        record_audit(
            actor=request.user,
            action="rnd_product_official_decision_saved",
            entity_type="rnd.marketing_recommendation",
            entity_id=recommendation.id,
            after_values={
                "official_decision": recommendation.official_decision,
                "official_quantity": recommendation.official_quantity,
            },
        )
        messages.success(request, "Official Decision Product berhasil disimpan.")
        return redirect("dashboard:upcoming_collection_detail", collection_id=product.collection_id)
    return render(request, "rnd/official_form.html", {"product": product, "recommendation": recommendation, "form": form})


@login_required
@require_POST
def upcoming_collection_commercial_approve(request, collection_id):
    collection = get_object_or_404(Collection, id=collection_id)
    try:
        approve_collection_commercially(collection=collection, actor=request.user)
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    except PermissionDenied:
        return HttpResponseForbidden("Commercial Approval hanya dapat dilakukan Super Admin.")
    else:
        messages.success(request, "Collection sudah mendapat Official Commercial Approval.")
    return redirect("dashboard:upcoming_collection_detail", collection_id=collection.id)
