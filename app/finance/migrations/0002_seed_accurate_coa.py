from django.db import migrations


SOURCE_FILENAME = "akun-perkiraan (1).xlsx"
SOURCE_SHA256 = "03ed15f2328a0b496d796375cb4fe6c672075ad2b40dbb15aaa76f08a0666414"

COA_DATA = """1101|Kas & Bank|BANK||IDR
110101|Petty Cash|BANK|1101|IDR
110102|Bank BCA|BANK|1101|IDR
110103|Bank Mandiri|BANK|1101|IDR
110104|Marketplace Balance - Vobia Shopee|BANK|1101|IDR
110105|Marketplace Balance - Vobia Tiktok|BANK|1101|IDR
110106|Website Balance|BANK|1101|IDR
110107|Passing Through|BANK|1101|IDR
110108|Bank Mandiri - 1640019049719|BANK|1101|IDR
1102|Setara Kas|BANK||IDR
110201|Deposito Bank|BANK|1102|IDR
110202|Deposito|BANK|1102|IDR
1103|Piutang Usaha|AREC||IDR
110301|Sales Receivable IDR|AREC|1103|IDR
110302|Uang Muka Pembelian IDR|AREC|1103|IDR
1104|Persediaan|INTR||IDR
110401|Persediaan|INTR|1104|IDR
110402|Persediaan Terkirim|INTR|1104|IDR
1105|Current Asset|OASS||IDR
110501|Prepaid Production|OASS|1105|IDR
110502|Account Receivable|OASS|1105|IDR
110503|Prepaid Rent|OASS|1105|IDR
110504|Product Accessories - Asset|OASS|1105|IDR
110505|Prepaid System|OASS|1105|IDR
110506|Security Deposit|OASS|1105|IDR
110507|Employee Receivable|OASS|1105|IDR
1106|Aset Lancar Lainnya|OCAS||IDR
110601|Perlengkapan Kantor|OCAS|1106|IDR
110602|Sewa Gedung Dibayar Dimuka|OCAS|1106|IDR
110603|Asuransi Dibayar Dimuka|OCAS|1106|IDR
110604|PPN Masukan|OCAS|1106|IDR
110605|PPh 23 Penjualan|OCAS|1106|IDR
110606|Asset Purchase|OCAS|1106|IDR
110607|Income Tax Article 22 - Shopee Vobia|OCAS|1106|IDR
110608|Income Tax Article 22 - Tiktok Vobia|OCAS|1106|IDR
1200|Aset Tetap|FASS||IDR
120001|Tanah|FASS|1200|IDR
120002|Gedung|FASS|1200|IDR
120003|Kendaraan|FASS|1200|IDR
120004|Peralatan|FASS|1200|IDR
120005|Inventaris Kantor|FASS|1200|IDR
120006|Akumulasi Depresiasi Aset Tetap|DEPR||IDR
12000601|Akumulasi Penyusutan Gedung|DEPR|120006|IDR
12000602|Akumulasi Penyusutan Kendaraan|DEPR|120006|IDR
12000603|Akumulasi Penyusutan Peralatan|DEPR|120006|IDR
12000604|Akumulasi Penyusutan Inventaris Kantor|DEPR|120006|IDR
2101|Sundry Creditors|APAY||IDR
210101|Abang Produksi|APAY|2101|IDR
210102|Arifin|APAY|2101|IDR
210103|4Brother|APAY|2101|IDR
210104|Aria|APAY|2101|IDR
210105|Harmonia|APAY|2101|IDR
210106|Herpro Garment|APAY|2101|IDR
210107|Pak Haris|APAY|2101|IDR
210108|Jodi Kresna|APAY|2101|IDR
210109|RKK|APAY|2101|IDR
210110|Rabika|APAY|2101|IDR
210111|Soni|APAY|2101|IDR
210112|Yadi|APAY|2101|IDR
210113|Pelangi Print|APAY|2101|IDR
210114|Bayu|APAY|2101|IDR
210115|Andi|APAY|2101|IDR
210116|Ato|APAY|2101|IDR
210117|Rino|APAY|2101|IDR
210118|UD. Sederhana|APAY|2101|IDR
210119|Firman|APAY|2101|IDR
210120|PT. Mid Solusi Nusantara|APAY|2101|IDR
210121|Others|APAY|2101|IDR
210122|Hartono Comp. Pondok Indah|APAY|2101|IDR
210123|IBOX ONE BELPARK MALL|APAY|2101|IDR
210124|Vobia|APAY|2101|IDR
210125|Morph Apparel|APAY|2101|IDR
2102|Current Liabilities|OCLY||IDR
210201|PPN Keluaran|OCLY|2102|IDR
210202|PPh 23 Pembelian|OCLY|2102|IDR
210203|Account Payable|OCLY|2102|IDR
210204|Ads Payable|OCLY|2102|IDR
210205|Prepaid Allowance|OCLY|2102|IDR
210206|Salary Payable|OCLY|2102|IDR
210207|Prepaid Revenue|OCLY|2102|IDR
210208|Tax Payable 21|OCLY|2102|IDR
2201|Loans (Liability)|LTLY||IDR
220101|Loan Payable - Spinjam|LTLY|2201|IDR
220102|Loan Payable - MTF|LTLY|2201|IDR
3000|Modal|EQTY||IDR
300001|Equitas Saldo Awal|EQTY|3000|IDR
300002|Laba Ditahan|EQTY|3000|IDR
300003|Capital|EQTY|3000|IDR
4100|Sales A/C|REVE||IDR
410001|Vobia Socks|REVE|4100|IDR
410002|Vobia T-Shirt|REVE|4100|IDR
410003|Vobia Shirt|REVE|4100|IDR
410004|Vobia Knitwear|REVE|4100|IDR
410005|Vobia Jacket|REVE|4100|IDR
410006|Vobia Pants|REVE|4100|IDR
410007|Vobia Headwear|REVE|4100|IDR
410008|Vobia Packaging|REVE|4100|IDR
4401|Diskon Penjualan|REVE||IDR
440101|Diskon Penjualan IDR|REVE|4401|IDR
440102|Discount on Sale|REVE|4401|IDR
440103|Sales Return|REVE|4401|IDR
5101|Beban Pokok Penjualan|COGS||IDR
6100|HRGA Expense|EXPS||IDR
610001|Salary Expense|EXPS|6100|IDR
610002|Equipment & Supplies Office|EXPS|6100|IDR
610003|Office Support Expense|EXPS|6100|IDR
610004|Utilities Expense|EXPS|6100|IDR
610005|System & Service Expense|EXPS|6100|IDR
610006|Employee Development|EXPS|6100|IDR
610007|Business Trip|EXPS|6100|IDR
610008|Rent Expense (Office)|EXPS|6100|IDR
610009|CSR Expense|EXPS|6100|IDR
610010|Beban Katering & Makan Karyawan|EXPS|6100|IDR
610011|Beban THR|EXPS|6100|IDR
610012|Beban Tunjangan Kesehatan|EXPS|6100|IDR
610013|Beban Asuransi Karyawan|EXPS|6100|IDR
610014|Discount on Sale|EXPS|6100|IDR
610015|Product Accessories - Expense|EXPS|6500|IDR
610016|Legal Expense|EXPS|6100|IDR
610017|Beban Bonus, Pesangon & Kompensasi|EXPS|6100|IDR
6200|Sales Expense|EXPS||IDR
620001|Admin Sales - Shopee Vobia|EXPS|6200|IDR
620002|Admin Sales - Tiktok Vobia|EXPS|6200|IDR
620003|Consignment Fee|EXPS|6200|IDR
620004|Reture & Claim Expense|EXPS|6200|IDR
620010|Operating Production Expense|EXPS|6200|IDR
620011|Operating Expense|EXPS|6200|IDR
620022|Operating System Expense|EXPS|6200|IDR
620023|Advertising Fee - Shopee Vobia|EXPS|6200|IDR
620024|Advertising Fee - Tiktok Vobia|EXPS|6200|IDR
6300|Promotion Expense|EXPS||IDR
630001|Marketing Expense|EXPS|6300|IDR
630002|Meta Ads Vobia|EXPS|6300|IDR
630003|Tiktok ads Vobia|EXPS|6300|IDR
630004|Shopee Ads Vobia|EXPS|6300|IDR
630005|Shopee Influencer Vobia|EXPS|6300|IDR
630006|Tiktok Affiliate Vobia|EXPS|6300|IDR
630007|Operating Event|EXPS|6300|IDR
6400|RnD Expense|EXPS||IDR
640001|Development Expense|EXPS|6400|IDR
6500|Production Expense|EXPS||IDR
650001|Supply & Equipment Production|EXPS|6500|IDR
650002|Delivery Expense - Purchase|EXPS|6500|IDR
650003|QC Expense|EXPS|6500|IDR
6600|Warehouse Expense|EXPS||IDR
660001|Supply & Equipment Warehouse|EXPS|6600|IDR
660002|Packaging|EXPS|6600|IDR
660003|Delivery Expense - Sales|EXPS|6600|IDR
6700|Financial Expense|EXPS||IDR
670001|Administration Bank|EXPS|6700|IDR
670002|Interest Expense - Spinjam|EXPS|6700|IDR
670003|Interest Expense - MTF|EXPS|6700|IDR
670004|Tax Expense|EXPS|6700|IDR
7100|Pendapatan Diluar Usaha|OINC||IDR
710001|Pendapatan Jasa Giro|OINC|7100|IDR
710002|Pendapatan Bunga Deposito|OINC|7100|IDR
710003|Penjualan Persediaan / Perlengkapan|OINC|7100|IDR
710004|Laba/Rugi Revaluasi Aset|OINC|7100|IDR
710005|Pendapatan Diluar Usaha Lainnya|OINC|7100|IDR
710006|e-Commerce Courier Fee|OINC|7100|IDR
710007|Other Income|OINC|7100|IDR
710008|Discount on Purchase|OINC|7100|IDR
7200|Indirect Expense|OEXP||IDR
720001|Bunga Giro|OEXP|7200|IDR
720002|Beban Adm. Bank & Buku Cek/Giro|OEXP|7200|IDR
720003|Pajak Jasa Giro|OEXP|7200|IDR
720004|Laba/Rugi Terealisasi|OEXP|7200|IDR
720005|Laba/Rugi Belum Terealisasi|OEXP|7200|IDR
720006|Laba/Rugi Disposisi Aset|OEXP|7200|IDR
720007|Beban Diluar Usaha Lainnya|OEXP|7200|IDR
8100|Beban Penyusutan|EXPS||IDR
810001|Beban Penyusutan - Inventaris Kantor|EXPS|8100|IDR
810002|Beban Penyusutan - Peralatan|EXPS|8100|IDR
810003|Beban Penyusutan - Kendaraan|EXPS|8100|IDR
810004|Beban Penyusutan - Gedung|EXPS|8100|IDR
9100|Beban Penyesuaian Persediaan|OEXP||IDR"""


