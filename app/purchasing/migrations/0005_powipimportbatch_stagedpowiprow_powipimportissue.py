import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("imports", "0008_alter_rawfile_dataset_type"),
        ("master_data", "0003_marketplaceskumapping"),
        ("purchasing", "0004_purchaseorder_migration_cutoff_date_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="POWIPImportBatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("parser_version", models.CharField(default="po-wip-v1", max_length=40)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PARSING", "Parsing"),
                            ("READY", "Ready for approval"),
                            ("BLOCKED", "Blocked"),
                            ("COMMITTED", "Committed"),
                        ],
                        default="PARSING",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("previewed_at", models.DateTimeField(blank=True, null=True)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("committed_at", models.DateTimeField(blank=True, null=True)),
                ("total_rows", models.PositiveIntegerField(default=0)),
                ("ready_rows", models.PositiveIntegerField(default=0)),
                ("po_count", models.PositiveIntegerField(default=0)),
                ("total_outstanding_qty", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("blocking_issue_count", models.PositiveIntegerField(default=0)),
                ("warning_count", models.PositiveIntegerField(default=0)),
                ("quality_summary", models.JSONField(blank=True, default=dict)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approved_po_wip_imports",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "raw_file",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="po_wip_batches",
                        to="imports.rawfile",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="StagedPOWIPRow",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("row_number", models.PositiveIntegerField()),
                ("po_number", models.CharField(max_length=40)),
                ("parent_sku", models.CharField(blank=True, max_length=100)),
                ("sku_text", models.CharField(max_length=100)),
                ("product_name_source", models.CharField(blank=True, max_length=255)),
                ("outstanding_qty", models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True)),
                ("proposed_cogs_snapshot", models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True)),
                (
                    "proposed_action",
                    models.CharField(
                        choices=[("NEW", "New PO WIP line"), ("BLOCKED", "Blocked")],
                        default="NEW",
                        max_length=20,
                    ),
                ),
                ("original_data", models.JSONField(blank=True, default=dict)),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="staged_rows",
                        to="purchasing.powipimportbatch",
                    ),
                ),
                (
                    "sku",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="staged_po_wip_rows",
                        to="master_data.sku",
                    ),
                ),
            ],
            options={"ordering": ("row_number",)},
        ),
        migrations.CreateModel(
            name="POWIPImportIssue",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("severity", models.CharField(choices=[("ERROR", "Error"), ("WARNING", "Warning")], max_length=10)),
                ("code", models.CharField(max_length=80)),
                ("field_name", models.CharField(blank=True, max_length=80)),
                ("message", models.TextField()),
                ("is_blocking", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="issues",
                        to="purchasing.powipimportbatch",
                    ),
                ),
                (
                    "staged_row",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="issues",
                        to="purchasing.stagedpowiprow",
                    ),
                ),
            ],
            options={"ordering": ("-is_blocking", "staged_row__row_number", "code")},
        ),
        migrations.AddConstraint(
            model_name="stagedpowiprow",
            constraint=models.UniqueConstraint(
                fields=("batch", "row_number"),
                name="purchasing_unique_po_wip_row_number",
            ),
        ),
        migrations.AddIndex(
            model_name="stagedpowiprow",
            index=models.Index(fields=["batch", "po_number", "sku_text"], name="purch_wip_batch_po_sku_idx"),
        ),
        migrations.AddIndex(
            model_name="powipimportissue",
            index=models.Index(fields=["batch", "is_blocking"], name="purch_wip_issue_block_idx"),
        ),
        migrations.AddIndex(
            model_name="powipimportissue",
            index=models.Index(fields=["batch", "code"], name="purch_wip_issue_code_idx"),
        ),
    ]
