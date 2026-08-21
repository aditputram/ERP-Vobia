from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from imports.models import SalesImportBatch
from imports.services.historical_sales import (
    commit_historical_sales_batch,
    create_historical_sales_import,
)


class Command(BaseCommand):
    help = "Migrate canonical Vobia Sales Transaction Jan-Jul without inventory posting."

    def add_arguments(self, parser):
        parser.add_argument("source_path")
        parser.add_argument("--username", default="vobiasuperadmin")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        try:
            actor = User.objects.get(username=options["username"])
        except User.DoesNotExist as exc:
            raise CommandError("User approval tidak ditemukan.") from exc
        batch = create_historical_sales_import(options["source_path"], actor)
        self.stdout.write(f"Batch {batch.id} · {batch.status} · {batch.total_rows} rows · {batch.warning_count} warnings")
        if options["commit"] and batch.status != SalesImportBatch.Status.COMMITTED:
            batch, counts = commit_historical_sales_batch(batch.id, actor)
            self.stdout.write(self.style.SUCCESS(f"Committed {counts}"))
