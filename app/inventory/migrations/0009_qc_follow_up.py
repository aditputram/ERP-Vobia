import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_follow_ups(apps, schema_editor):
    QCInspection = apps.get_model("inventory", "QCInspection")
    QCFollowUp = apps.get_model("inventory", "QCFollowUp")
    QCFollowUpEvent = apps.get_model("inventory", "QCFollowUpEvent")
    status_map = {
        "REWORK": "AWAITING_REWORK",
        "REJECTED": "REJECTED",
        "ACCEPTED_WITH_EXCEPTION": "ACCEPTED_EXCEPTION",
    }
    for inspection in QCInspection.objects.filter(qty_failed__gt=0).exclude(failed_disposition=""):
        follow_up, created = QCFollowUp.objects.get_or_create(
            source_inspection=inspection,
            defaults={
                "po_line_id": inspection.po_line_id,
                "status": status_map[inspection.failed_disposition],
                "original_failed_qty": inspection.qty_failed,
                "open_qty": inspection.qty_failed,
            },
        )
        if created:
            QCFollowUpEvent.objects.create(
                follow_up=follow_up,
                event_type="CREATED",
                activity_date=inspection.inspected_at.date(),
                qty_inspected=inspection.qty_inspected,
                qty_passed=inspection.qty_passed,
                qty_failed=inspection.qty_failed,
                failed_disposition=inspection.failed_disposition,
                notes=inspection.notes,
                actor_id=inspection.recorded_by_id,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0008_remove_qc_waiting_disposition"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="QCFollowUp",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("AWAITING_REWORK", "Menunggu Rework"), ("READY_RE_QC", "Menunggu Re-QC"), ("REJECTED", "Rejected"), ("ACCEPTED_EXCEPTION", "Accepted with Exception"), ("RESOLVED", "Lolos Re-QC")], max_length=30)),
                ("original_failed_qty", models.DecimalField(decimal_places=4, max_digits=18)),
                ("open_qty", models.DecimalField(decimal_places=4, max_digits=18)),
                ("resolved_passed_qty", models.DecimalField(decimal_places=4, default=0, max_digits=18)),
                ("rework_cycle", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("po_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="qc_follow_ups", to="purchasing.purchaseorderline")),
                ("source_inspection", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="follow_up", to="inventory.qcinspection")),
            ],
            options={"ordering": ("-updated_at",)},
        ),
        migrations.CreateModel(
            name="QCFollowUpEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("event_type", models.CharField(choices=[("CREATED", "QC Follow-up Dibuat"), ("REWORK_COMPLETED", "Rework Selesai"), ("RE_QC", "Re-QC")], max_length=30)),
                ("activity_date", models.DateField()),
                ("qty_inspected", models.DecimalField(decimal_places=4, default=0, max_digits=18)),
                ("qty_passed", models.DecimalField(decimal_places=4, default=0, max_digits=18)),
                ("qty_failed", models.DecimalField(decimal_places=4, default=0, max_digits=18)),
                ("failed_disposition", models.CharField(blank=True, max_length=30)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ("follow_up", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="events", to="inventory.qcfollowup")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddConstraint(model_name="qcfollowup", constraint=models.CheckConstraint(condition=models.Q(("original_failed_qty__gt", 0)), name="inventory_qc_followup_original_positive")),
        migrations.AddConstraint(model_name="qcfollowup", constraint=models.CheckConstraint(condition=models.Q(("open_qty__gte", 0)), name="inventory_qc_followup_open_nonnegative")),
        migrations.AddConstraint(model_name="qcfollowup", constraint=models.CheckConstraint(condition=models.Q(("resolved_passed_qty__gte", 0)), name="inventory_qc_followup_passed_nonnegative")),
        migrations.AddConstraint(model_name="qcfollowupevent", constraint=models.CheckConstraint(condition=models.Q(("qty_inspected__gte", 0)), name="inventory_qc_followup_event_inspected_nonnegative")),
        migrations.AddConstraint(model_name="qcfollowupevent", constraint=models.CheckConstraint(condition=models.Q(("qty_passed__gte", 0)), name="inventory_qc_followup_event_passed_nonnegative")),
        migrations.AddConstraint(model_name="qcfollowupevent", constraint=models.CheckConstraint(condition=models.Q(("qty_failed__gte", 0)), name="inventory_qc_followup_event_failed_nonnegative")),
        migrations.RunPython(backfill_follow_ups, migrations.RunPython.noop),
    ]
