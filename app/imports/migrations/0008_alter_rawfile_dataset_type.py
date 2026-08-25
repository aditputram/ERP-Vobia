from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("imports", "0007_salesimportbatch_voiding")]

    operations = [
        migrations.AlterField(
            model_name="rawfile",
            name="dataset_type",
            field=models.CharField(
                choices=[
                    ("MASTER_PRODUCT", "Master Product"),
                    ("FIFO_OPENING", "FIFO Opening"),
                    ("PO_WIP", "PO WIP Migration"),
                    ("SALES_SHOPEE", "Sales Shopee"),
                    ("SALES_TIKTOK", "Sales TikTok"),
                    ("SALES_HISTORICAL", "Sales Historical"),
                    ("TRAFFIC_SHOPEE", "Traffic Shopee"),
                    ("TRAFFIC_TIKTOK", "Traffic TikTok"),
                ],
                max_length=40,
            ),
        )
    ]
