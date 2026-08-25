from datetime import date
import re

from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


PO_PATTERN = re.compile(r"^PO-VOB-(?P<month>\d{2})/(?P<year>\d{2})-(?P<sequence>\d{3})$")


def backfill_issue_month(apps, schema_editor):
    purchase_order_model = apps.get_model("purchasing", "PurchaseOrder")
    sequence_model = apps.get_model("purchasing", "PurchaseOrderNumberSequence")

    sequence_model.objects.all().delete()
    last_sequences = {}
    for po in purchase_order_model.objects.exclude(po_number__isnull=True).order_by("released_at", "created_at", "id"):
        match = PO_PATTERN.fullmatch(po.po_number or "")
        if po.source != "LEGACY_WIP" and po.released_at:
            released_date = timezone.localtime(po.released_at).date()
            issue_month = released_date.replace(day=1)
        elif match:
            issue_month = date(2000 + int(match.group("year")), int(match.group("month")), 1)
        else:
            continue
        po.issue_month = issue_month
        po.save(update_fields=["issue_month"])
        if po.source != "LEGACY_WIP" and po.sequence:
            last_sequences[issue_month] = max(last_sequences.get(issue_month, 0), po.sequence)

    for issue_month, last_sequence in last_sequences.items():
        sequence_model.objects.create(issue_month=issue_month, last_sequence=last_sequence)


class Migration(migrations.Migration):

    dependencies = [
        ("purchasing", "0006_ppic_requirement_positive_qty"),
    ]

    operations = [
        migrations.RenameField(
            model_name="purchaseordernumbersequence",
            old_name="need_month",
            new_name="issue_month",
        ),
        migrations.AddField(
            model_name="purchaseorder",
            name="issue_month",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RemoveConstraint(
            model_name="purchaseorder",
            name="purchasing_unique_month_sequence",
        ),
        migrations.RunPython(backfill_issue_month, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="purchaseorder",
            constraint=models.UniqueConstraint(
                condition=Q(sequence__isnull=False),
                fields=("issue_month", "sequence"),
                name="purchasing_unique_issue_month_sequence",
            ),
        ),
    ]