def seed_accurate_coa(apps, schema_editor):
    Account = apps.get_model("finance", "Account")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    rows = [line.split("|") for line in COA_DATA.splitlines()]
    if len(rows) != 176:
        raise RuntimeError("Sumber COA Finance harus berisi tepat 176 akun.")

    parent_codes = {parent_code for _code, _name, _account_type, parent_code, _currency in rows if parent_code}
    for code, name, account_type, _parent_code, currency in rows:
        Account.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "account_type": account_type,
                "currency": currency,
                "is_postable": code not in parent_codes,
                "is_active": True,
            },
        )

    account_map = {account.code: account for account in Account.objects.filter(code__in=[row[0] for row in rows])}
    for code, _name, _account_type, parent_code, _currency in rows:
        parent = account_map.get(parent_code) if parent_code else None
        account = account_map[code]
        if account.parent_id != getattr(parent, "id", None):
            account.parent = parent
            account.save(update_fields=("parent", "updated_at"))

    AuditEvent.objects.create(
        action="finance_coa_imported",
        entity_type="finance.chart_of_accounts",
        after_values={"accounts": 176, "parent_accounts": 24, "transaction_accounts": 152},
        metadata={"source_filename": SOURCE_FILENAME, "source_sha256": SOURCE_SHA256},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        ("finance", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed_accurate_coa, migrations.RunPython.noop)]
