from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from audit.services import record_audit


class Command(BaseCommand):
    help = "Membuat akun awal vobiasuperadmin melalui prompt password tersembunyi."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="Ganti password akun yang sudah ada melalui prompt tersembunyi.",
        )

    def handle(self, *args, **options):
        username = "vobiasuperadmin"
        user_model = get_user_model()
        user = user_model.objects.filter(username=username).first()

        if user and not options["reset_password"]:
            self.stdout.write(
                self.style.WARNING(
                    "Akun vobiasuperadmin sudah ada. Gunakan --reset-password bila perlu."
                )
            )
            return

        password = getpass("Password baru: ")
        confirmation = getpass("Ulangi password: ")
        if password != confirmation:
            raise CommandError("Password dan konfirmasi tidak sama.")

        candidate_user = user or user_model(username=username)
        try:
            validate_password(password, candidate_user)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc

        if user is None:
            user = user_model(username=username)
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.set_password(password)
        user.save()

        record_audit(
            actor=user,
            action="superadmin_password_reset" if options["reset_password"] else "superadmin_created",
            entity_type="accounts.user",
            entity_id=user.pk,
        )

        action = "diperbarui" if options["reset_password"] else "dibuat"
        self.stdout.write(self.style.SUCCESS(f"Akun {username} berhasil {action}."))
