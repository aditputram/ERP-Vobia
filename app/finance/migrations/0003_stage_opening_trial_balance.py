from decimal import Decimal

from django.db import migrations


TRIAL_BALANCE_FILENAME = "neraca_percobaan_ptvobialegasisa_260910112827.xlsx"
TRIAL_BALANCE_SHA256 = "aee53e8e1fea95ad6fccb133f70d79eb0a28deb2023098635c2ea4e128353072"
COA_FILENAME = "akun-perkiraan (1).xlsx"
COA_SHA256 = "03ed15f2328a0b496d796375cb4fe6c672075ad2b40dbb15aaa76f08a0666414"

OPENING_DATA = """110101|15181|0
110104|227537095|0
110105|373298781|0
110108|452417324.86|0
110301|317139424|0
110401|1693521215.243363|0
110501|197672475|0
110503|95833334|0
110504|8880000|0
110505|4000000|0
110507|51539399|0
110608|880483|0
120005|195069600|0
12000604|0|7086275.000004
210102|0|163911718
210103|0|528003493
210105|0|700041920
210106|0|80678256
210110|0|384492401
210114|0|401945435
210125|0|72287000
210203|0|238144466.79
210208|0|5815788
300003|0|300000000
410001|0|39283269
410002|0|357168000
410003|0|1150256000
410004|0|1522121900
410005|0|284634000
410006|0|540210999
410007|0|5838000
410008|0|3276000
440102|170295017|0
440103|133603000|0
5101|1328253982.756637|0
610001|330200356|0
610002|21011242|0
610003|14688840|0
610004|11519789|0
610005|18318483|0
610006|25755142|0
610007|10509840|0
610008|22166666|0
610009|1000000|0
610015|8040000|0
620001|307298639|0
620002|295535836|0
630001|44410684|0
630002|70585075|0
630003|204503877|0
630004|11100000|0
630005|21140765|0
630006|22226097|0
640001|11562242|0
650001|17937500|0
650002|16899079|0
650003|3037500|0
660001|21468910|0
660003|13591320|0
670001|2198240.93|0
670004|1446211|0
810001|7086275.000004|0"""


def stage_opening_trial_balance(apps, schema_editor):
    Account = apps.get_model("finance", "Account")
    JournalEntry = apps.get_model("finance", "JournalEntry")
    JournalLine = apps.get_model("finance", "JournalLine")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    if JournalEntry.objects.filter(number="OPENING-20260831").exists():
        return

    rows = [line.split("|") for line in OPENING_DATA.splitlines()]
    debit_total = sum((Decimal(debit) for _code, debit, _credit in rows), Decimal("0"))
    credit_total = sum((Decimal(credit) for _code, _debit, credit in rows), Decimal("0"))
    if len(rows) != 62 or debit_total != credit_total:
        raise RuntimeError("Sumber opening Finance harus berisi 62 baris yang balance.")

    account_map = {
        account.code: account
        for account in Account.objects.filter(code__in=[code for code, _debit, _credit in rows])
    }
    missing_codes = sorted({code for code, _debit, _credit in rows} - set(account_map))
    if missing_codes:
        raise RuntimeError("COA opening tidak ditemukan: " + ", ".join(missing_codes))

    entry = JournalEntry.objects.create(
        number="OPENING-20260831",
        entry_date="2026-09-01",
        description="Saldo awal berdasarkan Trial Balance per 31 Agustus 2026",
        reference="TB-2026-08-31",
        status="DRAFT",
        source="OPENING",
        source_metadata={
            "cutoff_date": "2026-08-31",
            "coa_filename": COA_FILENAME,
            "coa_sha256": COA_SHA256,
            "trial_balance_filename": TRIAL_BALANCE_FILENAME,
            "trial_balance_sha256": TRIAL_BALANCE_SHA256,
            "reconciliation_status": "PENDING_CONTROL_ACCOUNT_RECONCILIATION",
        },
    )
    JournalLine.objects.bulk_create(
        [
            JournalLine(
                entry=entry,
                line_number=index,
                account=account_map[code],
                description="Opening 1 September 2026",
                debit=Decimal(debit),
                credit=Decimal(credit),
            )
            for index, (code, debit, credit) in enumerate(rows, start=1)
        ]
    )
    AuditEvent.objects.create(
        action="finance_cutover_staged",
        entity_type="finance.journal_entry",
        entity_id=entry.id,
        after_values={
            "cutoff_date": "2026-08-31",
            "accounts": 176,
            "opening_lines": 62,
            "debit": str(debit_total),
            "credit": str(credit_total),
            "status": "DRAFT",
        },
        metadata=entry.source_metadata,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0002_seed_accurate_coa"),
    ]

    operations = [migrations.RunPython(stage_opening_trial_balance, migrations.RunPython.noop)]
