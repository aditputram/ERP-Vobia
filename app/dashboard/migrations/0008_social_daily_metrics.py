from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0007_campaign_creative_asset_url")]

    operations = [
        migrations.CreateModel(
            name="SocialDailyMetric",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("platform", models.CharField(choices=[("INSTAGRAM", "Instagram"), ("TIKTOK", "TikTok")], max_length=20)),
                ("account", models.CharField(max_length=100)),
                ("date", models.DateField()),
                *[(name, models.PositiveBigIntegerField(blank=True, null=True)) for name in (
                    "reach", "impressions", "total_engagement", "accounts_engaged", "profile_visits",
                    "website_clicks", "likes", "comments", "shares", "new_followers", "lost_followers",
                )],
                ("synced_at", models.DateTimeField()),
            ],
            options={"ordering": ("platform", "account", "date")},
        ),
        migrations.CreateModel(
            name="SocialSyncRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("idempotency_key", models.CharField(max_length=120, unique=True)),
                ("platform", models.CharField(choices=[("INSTAGRAM", "Instagram"), ("TIKTOK", "TikTok")], max_length=20)),
                ("account", models.CharField(max_length=100)),
                ("source", models.CharField(max_length=30)),
                ("actor", models.CharField(blank=True, max_length=150)),
                ("status", models.CharField(choices=[("RUNNING", "Running"), ("COMPLETED", "Completed"), ("FAILED", "Failed")], max_length=20)),
                ("cutoff", models.DateField()),
                ("started_at", models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("snapshot_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.CharField(blank=True, max_length=255)),
            ],
            options={"ordering": ("-started_at",)},
        ),
        migrations.AddConstraint(
            model_name="socialdailymetric",
            constraint=models.UniqueConstraint(fields=("platform", "account", "date"), name="dashboard_social_daily_platform_account_date_unique"),
        ),
    ]
