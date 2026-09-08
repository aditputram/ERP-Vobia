from django.db import migrations, models


def sync_existing_collection_statuses(apps, schema_editor):
    Collection = apps.get_model("rnd", "Collection")
    DevelopmentProduct = apps.get_model("rnd", "DevelopmentProduct")

    for collection in Collection.objects.all().iterator():
        products = DevelopmentProduct.objects.filter(collection_id=collection.id)
        if collection.commercial_approved_at:
            status = "COMMERCIAL_APPROVED"
        elif collection.handed_over_at:
            status = "MARKETING_REVIEW"
        elif collection.development_started_at:
            status = (
                "FINAL_DEVELOPMENT"
                if products.exists() and not products.exclude(development_stage="FINAL").exists()
                else "DEVELOPMENT"
            )
        else:
            document_statuses = list(products.values_list("document_status", flat=True))
            if not document_statuses or set(document_statuses) == {"DRAFT"}:
                status = "DRAFT"
            elif all(value == "APPROVED" for value in document_statuses):
                status = "READY_FOR_DEVELOPMENT"
            else:
                status = "DOCUMENT_APPROVAL"
        Collection.objects.filter(pk=collection.pk).update(status=status)


def restore_legacy_collection_statuses(apps, schema_editor):
    Collection = apps.get_model("rnd", "Collection")
    Collection.objects.filter(
        status__in=("DOCUMENT_APPROVAL", "READY_FOR_DEVELOPMENT")
    ).update(status="DRAFT")
    Collection.objects.filter(status="FINAL_DEVELOPMENT").update(status="DEVELOPMENT")


class Migration(migrations.Migration):
    dependencies = [("rnd", "0011_collection_development_workflow")]

    operations = [
        migrations.AlterField(
            model_name="collection",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"),
                    ("DOCUMENT_APPROVAL", "Approval Dokumen"),
                    ("READY_FOR_DEVELOPMENT", "Siap Development"),
                    ("DEVELOPMENT", "Development"),
                    ("FINAL_DEVELOPMENT", "Final Development"),
                    ("MARKETING_REVIEW", "Marketing Review"),
                    ("COMMERCIAL_APPROVED", "Commercial Approved"),
                ],
                default="DRAFT",
                max_length=30,
            ),
        ),
        migrations.RunPython(
            sync_existing_collection_statuses,
            reverse_code=restore_legacy_collection_statuses,
        ),
    ]
