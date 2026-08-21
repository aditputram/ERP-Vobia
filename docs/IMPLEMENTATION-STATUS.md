# VOBIA ERP — Implementation Status

Updated: 21 August 2026

## Current result

The local MVP implements the full agreed operational chain:

`Master → Sales/Traffic → Projection → Incoming → PPIC → PO → QC → Inbound → Inventory/FIFO → Return → PO Aging → Reconciliation`

The application remains local and uses SQLite only for development/UAT. No live workbook, Shared Drive data, or production service was changed.

## Verified capabilities

### Foundation and security

- Local username/password authentication for `vobiasuperadmin`; password remains hashed and was entered by Adit.
- Five-failure/15-minute login lockout, CSRF, secure-session production settings, and append-only audit events.
- Production-like `manage.py check --deploy` passes with HTTPS/HSTS/secure-cookie flags enabled.

### Master, Sales, and Traffic

- Canonical Master Product `.xlsx/.csv` upload, private raw evidence, SHA-256 duplicate prevention, staging, blockers/warnings, preview, approval, atomic commit, and value history.
- Shopee and TikTok Sales parsers verified against representative local exports; TikTok discount allocation and Shopee return normalization are tested.
- Historical financial snapshots remain immutable when Master Retail Price/COGS later changes.
- Vobia definitions: `GPM = Net Sales − COGS`; `GPM Rate = GPM / Gross Sales`.
- Net price above Master Retail uses the approved special case: Retail snapshot becomes net price, Gross = Net, and discount cannot become negative.
- Sales import requirement reports every historical Source/month with nonfinal status and new-data cutoff through yesterday.
- Manual non-marketplace transaction keeps the actual Source label and posts through the same snapshot/FIFO path.
- Traffic parser maps product-page code to canonical Product, blocks duplicate variation rows, supports safe MTD re-import update, complete, and audited re-open.
- Representative real Traffic export headers are still required during UAT; unknown/ambiguous headers intentionally block rather than being guessed.

### Merchandising, PPIC, and PO

- Current-month projection uses actual through the last available Sales cutoff and the agreed multiplier from the projection run date. Beginning = prior Ending + current Incoming, with Sales Qty capped to non-negative Beginning.
- Official matrix separates January–July actual/reference, August current projection, and blank September–December cells pending Projection Builder; future workbook formulas remain immutable audit reference only.
- Dashboard Summary is connected to the same official projection service. Verified labels/formulas are Stock Value Ratio, ITO YTD, GPM value, GPM Rate, Margin Ratio, and Incoming Capital Turnover; future blanks do not enter totals.
- Future methods: Increase %, Decrease %, Target Stock Ratio.
- Scope priority: Product > Category > Product Status; product-specific rule remains possible.
- Whole-unit approval, stock guardrails, Minimum Incoming, Target Ratio recommendation, and Final Approved Incoming.
- Approved Incoming automatically creates/revises Need Month + SKU PPIC Requirement; qty already allocated to PO cannot be silently reduced.
- PO review/draft/release, multi-requirement PO, manual new-product PO, concurrency-safe monthly sequence, `PO-VOB-MM/YY-NNN`, frozen COGS, print view, and audited cancellation.

### QC, Inbound, Inventory, Return, and Aging

- Partial QC and Inbound; cumulative Inspected ≤ PO Qty and cumulative Received ≤ QC Passed.
- QC never adds inventory; actual Inbound creates one immutable Incoming movement and one PO-cost FIFO layer.
- Immutable FIFO Opening snapshot at end of day 31 July 2026, available for movements from 1 August; positive layers, zero evidence, negative evidence, quantities, costs, and existing FIFO allocations were preserved during the date/key rebaseline.
- Warehouse now has five operational subtabs: Inventory Summary, Inventory Turnover, Inbound, Return Log, and Outbound.
- Inventory Summary includes all 746 active SKU and uses signed opening + post-cutover movements; negative opening is part of official balance without creating a fake FIFO layer.
- Inventory Turnover exposes the immutable ledger and per-SKU running balance. Date/type filters do not remove earlier movements from the running-balance calculation.
- PO WIP has an explicit source, migration cutoff/evidence, received-before-cutover, and QC-passed-before-cutover contract. Actual PO WIP records remain uncommitted until the approved source file is reviewed.
- Sales Out consumes oldest layers, preserves actual transaction on short stock, and records FIFO Short without invented cost.
- Traceable Adjustment requires date, reason, evidence/reference, and approved cost for Adjustment In; valid historical evidence can resolve a linked exception.
- Marketplace Retur creates Expected Return; only physical Sellable return posts Return In and restores the originating sale allocations.
- PO Remaining = outstanding inbound + remaining PO layers; close/re-open and 60/90-day aging states are derived automatically.
- Merchandising Incoming current month supports Projection, Actual Warehouse, and Comparison. Month close freezes actual QTY/COGS/Gross and variance while preserving the original projection.
- Carry-over is created only from a valid outstanding PO line at month close, remains linked to PO + SKU, does not duplicate PPIC Requirement, and is added to future official Incoming together with any approved new plan.

### Reconciliation and usability

- Record-level checks cover shipped/final Sales ↔ Sales Out, Inbound ↔ Incoming, QC/PO limits, Inbound/QC limits, FIFO coverage, movement/FIFO balance, and PO close validity.
- Five Warehouse pages plus Merchandising Projection/Dashboard passed authenticated browser smoke tests without server errors.
- In-app `Panduan & UAT` page documents the exact operating sequence for Adit.

## Verification evidence

- Full automated regression: **106 tests passed**.
- End-to-end acceptance automation passed Projection → Incoming → PPIC → PO → QC → Inbound → FIFO Sales → sellable Return → PO reopen → resale → PO close → Reconciliation.
- Django system check: **0 issues**.
- Migration drift check: **No changes detected** at the checkpoint.
- Production-like deployment security check: **0 issues**.
- Real browser smoke check: Sales, Traffic, Merchandising, PPIC/PO, Inventory, and Reconciliation all loaded without server errors.

## Deliberately not performed

- No Google Sheets write and no access to `2026 Projection Vobia`.
- No production data commit or cutover; canonical records currently remain in the local UAT database.
- No cloud deployment, production credential, domain, or DNS change.
- No cutover: Google Sheets stays operational in parallel.

## Remaining acceptance gates

1. Adit provides the final approved PO WIP evidence/export as of 31 July; ERP previews and reconciles it before any PO WIP commit. Workbook Purchase Order, QC Detail, Inbound Detail, and Return Log test rows are not migration sources.
2. Adit runs guided UAT using representative real files and a controlled SKU/PO sample.
3. Real Traffic export headers are verified and aliases expanded only with evidence if needed.
4. Record-level reconciliation against approved workbook baselines reaches 100%; explained money rounding tolerance is at most Rp1.
5. Seven consecutive daily parallel-run cycles pass.
6. Hosting, managed PostgreSQL, backups/restore, monitoring, and cutover are separately approved.
