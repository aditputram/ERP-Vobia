from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import AuditEvent
from .services import record_audit


class AuditEventTests(TestCase):
    def test_event_cannot_be_updated_or_deleted_through_model(self):
        actor = get_user_model().objects.create_user(
            username="auditor",
            password="NotUsed-But-Hashed-2026!",
        )
        event = record_audit(
            actor=actor,
            action="test_created",
            entity_type="tests.entity",
            entity_id="123",
        )
        event.reason = "changed"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

