from .models import AuditEvent


def record_audit(
    *,
    actor,
    action,
    entity_type,
    entity_id="",
    reason="",
    before_values=None,
    after_values=None,
    metadata=None,
):
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else "",
        reason=reason,
        before_values=before_values or {},
        after_values=after_values or {},
        metadata=metadata or {},
    )

