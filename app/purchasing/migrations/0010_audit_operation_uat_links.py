import uuid

from django.db import migrations
from django.db.models import Count, Sum


TARGET_SCENARIO_ID = uuid.UUID("9caf7f15-9b04-45b4-917b-0dc3fc4d380a")
ARCHIVE_KEY = "operation-uat-po-vob-08-26-001-004"


def audit_operation_uat_links(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    database = schema_editor.connection.alias
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    linked_pos = list(
        PurchaseOrderLine.objects.using(database)
        .filter(requirement__incoming_plan__scenario_id=TARGET_SCENARIO_ID)
        .values(
            "po_id",
            "po__po_number",
            "po__source",
            "po__status",
            "po__need_month",
        )
        .annotate(line_count=Count("id"), ordered_qty=Sum("ordered_qty"))
        .order_by("po__po_number")
    )
    for row in linked_pos:
        row["po_id"] = str(row["po_id"])
        row["po__need_month"] = str(row["po__need_month"])
        row["ordered_qty"] = str(row["ordered_qty"])

    AuditEvent.objects.using(database).create(
        actor=None,
        action="operation_uat_cleanup_link_audit",
        entity_type="merchandising.projectionscenario",
        entity_id=ARCHIVE_KEY,
        reason="Read-only preflight relasi PO untuk cleanup Operation UAT.",
        after_values={"linked_purchase_orders": linked_pos},
    )


def remove_link_audit(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    apps.get_model("audit", "AuditEvent").objects.using(schema_editor.connection.alias).filter(
        action="operation_uat_cleanup_link_audit",
        entity_id=ARCHIVE_KEY,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0009_cleanup_operation_uat"),
        ("audit", "0001_initial"),
    ]

    operations = [migrations.RunPython(audit_operation_uat_links, remove_link_audit)]
