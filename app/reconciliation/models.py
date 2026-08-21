import uuid

from django.conf import settings
from django.db import models


class ReconciliationRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        PASSED = "PASSED", "Passed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    as_of_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    totals = models.JSONField(default=dict, blank=True)
    check_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-started_at",)


class ReconciliationIssue(models.Model):
    class Severity(models.TextChoices):
        CRITICAL = "CRITICAL", "Critical"
        WARNING = "WARNING", "Warning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(ReconciliationRun, on_delete=models.PROTECT, related_name="issues")
    severity = models.CharField(max_length=20, choices=Severity.choices)
    code = models.CharField(max_length=80)
    entity_type = models.CharField(max_length=100)
    entity_key = models.CharField(max_length=240)
    expected_value = models.CharField(max_length=200, blank=True)
    actual_value = models.CharField(max_length=200, blank=True)
    difference = models.CharField(max_length=200, blank=True)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("severity", "code", "entity_key")
