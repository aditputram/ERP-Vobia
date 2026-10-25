from django.db import migrations
from django.db.models import Q


def _replace(value, old, new):
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, dict):
        return {key: _replace(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, old, new) for item in value]
    return value


def renumber_delivery_orders(apps, schema_editor):
    database = schema_editor.connection.alias
    DeliveryOrder = apps.get_model("production", "ProductionDeliveryOrder")
    ProductionActivity = apps.get_model("production", "ProductionActivity")
    InboundReceipt = apps.get_model("inventory", "InboundReceipt")
    InventoryMovement = apps.get_model("inventory", "InventoryMovement")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    orders = list(
        DeliveryOrder.objects.using(database).order_by("delivery_date", "created_at", "id")
    )
    annual_sequences = {}
    changes = []
    for order in orders:
        year = order.delivery_date.year
        annual_sequences[year] = annual_sequences.get(year, 0) + 1
        sequence = annual_sequences[year]
        issue_month = order.delivery_date.replace(day=1)
        number = f"DOP.VOB-{order.delivery_date:%m/%y}-{sequence:03d}"
        changes.append((order, order.number, number, issue_month, sequence))

    # Park every row on a unique temporary number and sequence first. Existing
    # monthly sequences can otherwise collide while they are being rewritten
    # into one annual sequence on PostgreSQL, even when the final values are
    # unique.
    max_existing_sequence = max((order.sequence or 0 for order in orders), default=0)
    for position, (order, _old, _new, _issue_month, _sequence) in enumerate(changes, start=1):
        DeliveryOrder.objects.using(database).filter(pk=order.pk).update(
            number=f"TMP-{str(order.pk).replace('-', '')[:26]}",
            sequence=max_existing_sequence + position,
        )

    for order, old, new, issue_month, sequence in changes:
        if old != new:
            for activity in ProductionActivity.objects.using(database).filter(delivery_order_id=order.pk):
                ProductionActivity.objects.using(database).filter(pk=activity.pk).update(
                    description=activity.description.replace(old, new),
                    before_values=_replace(activity.before_values, old, new),
                    after_values=_replace(activity.after_values, old, new),
                )
            for receipt in InboundReceipt.objects.using(database).filter(reference__startswith=f"{old}/"):
                InboundReceipt.objects.using(database).filter(pk=receipt.pk).update(
                    reference=receipt.reference.replace(old, new, 1)
                )
            movements = InventoryMovement.objects.using(database).filter(
                Q(movement_key__contains=old)
                | Q(source_reference=old)
                | Q(evidence_reference=old)
            )
            for movement in movements:
                InventoryMovement.objects.using(database).filter(pk=movement.pk).update(
                    movement_key=movement.movement_key.replace(old, new),
                    source_reference=movement.source_reference.replace(old, new),
                    evidence_reference=movement.evidence_reference.replace(old, new),
                )
            AuditEvent.objects.using(database).create(
                action="delivery_order_renumbered",
                entity_type="production.productiondeliveryorder",
                entity_id=str(order.pk),
                reason="Urutan nomor Delivery Order diselaraskan per tahun.",
                before_values={"number": old, "sequence": order.sequence},
                after_values={"number": new, "sequence": sequence},
            )
        DeliveryOrder.objects.using(database).filter(pk=order.pk).update(
            number=new,
            issue_month=issue_month,
            sequence=sequence,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        ("inventory", "0019_alter_qcfollowup_status_and_qcinspection_disposition"),
        ("production", "0009_rejected_goods_delivery_activity"),
    ]

    operations = [
        migrations.RunPython(renumber_delivery_orders, migrations.RunPython.noop),
    ]
