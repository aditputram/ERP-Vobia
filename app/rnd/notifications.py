from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse

from accounts.access import module_level
from chat.push import send_web_push

from .models import Collection, DesignAsset, DevelopmentProduct, RndNotification


ACTION_COPY = {
    "rnd_design_uploaded": ("Desain baru", "meng-upload desain"),
    "rnd_design_commented": ("Komentar desain baru", "memberi komentar pada desain"),
    "rnd_design_recommended": ("Desain direkomendasikan", "merekomendasikan desain"),
    "rnd_collection_created": ("Collection baru", "membuat collection"),
    "rnd_product_created": ("Produk baru", "menambahkan produk"),
    "rnd_product_duplicated": ("Produk diduplikasi", "menduplikasi produk"),
    "rnd_product_document_submitted": ("Dokumen menunggu approval", "mengajukan dokumen"),
    "rnd_product_document_approved": ("Dokumen disetujui", "menyetujui dokumen"),
    "rnd_product_document_rejected": ("Dokumen ditolak", "menolak dokumen"),
    "rnd_product_document_revision_requested": ("Revisi diminta", "meminta revisi dokumen"),
    "rnd_collection_status_changed": ("Collection siap Development", "menyelesaikan approval collection"),
    "rnd_collection_development_started": ("Development dimulai", "memulai development collection"),
    "rnd_product_development_material_completed": ("Pembelian material selesai", "menyelesaikan pembelian material"),
    "rnd_product_development_sampling_completed": ("Sampling selesai", "menyelesaikan sampling"),
    "rnd_product_development_prototype_resampling": ("Prototype perlu resampling", "memulai resampling"),
    "rnd_product_development_resampling_completed": ("Resampling selesai", "menyelesaikan resampling"),
    "rnd_product_development_prototype_final": ("Final Development", "menyetujui final development"),
    "rnd_collection_marketing_preview_published": ("Upcoming collection dibuka", "membuka collection untuk Marketing"),
    "rnd_collection_handed_to_marketing": ("Handover ke Marketing", "menyerahkan collection ke Marketing"),
    "rnd_collection_commercially_approved": ("Collection disetujui komersial", "menyetujui collection secara komersial"),
}

COMMENT_ACTIONS = {"rnd_design_commented"}
APPROVAL_ACTIONS = {
    "rnd_product_document_submitted",
    "rnd_product_document_approved",
    "rnd_product_document_rejected",
    "rnd_product_document_revision_requested",
    "rnd_collection_status_changed",
    "rnd_product_development_prototype_resampling",
    "rnd_product_development_prototype_final",
    "rnd_collection_commercially_approved",
}
CATEGORY_BY_TITLE = {
    title: "comment" if action in COMMENT_ACTIONS else "approval" if action in APPROVAL_ACTIONS else "new"
    for action, (title, _verb) in ACTION_COPY.items()
}


def notification_category(notification):
    return CATEGORY_BY_TITLE.get(notification.title, "new")


def _actor_name(actor):
    return (actor.get_full_name() or actor.username).strip()


def _target_and_label(event):
    after = event.after_values or {}
    if event.action in {"rnd_design_uploaded", "rnd_design_recommended"}:
        label = after.get("original_name") or DesignAsset.objects.filter(pk=event.entity_id).values_list(
            "original_name", flat=True
        ).first()
        return reverse("rnd:design_detail", args=[event.entity_id]), label or "desain"
    if event.action == "rnd_design_commented":
        design_id = after.get("design_id")
        label = DesignAsset.objects.filter(pk=design_id).values_list("original_name", flat=True).first()
        return reverse("rnd:design_detail", args=[design_id]), label or "desain"
    if event.action.startswith("rnd_product_"):
        label = after.get("product_name") or DevelopmentProduct.objects.filter(pk=event.entity_id).values_list(
            "name", flat=True
        ).first()
        route = (
            "rnd:development_product_detail"
            if event.action.startswith("rnd_product_development_")
            else "rnd:product_detail"
        )
        return reverse(route, args=[event.entity_id]), label or "produk"
    label = after.get("name") or Collection.objects.filter(pk=event.entity_id).values_list(
        "name", flat=True
    ).first()
    route = (
        "rnd:development_detail"
        if event.action == "rnd_collection_development_started"
        else "rnd:collection_detail"
    )
    return reverse(route, args=[event.entity_id]), label or "collection"


def create_notifications_for_audit(event):
    copy = ACTION_COPY.get(event.action)
    if not copy or not event.actor_id:
        return 0
    if event.action == "rnd_collection_status_changed" and (event.after_values or {}).get("status") != "READY_FOR_DEVELOPMENT":
        return 0

    target_url, label = _target_and_label(event)
    title, verb = copy
    actor_name = _actor_name(event.actor)
    users = get_user_model().objects.filter(is_active=True).exclude(pk=event.actor_id)
    recipients = [user for user in users if user.is_superuser or module_level(user, "rnd") != "none"]
    notifications = [
        RndNotification(
            recipient=user,
            actor=event.actor,
            source_key=f"audit:{event.id}",
            title=title,
            message=f"{actor_name} {verb}: {label}.",
            target_url=target_url,
        )
        for user in recipients
    ]
    created = RndNotification.objects.bulk_create(notifications, ignore_conflicts=True)
    if created:
        recipient_ids = [notification.recipient_id for notification in created]
        category = "Approval" if event.action in APPROVAL_ACTIONS else "Notifikasi"
        transaction.on_commit(
            lambda recipients=recipient_ids, source=event.id, push_category=category: send_web_push(
                recipients,
                title=f"{push_category} R&D baru",
                body="Buka Space untuk melihat aktivitas R&D.",
                url=reverse("rnd:dashboard"),
                tag=f"rnd:{source}",
            )
        )
    return len(created)
