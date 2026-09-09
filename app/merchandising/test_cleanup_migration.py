import importlib
from datetime import date
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase

from merchandising.models import ProjectionScenario


cleanup = importlib.import_module("merchandising.migrations.0010_cleanup_remaining_planning_uat")


class CleanupRemainingPlanningUATMigrationTests(TransactionTestCase):
    def test_cleanup_is_idempotent_when_targets_are_already_absent(self):
        cleanup._cleanup_remaining_planning_uat(apps, SimpleNamespace(connection=connection))
        self.assertFalse(ProjectionScenario.objects.exists())

    def test_cleanup_blocks_when_audited_child_counts_change(self):
        user = get_user_model().objects.create_user(username="planning-cleanup-test")
        for scenario_id, (name, status) in cleanup.TARGET_SCENARIOS.items():
            ProjectionScenario.objects.create(
                id=scenario_id,
                name=name,
                status=status,
                start_month=date(2026, 9, 1),
                end_month=date(2026, 12, 1),
                created_by=user,
            )

        with self.assertRaisesMessage(RuntimeError, "komposisi child berubah"):
            cleanup._cleanup_remaining_planning_uat(apps, SimpleNamespace(connection=connection))

        self.assertEqual(ProjectionScenario.objects.count(), 2)
