import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("production", "0004_productiontrial_target_trial_date"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductionPlan",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("ACTIVE", "Active")], default="DRAFT", max_length=20)),
                ("target_material_purchase_date", models.DateField(blank=True, null=True)),
                ("target_trial_date", models.DateField(blank=True, null=True)),
                ("target_cut_start_date", models.DateField(blank=True, null=True)),
                ("target_cut_end_date", models.DateField(blank=True, null=True)),
                ("target_make_start_date", models.DateField(blank=True, null=True)),
                ("target_make_end_date", models.DateField(blank=True, null=True)),
                ("target_trim_start_date", models.DateField(blank=True, null=True)),
                ("target_trim_end_date", models.DateField(blank=True, null=True)),
                ("target_qc_start_date", models.DateField(blank=True, null=True)),
                ("target_qc_end_date", models.DateField(blank=True, null=True)),
                ("target_inbound_date", models.DateField(blank=True, null=True)),
                ("notes", models.TextField(blank=True)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("activated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="activated_production_plans", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_production_plans", to=settings.AUTH_USER_MODEL)),
                ("production_order", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="plan", to="production.productionorder")),
            ],
            options={"ordering": ("production_order__po__po_number",)},
        ),
        migrations.AddField(
            model_name="productionactivity",
            name="activity_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="productionactivity",
            name="activity_type",
            field=models.CharField(blank=True, choices=[("MATERIAL_PURCHASE", "Pembelian Material"), ("MATERIAL_ARRIVAL", "Material Tiba di Tempat Produksi"), ("TRIAL_SUBMIT", "Submit Trial Production"), ("TRIAL_APPROVE", "Approve Trial Production"), ("TRIAL_REVISION", "Revision Required Trial Production"), ("CUT", "Cut · Potong"), ("MAKE", "Make · Jahit"), ("TRIM", "Trim · Finishing"), ("QC", "Quality Control")], max_length=30),
        ),
        migrations.AddField(
            model_name="productionactivity",
            name="entry_kind",
            field=models.CharField(choices=[("SYSTEM", "System Event"), ("ACTIVITY", "Production Activity"), ("CORRECTION", "Correction"), ("VOID", "Void")], default="SYSTEM", max_length=20),
        ),
        migrations.AddField(
            model_name="productionactivity",
            name="notes",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="productionactivity",
            name="po_line",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="production_activities", to="purchasing.purchaseorderline"),
        ),
        migrations.AddField(
            model_name="productionactivity",
            name="quantity",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name="productionactivity",
            name="source_activity",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="correction_entries", to="production.productionactivity"),
        ),
        migrations.AddIndex(
            model_name="productionactivity",
            index=models.Index(fields=["production_order", "entry_kind", "activity_type"], name="production_entry_type_idx"),
        ),
    ]
