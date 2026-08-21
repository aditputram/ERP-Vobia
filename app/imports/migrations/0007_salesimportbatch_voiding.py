import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("imports", "0006_salesimportbatch_mode_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="salesimportbatch",
            name="status",
            field=models.CharField(
                choices=[
                    ("PARSING", "Parsing"),
                    ("READY", "Ready for approval"),
                    ("BLOCKED", "Blocked"),
                    ("COMMITTED", "Committed"),
                    ("VOIDED", "Dibatalkan"),
                ],
                default="PARSING",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="salesimportbatch",
            name="void_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="salesimportbatch",
            name="voided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesimportbatch",
            name="voided_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="voided_sales_imports",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
