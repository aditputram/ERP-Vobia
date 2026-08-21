# VOBIA ERP — Source Schema Audit

Status: Read-only discovery audit  
Audit date: 19 August 2026  
Access boundary: Shared Drive ADITYA only  
Live writes: None  
Forbidden reference opened: No (`2026 Projection Vobia` was not opened)

## 1. Audited sources

| Workbook | Spreadsheet ID | Timezone | Purpose |
|---|---|---|---|
| Bank Data All Source 26 | `1rf9-CDDYj0ks9AU3eJn_O6WMFc1S4649ksaGV9Fj364` | America/Los_Angeles | Canonical Product/SKU master |
| Vobia Sales 2026 | `14iBjrGwlLtOKpk8O6cefDyOZAYztkX_fb22wToI41-k` | Asia/Jakarta | Sales transaction and traffic |
| Vobia MD 2026 | `1crjvZPKrSSj2MFrysQZ3PWH5rUHrkhULzArjykFSlvU` | Asia/Jakarta | MD, PPIC, PO, QC, inbound, inventory, FIFO, return, and PO Aging |

The Bank Data tabs in Sales and MD both contain the same formula:

`=IMPORTRANGE("1rf9-CDDYj0ks9AU3eJn_O6WMFc1S4649ksaGV9Fj364","Vobia!A1:M2000")`

Metadata on the source workbook identifies the tab as `VOBIA`. This confirms one canonical source.

## 2. Canonical Bank Data profile

Canonical range: `VOBIA!A1:M747`.

Headers:

1. SOURCE
2. SKU
3. Parrent Sku
4. ARTICLE
5. CATEGORY
6. SUB CATAGORY
7. VARIANT
8. SUB VARIANT
9. STATUS PRODUCT
10. COGS
11. Retail Price
12. Kode Shopee
13. Kode Tiktok

Data profile:

- Data rows: 746.
- Unique SKU: 746.
- Duplicate SKU: 0.
- Source: Vobia for all 746 rows.
- Missing Parent SKU: 34.
- Missing COGS: 40.
- Missing Retail Price: 40.
- The same 40 records lack both COGS and Retail Price; all are Seasonal New.
- Missing Kode Shopee: 139.
- Missing Kode Tiktok: 127.

Status counts:

| Status | SKU count |
|---|---:|
| Discontinue | 248 |
| Regular | 242 |
| Seasonal New | 105 |
| Essential+ | 96 |
| Seasonal Regular | 55 |

Category counts:

| Category | SKU count |
|---|---:|
| Shirt | 208 |
| Pants | 136 |
| Knitwear | 129 |
| T-Shirt | 103 |
| Socks | 90 |
| Jacket | 63 |
| Headwear | 16 |
| Packaging | 1 |

### Migration implications

- ERP field names use correctly spelled canonical names internally while retaining source-header aliases (`Parrent Sku`, `SUB CATAGORY`) in the import contract.
- Marketplace product codes are stored as text. TikTok codes exceed safe JavaScript integer precision and must never be parsed as numbers.
- Missing marketplace codes are allowed with a mapping warning because not every SKU must be listed on every channel.
- Missing COGS/Retail Price does not prevent master import, but it blocks financial posting/PO release for affected SKU until resolved or explicitly handled by an approved rule.
- Missing Parent SKU remains a data-quality warning until its legitimate standalone/incomplete meaning is confirmed.
- Source workbook timezone is not used for business timestamps. ERP stores consistent instants and displays business time in Asia/Jakarta.

## 3. Vobia Sales 2026

### Transaction

- Sheet ID: 287890533.
- Grid: 80,287 rows × 22 columns.
- Header row: 1; data currently reaches row 80,287 (80,286 data rows).
- Canonical columns A:V: Month, Date, Source, Source Group, No. Pesanan, Status Pesanan, SKU, Status Produk, Category, Sub Category, Nama Produk, Nama Variasi, Qty, Harga Setelah Diskon, Retail Price, COGS, Total Gross Sales, Total Net Sales, Total COGS, Margin, GPM Rate, Traffic Product Mapping.
- Current grain matches `Source + No. Pesanan + SKU`.
- Month is a display label and Date is day-of-month only; ERP stores a real order date/timestamp from the raw export.
- Product/status/category/price/COGS fields are currently derived from live Bank Data formulas.

Important migration constraint:

- ERP must freeze transaction financial snapshots. Importing formula semantics literally would allow later master changes to alter history.
- Live GPM Rate formula is `Total Margin / unit Retail Price` (`T/O`), so Qty > 1 can make the result exceed 100%.
- Adit confirmed the ERP business definition on 19 August 2026: `GPM = Total Net Sales - Total COGS` and `GPM Rate = GPM / Total Gross Sales`.
- Therefore the current live `T/O` formula is migration evidence, not the ERP calculation contract. ERP uses immutable transaction snapshots for all three monetary inputs.

### Traffic Product Shopee

- Sheet ID: 20463562.
- Data reaches row 3,456 (3,455 data rows).
- Canonical grain is month + Kode Shopee/product page.
- Core source metrics: product views, clicks, and product visitors; sales metrics are derived.

### Traffic Product TikTok

- Sheet ID: 1064146853.
- Data reaches row 1,966 (1,965 data rows).
- Latest observed month in the stored data is July; August traffic has not yet been loaded.
- Canonical grain is month + Kode Tiktok/product page.

### ERP implications

