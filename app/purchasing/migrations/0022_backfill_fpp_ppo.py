from django.db import migrations


def backfill_fpp_ppo(apps, schema_editor):
    ProductionPlan = apps.get_model("purchasing", "PPICProductionPlan")
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    for plan in ProductionPlan.objects.filter(
        production_method="FPP",
        purchase_order__isnull=False,
    ).select_related("purchase_order"):
        po = plan.purchase_order
        PurchaseOrder.objects.filter(pk=po.pk).update(
            parent_plan_id=plan.pk,
            child_code="",
            order_type="FPP",
        )
        if po.po_number and po.sequence and po.issue_month:
            ProductionPlan.objects.filter(pk=plan.pk).update(
                ppo_number=po.po_number.replace("PO-", "PPO-", 1),
                ppo_sequence=po.sequence,
                ppo_issue_month=po.issue_month,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("purchasing", "0021_ppo_child_purchase_orders"),
    ]

    operations = [
        migrations.RunPython(backfill_fpp_ppo, migrations.RunPython.noop),
    ]
