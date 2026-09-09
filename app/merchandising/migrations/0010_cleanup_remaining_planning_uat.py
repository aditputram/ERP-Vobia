import hashlib
import importlib
import uuid
from datetime import date

from django.db import migrations, transaction


helpers = importlib.import_module("purchasing.migrations.0009_cleanup_operation_uat")

TARGET_SCENARIOS = {
    uuid.UUID("473f26c6-2148-4bba-b800-08f937441d63"): ("T-Shirt", "REVISION_DRAFT"),
    uuid.UUID("f710c7ac-2750-48c1-94a4-391d7c992622"): ("Sep - Des 2026 Reguler Shirt", "APPROVED"),
}
EXPECTED_COUNTS = {
    "rules": 160,
    "projections": 488,
    "plans": 488,
    "requirements": 106,
    "revisions": 106,
}
AUDIT_KEY = "all-planning-builder-scenarios-2026-09-09"


def _planning_fingerprint(models, database):
    parts = []
    for model in models:
        fields = [field.attname for field in model._meta.concrete_fields]
        rows = model.objects.using(database).order_by(model._meta.pk.name).values_list(*fields)
        parts.extend(
            f"{model._meta.label_lower}|" + "|".join("" if value is None else str(value) for value in row)
            for row in rows
        )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _cleanup_remaining_planning_uat(apps, schema_editor):
    database = schema_editor.connection.alias
    ProjectionScenario = apps.get_model("merchandising", "ProjectionScenario")
    ProjectionRule = apps.get_model("merchandising", "ProjectionRule")
    SalesProjection = apps.get_model("merchandising", "SalesProjection")
    IncomingPlan = apps.get_model("merchandising", "IncomingPlan")
    IncomingMonthClose = apps.get_model("merchandising", "IncomingMonthClose")
    PPICRequirement = apps.get_model("purchasing", "PPICRequirement")
    PPICRequirementRevision = apps.get_model("purchasing", "PPICRequirementRevision")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    scenario_ids = list(TARGET_SCENARIOS)
    scenarios = ProjectionScenario.objects.using(database).select_for_update().filter(pk__in=scenario_ids)
    if not scenarios.exists() and not ProjectionScenario.objects.using(database).exists():
        return
    identity = {row.id: (row.name, row.status) for row in scenarios}
    if identity != TARGET_SCENARIOS or ProjectionScenario.objects.using(database).count() != 2:
        raise RuntimeError("Cleanup Planning UAT dibatalkan: daftar atau status Scenario live berubah.")
    if ProjectionScenario.objects.using(database).exclude(pk__in=scenario_ids).filter(
        superseded_by_id__in=scenario_ids
    ).exists():
        raise RuntimeError("Cleanup Planning UAT dibatalkan: Scenario target dirujuk Scenario lain.")

    rules = ProjectionRule.objects.using(database).filter(scenario_id__in=scenario_ids)
    projections = SalesProjection.objects.using(database).filter(scenario_id__in=scenario_ids)
    plans = IncomingPlan.objects.using(database).filter(scenario_id__in=scenario_ids)
    requirements = PPICRequirement.objects.using(database).filter(incoming_plan__scenario_id__in=scenario_ids)
    requirement_ids = list(requirements.values_list("id", flat=True))
    revisions = PPICRequirementRevision.objects.using(database).filter(requirement_id__in=requirement_ids)
    counts = {
        "rules": rules.count(),
        "projections": projections.count(),
        "plans": plans.count(),
        "requirements": requirements.count(),
        "revisions": revisions.count(),
    }
    if counts != EXPECTED_COUNTS:
        raise RuntimeError(f"Cleanup Planning UAT dibatalkan: komposisi child berubah menjadi {counts}.")
    if PurchaseOrderLine.objects.using(database).filter(requirement_id__in=requirement_ids).exists():
        raise RuntimeError("Cleanup Planning UAT dibatalkan: Requirement target sudah terhubung ke PO.")
    if plans.exclude(sales_projection__scenario_id__in=scenario_ids).exists() or (
        IncomingPlan.objects.using(database).exclude(scenario_id__in=scenario_ids).filter(
            sales_projection__scenario_id__in=scenario_ids
        ).exists()
    ):
        raise RuntimeError("Cleanup Planning UAT dibatalkan: relasi Projection dan Incoming Plan silang Scenario.")
    if IncomingMonthClose.objects.using(database).filter(month__gte=date(2026, 9, 1)).exists():
        raise RuntimeError("Cleanup Planning UAT dibatalkan: September sudah di-month-close.")

    fingerprint_models = (
        ProjectionScenario,
        ProjectionRule,
        SalesProjection,
        IncomingPlan,
        PPICRequirement,
        PPICRequirementRevision,
    )
    event = AuditEvent.objects.using(database).create(
        actor=None,
        action="planning_uat_cleanup_committed",
        entity_type="merchandising.projectionscenario",
        entity_id=AUDIT_KEY,
        reason="Menghapus seluruh Scenario Planning Builder yang dinyatakan data UAT belum valid oleh Adit.",
        before_values={
            "records": {
                "merchandising.ProjectionScenario": helpers._snapshot(scenarios),
                "merchandising.ProjectionRule": helpers._snapshot(rules),
                "merchandising.SalesProjection": helpers._snapshot(projections),
                "merchandising.IncomingPlan": helpers._snapshot(plans),
                "purchasing.PPICRequirement": helpers._snapshot(requirements),
                "purchasing.PPICRequirementRevision": helpers._snapshot(revisions),
            }
        },
        after_values={**{key: 0 for key in EXPECTED_COUNTS}, "scenario_count": 0},
        metadata={
            "target_scenarios": {str(key): value[0] for key, value in TARGET_SCENARIOS.items()},
            "before_counts": counts,
            "before_fingerprint": _planning_fingerprint(fingerprint_models, database),
        },
    )

    revisions.delete()
    requirements.delete()
    plans.delete()
    projections.delete()
    rules.delete()
    scenarios.update(superseded_by_id=None)
    scenarios.delete()
    if ProjectionScenario.objects.using(database).exists():
        raise RuntimeError("Cleanup Planning UAT gagal menghapus seluruh Scenario.")
    AuditEvent.objects.using(database).filter(pk=event.pk).update(
        after_values={
            **event.after_values,
            "after_fingerprint": _planning_fingerprint(fingerprint_models, database),
        }
    )


