from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from traffic.services.historical import migrate_historical_traffic


class Command(BaseCommand):
    help = "Migrate Jan-Jul product traffic from Vobia Sales 2026 without inventory impact."

    def add_arguments(self, parser):
        parser.add_argument("source_path")
        parser.add_argument("--source", choices=["Shopee", "Tiktok"], required=True)
        parser.add_argument("--username", default="vobiasuperadmin")

    def handle(self, *args, **options):
        try:
            actor = User.objects.get(username=options["username"])
        except User.DoesNotExist as exc:
            raise CommandError("User approval tidak ditemukan.") from exc
        batches = migrate_historical_traffic(options["source_path"], options["source"], actor)
        self.stdout.write(self.style.SUCCESS(
            f"{options['source']}: {len(batches)} month batches, {sum(batch.ready_rows for batch in batches)} metrics committed"
        ))
