import django.db.models.deletion
import uuid

from django.db import migrations, models


def grant_sales_settings_access(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.all().iterator():
        access = dict(user.tab_access or {})
        if "finance" not in access:
            continue
        tabs = list(access.get("finance") or [])
        if "sales_settings" not in tabs and (
            "sales_invoice" in tabs or "sales_invoice_report" in tabs
        ):
            tabs.append("sales_settings")
            access["finance"] = tabs
            user.tab_access = access
            user.save(update_fields=("tab_access",))


def revoke_sales_settings_access(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.all().iterator():
        access = dict(user.tab_access or {})
        tabs = list(access.get("finance") or [])
        if "sales_settings" in tabs:
            access["finance"] = [tab for tab in tabs if tab != "sales_settings"]
            user.tab_access = access
            user.save(update_fields=("tab_access",))


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_user_tab_access"),
        ("finance", "0003_stage_opening_trial_balance"),
        ("master_data", "0005_reject_warehouse"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductSalesAccount",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("product", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="finance_sales_setting", to="master_data.product")),
                ("sales_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="product_sales_settings", to="finance.account")),
            ],
            options={"ordering": ("product__name",)},
        ),
        migrations.RunPython(grant_sales_settings_access, revoke_sales_settings_access),
    ]
