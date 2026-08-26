"""Buat akun per orang untuk anggota tim.

Satu akun dipakai ramai-ramai membuat jejak audit kehilangan artinya: semua
tindakan tercatat atas nama orang yang sama. Perintah ini memberi setiap orang
akunnya sendiri, lengkap dengan peran sebagai label grup.

    DJANGO_NEW_USER_PASSWORD=... python manage.py create_team_user budi --role gudang
"""

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from audit.services import record_audit

ROLES = ["owner", "merchandising", "purchasing", "gudang", "finance", "viewer"]


class Command(BaseCommand):
    help = "Buat akun anggota tim dengan peran (password dari DJANGO_NEW_USER_PASSWORD)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--role", choices=ROLES, required=True)
        parser.add_argument("--full-name", default="")
        parser.add_argument(
            "--staff",
            action="store_true",
            help="beri akses halaman /admin (hanya untuk yang benar-benar perlu)",
        )

    def handle(self, *args, **options):
        password = os.getenv("DJANGO_NEW_USER_PASSWORD", "")
        if not password:
            raise CommandError("DJANGO_NEW_USER_PASSWORD belum diisi.")

        user_model = get_user_model()
        username = options["username"]

        with transaction.atomic():
            if user_model.objects.filter(username=username).exists():
                raise CommandError(f"Akun {username} sudah ada.")

            user = user_model(
                username=username,
                first_name=options["full_name"][:150],
                is_active=True,
                is_staff=options["staff"],
                is_superuser=False,
            )
            try:
                validate_password(password, user)
            except ValidationError as exc:
                raise CommandError("Password ditolak: " + "; ".join(exc.messages))
            user.set_password(password)
            user.save()

            group, _ = Group.objects.get_or_create(name=options["role"])
            user.groups.add(group)

            record_audit(
                actor=user,
                action="team_user_created",
                entity_type="accounts.user",
                entity_id=user.pk,
                metadata={"role": options["role"], "staff": options["staff"]},
            )

        self.stdout.write(self.style.SUCCESS(f"Akun {username} dibuat dengan peran {options['role']}."))