- August TikTok traffic should appear as an Import Requirement until uploaded and confirmed complete.
- Sales/traffic parser must use raw export headers and a versioned alias map, not these report-output columns alone.
- Raw marketplace export header audit remains pending until representative source files are supplied/approved.

## 4. Vobia MD 2026

### MD Actual

- Sheet ID: 20260812.
- Grid: 1,000 rows × 181 columns.
- Header rows: 1–4; SKU rows currently 5–750, covering 746 SKU.
- Current layout repeats approximately 14 metrics per month and adds August MOS.
- ERP must not model this as 181 database columns. It is normalized to `scenario/version + year-month + SKU` rows.

### MD Actual Data gap

- Sheet ID: 20260811.
- Header: 25 normalized columns.
- Current populated data ends at row 4,824.
- It contains 689 SKU × seven months (January–July), not the current 746 SKU master and not August–December.
- This helper is not a complete migration source for current MD state.

### PPIC Requirement coverage gap

- Sheet ID: 1523283237.
- Grain/Need Key: Need Month + SKU.
- Live spill formula reads `MD Actual` rows 5–693 for September–December and filters Incoming Qty > 0.
- `MD Actual` itself is populated through row 750.
- Therefore rows 694–750 (57 SKU) are outside the current PPIC formula source range. If any of them receives future Incoming Qty, the current PPIC helper will omit it.

ERP requirement:

- Generate PPIC requirements from the approved normalized Incoming Plan table with no fixed row cutoff.
- Reconciliation must explicitly test all 746 current SKU and future master additions.

### Purchase Order

- Sheet ID: 20260818.
- Columns: Created At, PO Key, No. PO, Need Month, Required Arrival, product snapshots, SKU, COGS, PO Qty, PO COGS.
- COGS is stored as a snapshot and is suitable as the inbound FIFO cost source.
- Supplier is currently stored in PO Tracking rather than the PO database. ERP moves Supplier to the PO header because one approved No. PO has exactly one Supplier and one Need Month.

### PO Tracking

- Sheet ID: 1270197246.
- 22 columns.
- Manual tracking fields include Supplier, PO Status, ETA, Actual Received Date, and Notes.
- Received Qty and Qty QC Passed are derived from detail logs.

### QC Detail

- Sheet ID: 20260824.
- 20 columns.
- Current source records only Qty QC Passed; it has no Qty Inspected, Qty Failed, Failure Reason, or disposition.
- ERP adds these fields prospectively. Legacy migration must not invent failed quantities.

### Inbound Detail

- Sheet ID: 20260823.
- 20 columns.
- Supports partial receipt by repeated No. PO + SKU rows.
- Current source has Received By but no Warehouse field. Warehouse mapping/default requires a separate decision before migration.

### Return Log

- Sheet ID: 20260821.
- 17 columns.
- Return Key: Source + No. Pesanan + SKU.
- Current process records only physically received sellable goods, but the sheet has no explicit condition field.
- ERP migration should mark legacy return receipts as Sellable only after the migration rule is explicitly approved and should preserve a legacy-source marker.

### Inventory/FIFO

- Inventory Turnover header has 22 columns and matches the planned movement-ledger read model.
- FIFO Opening stores SKU, Opening Qty, Frozen Unit COGS, Opening Inventory COGS, Cutover Date, and Layer Key.
- Inventory Movement Data separates quantity movements from FIFO cost output.
- FIFO Engine returns unit/debit/credit/running cost, status, and Movement Key.
- PO Aging has 28 columns and exposes Opening/PO layer, remaining quantity/cost, close dates, aging, and exception status.

Key-design migration requirement:

- ERP uses an immutable unique ID for every movement plus a unique source-line business key.
- Partial inbound and partial physical return receipts must remain separate even when they share month, PO, order, and SKU.
- Legacy movement keys are retained as migration references, not used as the sole internal primary key.

### Live filter observation

At audit time, `Inventory Turnover` row-2 filter-aware summary showed Opening 124, Incoming 2, Sales Out 87, Return In 0, Ending 39 rather than the known full-ledger baseline. This is consistent with an active Basic Filter. The audit did not reset or change that filter. Migration/reconciliation must read the underlying ledger and explicitly control filter state rather than treating a filter-aware subtotal as an unfiltered total.

## 5. Confirmed changes to the logical model

- Master Product import source is now exact and verified.
- Product codes are text fields.
- Supplier belongs on the PO header.
- Month/year is normalized rather than repeated columns.
- Raw import/staging/parser-version entities remain required.
- Financial snapshots are mandatory at transaction/PO posting.
- Warehouse cannot be inferred from current Inbound Detail.
- Legacy QC/return fields require migration provenance rather than fabricated values.
- PPIC generation must be data-driven with no row-number boundary.

## 6. Open items before physical schema is final

1. Default/initial warehouse and warehouse master fields.
2. Supplier master fields and migration of current PO Tracking supplier values.
3. Treatment of 40 Seasonal New SKU without COGS/Retail Price.
4. Meaning/treatment of 34 missing Parent SKU values.
5. Approval to migrate current Return Log rows as legacy Sellable physical receipts.
6. Representative raw Shopee/TikTok transaction and traffic export headers and file sizes.
7. MOQ per color and size allocation.
8. Required Arrival/lead-time rules.

## 7. Audit safety statement

- No Google Sheets value, formula, formatting, validation, filter, Apps Script, or metadata was changed.
- No Shared Drive other than the authorized ADITYA scope was searched or inspected.
- `2026 Projection Vobia` was not opened.
