import django.db.models.deletion
import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("merchandising", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MerchandisingSnapshotBatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_workbook_id", models.CharField(max_length=160)),
                ("source_file_name", models.CharField(max_length=255)),
                ("source_sha256", models.CharField(max_length=64, unique=True)),
                ("source_modified_at", models.DateTimeField(blank=True, null=True)),
                ("imported_at", models.DateTimeField(auto_now_add=True)),
                ("row_count", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("imported_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="imported_merchandising_snapshots", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-imported_at",)},
        ),
        migrations.CreateModel(
            name="MerchandisingMonthlySnapshot",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_row", models.PositiveIntegerField()),
                ("month", models.DateField()),
                ("status_snapshot", models.CharField(max_length=120)),
                ("product_snapshot", models.CharField(max_length=255)),
                ("variant_snapshot", models.CharField(blank=True, max_length=180)),
                ("category_snapshot", models.CharField(max_length=160)),
                ("subcategory_snapshot", models.CharField(blank=True, max_length=160)),
                ("size_snapshot", models.CharField(blank=True, max_length=100)),
                ("cogs_snapshot", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("retail_price_snapshot", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("prior_year_ending_qty", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("prior_year_ending_cogs", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("prior_year_ending_gross", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("incoming_qty", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("incoming_cogs", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("incoming_gross", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("beginning_qty", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("beginning_cogs", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("beginning_gross", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("sales_qty", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("sales_cogs", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("sales_gross", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("sales_net", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("ratio", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("ending_qty", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("ending_cogs", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("ending_gross", models.DecimalField(decimal_places=4, default=0, max_digits=22)),
                ("mos", models.DecimalField(blank=True, decimal_places=4, max_digits=22, null=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="monthly_rows", to="merchandising.merchandisingsnapshotbatch")),
                ("sku", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="merchandising_snapshots", to="master_data.sku")),
            ],
            options={"ordering": ("month", "source_row")},
        ),
        migrations.AddConstraint(
            model_name="merchandisingmonthlysnapshot",
            constraint=models.UniqueConstraint(fields=("batch", "sku", "month"), name="merch_unique_snapshot_batch_sku_month"),
        ),
        migrations.AddIndex(model_name="merchandisingmonthlysnapshot", index=models.Index(fields=["batch", "month"], name="merchandis_batch_i_75d13a_idx")),
        migrations.AddIndex(model_name="merchandisingmonthlysnapshot", index=models.Index(fields=["batch", "status_snapshot"], name="merchandis_batch_i_674304_idx")),
        migrations.AddIndex(model_name="merchandisingmonthlysnapshot", index=models.Index(fields=["batch", "category_snapshot"], name="merchandis_batch_i_9c836f_idx")),
        migrations.AddIndex(model_name="merchandisingmonthlysnapshot", index=models.Index(fields=["batch", "product_snapshot"], name="merchandis_batch_i_6d2217_idx")),
    ]
