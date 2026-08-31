from datetime import date

from django.core.management.base import BaseCommand, CommandError

from dashboard.social_sync import sync_daily


class Command(BaseCommand):
    help = "Sync D-1 social metrics and re-scan the last four completed days."

    def add_arguments(self, parser):
        parser.add_argument("--cutoff", type=date.fromisoformat)
        parser.add_argument("--lookback-days", type=int, default=4)

    def handle(self, *args, **options):
        if not 1 <= options["lookback_days"] <= 90:
            raise CommandError("--lookback-days harus 1 sampai 90.")
        runs = sync_daily(
            cutoff=options["cutoff"], lookback_days=options["lookback_days"],
            source="management-command",
        )
        for run in runs:
            self.stdout.write(f"{run.platform}: {run.status} (cutoff {run.cutoff})")
        if any(run.status == run.Status.FAILED for run in runs):
            raise CommandError("Satu atau lebih platform gagal; snapshot lama dipertahankan.")
