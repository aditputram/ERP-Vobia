from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from finance.importers import stage_finance_cutover


class Command(BaseCommand):
    help = "Import Chart of Accounts dan stage opening journal dari Trial Balance."

    def add_arguments(self, parser):
        parser.add_argument("--coa", required=True)
        parser.add_argument("--trial-balance", required=True)
        parser.add_argument("--cutoff", default="2026-08-31")
        parser.add_argument("--actor")
        parser.add_argument("--replace-staged", action="store_true")

    def handle(self, *args, **options):
        cutoff = parse_date(options["cutoff"])
        if not cutoff or cutoff != date(2026, 8, 31):
            raise CommandError("Cutover Finance Vobia harus 31 Agustus 2026.")
        actor = None
        if options["actor"]:
            actor = get_user_model().objects.filter(username=options["actor"]).first()
            if actor is None:
                raise CommandError("Actor tidak ditemukan.")
        else:
            actor = get_user_model().objects.filter(is_superuser=True).first()
        try:
            entry = stage_finance_cutover(
                coa_path=options["coa"],
                trial_balance_path=options["trial_balance"],
                cutoff_date=cutoff,
                actor=actor,
                replace_staged=options["replace_staged"],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Finance cutover staged: {entry.number}, {entry.lines.count()} line, "
                f"Debit/Kredit {entry.debit_total}."
            )
        )
