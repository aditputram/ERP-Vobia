from django.db import migrations, models


def remove_unallocated_zero_requirements(apps, schema_editor):
    requirement_model = apps.get_model("purchasing", "PPICRequirement")
    revision_model = apps.get_model("purchasing", "PPICRequirementRevision")
    po_line_model = apps.get_model("purchasing", "PurchaseOrderLine")
    zero_requirements = requirement_model.objects.filter(approved_qty=0)
    if po_line_model.objects.filter(requirement__in=zero_requirements).exists():
        raise RuntimeError(
            "PPIC Requirement qty 0 masih memiliki histori PO line; "
            "selesaikan rekonsiliasi sebelum menerapkan constraint positive qty."
        )
    revision_model.objects.filter(requirement__in=zero_requirements).delete()
    zero_requirements.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("purchasing", "0005_powipimportbatch_stagedpowiprow_powipimportissue"),
    ]

    operations = [
        migrations.RunPython(remove_unallocated_zero_requirements, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="ppicrequirement",
            name="purchasing_requirement_qty_nonnegative",
        ),
        migrations.AddConstraint(
            model_name="ppicrequirement",
            constraint=models.CheckConstraint(
                condition=models.Q(("approved_qty__gt", 0)),
                name="purchasing_requirement_qty_positive",
            ),
        ),
    ]
