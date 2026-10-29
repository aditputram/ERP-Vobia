from django.db import migrations, models


def backfill_prototype_document_revisions(apps, schema_editor):
    DevelopmentProduct = apps.get_model("rnd", "DevelopmentProduct")
    DevelopmentProductStageDate = apps.get_model("rnd", "DevelopmentProductStageDate")
    products = DevelopmentProduct.objects.filter(prototype_number__gte=1).only(
        "id",
        "prototype_number",
        "document_revision",
    )
    for product in products.iterator():
        for number in range(1, product.prototype_number + 1):
            DevelopmentProductStageDate.objects.update_or_create(
                product_id=product.id,
                stage_key=f"prototype_{number}",
                defaults={"document_revision": product.document_revision},
            )


def clear_prototype_document_revisions(apps, schema_editor):
    DevelopmentProductStageDate = apps.get_model("rnd", "DevelopmentProductStageDate")
    DevelopmentProductStageDate.objects.filter(stage_key__startswith="prototype_").update(
        document_revision=None
    )


class Migration(migrations.Migration):
    dependencies = [
        ("rnd", "0021_rndnotification_module"),
    ]

    operations = [
        migrations.AddField(
            model_name="developmentproductstagedate",
            name="document_revision",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_prototype_document_revisions,
            clear_prototype_document_revisions,
        ),
    ]
