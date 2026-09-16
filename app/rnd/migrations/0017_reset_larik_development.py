from django.db import migrations
from django.utils import timezone


LARIK_COLLECTION_ID = "5a2199db-656f-4a34-adfb-7dd90c57ca63"


def reset_larik_development(apps, schema_editor):
    DevelopmentProduct = apps.get_model("rnd", "DevelopmentProduct")
    DevelopmentProduct.objects.filter(collection_id=LARIK_COLLECTION_ID).update(
        development_stage="MATERIAL_PURCHASE",
        prototype_number=0,
        updated_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [("rnd", "0016_developmentproductstagedate_notes_and_more")]

    operations = [
        migrations.RunPython(reset_larik_development, migrations.RunPython.noop),
    ]