def cleanup_remaining_planning_uat(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    database = schema_editor.connection.alias
    AuditEvent = apps.get_model("audit", "AuditEvent")
    try:
        with transaction.atomic(using=database):
            _cleanup_remaining_planning_uat(apps, schema_editor)
    except Exception as exc:
        AuditEvent.objects.using(database).create(
            actor=None,
            action="planning_uat_cleanup_blocked",
            entity_type="merchandising.projectionscenario",
            entity_id=AUDIT_KEY,
            reason=str(exc),
            metadata={"exception_type": type(exc).__name__},
        )


def restore_remaining_planning_uat(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    database = schema_editor.connection.alias
    ProjectionScenario = apps.get_model("merchandising", "ProjectionScenario")
    ProjectionRule = apps.get_model("merchandising", "ProjectionRule")
    SalesProjection = apps.get_model("merchandising", "SalesProjection")
    IncomingPlan = apps.get_model("merchandising", "IncomingPlan")
    PPICRequirement = apps.get_model("purchasing", "PPICRequirement")
    PPICRequirementRevision = apps.get_model("purchasing", "PPICRequirementRevision")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    event = AuditEvent.objects.using(database).filter(
        action="planning_uat_cleanup_committed", entity_id=AUDIT_KEY
    ).order_by("-occurred_at").first()
    if not event or ProjectionScenario.objects.using(database).filter(pk__in=TARGET_SCENARIOS).exists():
        return

    fingerprint_models = (
        ProjectionScenario,
        ProjectionRule,
        SalesProjection,
        IncomingPlan,
        PPICRequirement,
        PPICRequirementRevision,
    )
    expected = event.after_values.get("after_fingerprint")
    if not expected or _planning_fingerprint(fingerprint_models, database) != expected:
        raise RuntimeError("Rollback cleanup Planning UAT dibatalkan: data planning/requirement sudah berubah.")

    records = event.before_values.get("records", {})
    scenario_rows = records.get("merchandising.ProjectionScenario", [])
    deferred_superseded = {row["id"]: row.get("superseded_by_id") for row in scenario_rows}
    helpers._restore_rows(
        ProjectionScenario,
        [{**row, "superseded_by_id": None} for row in scenario_rows],
        database,
    )
    for scenario_id, superseded_by_id in deferred_superseded.items():
        if superseded_by_id:
            ProjectionScenario.objects.using(database).filter(pk=scenario_id).update(
                superseded_by_id=superseded_by_id
            )
    for label in (
        "merchandising.ProjectionRule",
        "merchandising.SalesProjection",
        "merchandising.IncomingPlan",
        "purchasing.PPICRequirement",
        "purchasing.PPICRequirementRevision",
    ):
        helpers._restore_rows(apps.get_model(*label.split(".")), records.get(label, []), database)

    AuditEvent.objects.using(database).create(
        actor=None,
        action="planning_uat_cleanup_rolled_back",
        entity_type="merchandising.projectionscenario",
        entity_id=AUDIT_KEY,
        reason="Rollback migration cleanup seluruh Scenario Planning Builder UAT.",
        after_values={"restored_scenario_count": len(TARGET_SCENARIOS), **EXPECTED_COUNTS},
    )


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ("merchandising", "0009_audit_remaining_planning_uat"),
        ("purchasing", "0011_cleanup_operation_uat_retry"),
        ("audit", "0001_initial"),
    ]

    operations = [migrations.RunPython(cleanup_remaining_planning_uat, restore_remaining_planning_uat)]
