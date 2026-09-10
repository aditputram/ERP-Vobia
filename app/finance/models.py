import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


FINANCE_CUTOVER_DATE = date(2026, 8, 31)
FINANCE_OPENING_DATE = date(2026, 9, 1)


class Account(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=180)
    account_type = models.CharField(max_length=12)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    currency = models.CharField(max_length=3, default="IDR")
    is_postable = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)

    def clean(self):
        super().clean()
        if self.parent_id and self.parent_id == self.id:
            raise ValidationError({"parent": "Akun tidak boleh menjadi induk dirinya sendiri."})

    def __str__(self):
        return f"{self.code} · {self.name}"


class JournalNumberSequence(models.Model):
    period = models.DateField(unique=True)
    last_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class JournalEntry(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"

    class Source(models.TextChoices):
        MANUAL = "MANUAL", "Jurnal Voucher"
        OPENING = "OPENING", "Opening Balance"
        SYSTEM = "SYSTEM", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=40, unique=True)
    entry_date = models.DateField()
    description = models.CharField(max_length=255)
    reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.MANUAL)
    source_metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="finance_journals_created",
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="finance_journals_posted",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-entry_date", "-number")

    def clean(self):
        super().clean()
        if self.entry_date and self.entry_date < FINANCE_OPENING_DATE:
            raise ValidationError(
                {"entry_date": "Transaksi Finance dimulai 1 September 2026 setelah cutover 31 Agustus 2026."}
            )

    @property
    def debit_total(self):
        return self.lines.aggregate(total=models.Sum("debit"))["total"] or Decimal("0")

    @property
    def credit_total(self):
        return self.lines.aggregate(total=models.Sum("credit"))["total"] or Decimal("0")

    def __str__(self):
        return self.number


class JournalLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="lines")
    line_number = models.PositiveSmallIntegerField()
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="journal_lines")
    description = models.CharField(max_length=255, blank=True)
    debit = models.DecimalField(max_digits=22, decimal_places=6, default=0)
    credit = models.DecimalField(max_digits=22, decimal_places=6, default=0)

    class Meta:
        ordering = ("entry", "line_number")
        constraints = [
            models.UniqueConstraint(
                fields=("entry", "line_number"),
                name="finance_unique_journal_line_number",
            ),
            models.CheckConstraint(
                condition=(Q(debit__gt=0, credit=0) | Q(credit__gt=0, debit=0)),
                name="finance_journal_line_one_sided_amount",
            ),
        ]

    def clean(self):
        super().clean()
        if self.account_id and (not self.account.is_active or not self.account.is_postable):
            raise ValidationError({"account": "Pilih akun transaksi yang aktif, bukan akun induk."})
        if (self.debit > 0) == (self.credit > 0):
            raise ValidationError("Isi tepat salah satu nilai Debit atau Kredit.")

    def __str__(self):
        return f"{self.entry.number} · {self.line_number}"
