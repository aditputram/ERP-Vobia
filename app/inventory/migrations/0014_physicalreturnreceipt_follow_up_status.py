from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0013_rejected_goods_inventory_movement"),
    ]

    operations = [
        migrations.AddField(
            model_name="physicalreturnreceipt",
            name="follow_up_status",
            field=models.CharField(
                choices=[
                    ("NOT_SUBMITTED", "Belum Diajukan"),
                    ("SUBMITTED", "Diajukan ke Marketplace"),
                    ("IN_REVIEW", "Diproses Marketplace"),
                    ("APPROVED", "Kompensasi Disetujui"),
                    ("REJECTED", "Kompensasi Ditolak"),
                    ("COMPENSATED", "Kompensasi Diterima"),
                ],
                default="NOT_SUBMITTED",
                max_length=30,
            ),
        ),
    ]
