import uuid

from django.db import migrations
from django.db.models import Count, Sum


TARGET_SCENARIOS = {
    uuid.UUID("473f26c6-2148-4bba-b800-08f937441d63"): "T-Shirt",
    uuid.UUID("f710c7ac-2750-48c1-94a4-391d7c992622"): "Sep - Des 2026 Reguler Shirt",
}
AUDIT_KEY = "all-planning-builder-scenarios-2026-09-09"


def audit_remaining_planning_uat(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    database = schema_editor.connection.alias
    ProjectionScenario = apps.get_model("merchandising", "ProjectionScenario")
    ProjectionRule = apps.get_model("merchandising", "ProjectionRule")
    SalesProjection = apps.get_model("merchandising", "SalesProjection")
    IncomingPlan = apps.get_model("merchandising", "IncomingPlan")
    PPICRequirement = apps.get_model("purchasing", "PPICRequirement")
    PPICRequirementRevision = apps.get_model("purchasing", "PPICRequirementRevision")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    scenario_ids = list(TARGET_SCENARIOS)
    scenarios = list(
        ProjectionScenario.objects.using(database)
        .filter(pk__in=scenario_ids)
        .values("id", "name", "status", "start_month", "end_month")
        .order_by("name")
    )
    for row in scenarios:
        row["id"] = str(row["id"])
        row["start_month"] = str(row["start_month"])
        row["end_month"] = str(row["end_month"])

    requirements = PPICRequirement.objects.using(database).filter(
        incoming_plan__scenario_id__in=scenario_ids
    )
    requirement_ids = list(requirements.values_list("id", flat=True))
    linked_pos = list(
        PurchaseOrderLine.objects.using(database)
        .filter(requirement_id__in=requirement_ids)
        .values("po_id", "po__po_number", "po__source", "po__status", "po__need_month")
        .annotate(line_count=Count("id"), ordered_qty=Sum("ordered_qty"))
        .order_by("po__po_number")
    )
    for row in linked_pos:
        row["po_id"] = str(row["po_id"])
        row["po__need_month"] = str(row["po__need_month"])
        row["ordered_qty"] = str(row["ordered_qty"])

    AuditEvent.objects.using(database).create(
        actor=None,
        action="planning_uat_cleanup_audit",
        entity_type="merchandising.projectionscenario",
        entity_id=AUDIT_KEY,
        reason="Read-only preflight penghapusan seluruh Scenario Planning Builder UAT atas instruksi Adit.",
        after_values={
            "scenarios": scenarios,
            "total_scenario_count": ProjectionScenario.objects.using(database).count(),
            "target_scenario_count": len(scenarios),
            "rule_count": ProjectionRule.objects.using(database).filter(scenario_id__in=scenario_ids).count(),
            "projection_count": SalesProjection.objects.using(database).filter(scenario_id__in=scenario_ids).count(),
            "incoming_plan_count": IncomingPlan.objects.using(database).filter(scenario_id__in=scenario_ids).count(),
            "requirement_count": requirements.count(),
            "requirement_revision_count": PPICRequirementRevision.objects.using(database).filter(
                requirement_id__in=requirement_ids
            ).count(),
            "linked_purchase_orders": linked_pos,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("merchandising", "0008_alter_projectionscenario_status"),
        ("purchasing", "0011_cleanup_operation_uat_retry"),
        ("audit", "0001_initial"),
    ]

    operations = [migrations.RunPython(audit_remaining_planning_uat, migrations.RunPython.noop)]
