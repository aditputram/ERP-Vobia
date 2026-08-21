# VOBIA ERP — Decision Log

## 21 August 2026

- FIFO migration uses the signed Ending Stock balance at end of day 31 July 2026. The resulting opening layer is available from 1 August 2026.
- August incoming is not included in FIFO Opening. Warehouse records each actual August inbound manually when the goods are physically received.
- Existing `Return Log` data in Vobia MD 2026 is test/UAT data and is excluded from migration.
- Verified actual POs that remain outstanding at end of day 31 July 2026 are migrated as `PO WIP`. Workbook PO test rows are not a migration source.
- Quantities received through 31 July remain solely in FIFO Opening; Warehouse posts each PO WIP arrival from 1 August onward as a manual operational Inbound transaction.
- August is the first operational inventory month in ERP. New August inbound is not treated as migration data.
- Current-month Incoming provides Projection, Actual, and Comparison modes. Month close freezes Actual from physical Inbound while retaining the original projection and variance.
- Projected Incoming shortfall carries forward only when backed by valid outstanding PO/PO WIP. Carryover is shown separately from New Incoming Plan and cannot create duplicate purchasing requirements.
- The existing local FIFO opening foundation must be revalidated against this date semantic before the Warehouse module is accepted; this decision does not authorize a production cutover.

## 19 August 2026

- MVP is end-to-end through PPIC, PO, QC, inbound, FIFO inventory, returns, and PO Aging.
- MVP is single-user; `vobiasuperadmin` has full access.
- Authentication uses local username/password.
- ERP is a private cloud web application built and tested locally first.
- Marketplace sales and product traffic use manual raw-file upload in MVP.
- ERP generates required import periods from historical nonfinal orders and missing/incomplete traffic periods.
- Non-marketplace sales are entered manually in ERP.
- Future-month projection supports per-product percentage changes and target Stock Ratio with preview and approval.
- PPIC only receives Final Approved Incoming.
- One PO contains one Supplier and one Need Month.
- QC Failed qty is recorded with disposition and does not add stock.
- Actual sales may create visible Negative Stock/FIFO Short exceptions; cost is never invented.
- Only physically received Sellable returns restore inventory/FIFO.
- PO Aging warns from day 61 and has a 90-day target.
- Cutover requires exact key/quantity reconciliation, explainable monetary rounding within Rp1, and at least seven consecutive passing daily parallel-run cycles.
- Approved stack: Python, Django 5.2 LTS, PostgreSQL, Django Templates + HTMX 2.x, Tailwind CSS, and a modular-monolith architecture.
- Sales profitability uses Vobia's business definition: `GPM = Total Net Sales - Total COGS`; `GPM Rate = GPM / Total Gross Sales`.
- Foundation dependency pins: Django 5.2.17, psycopg 3.2.13, and Gunicorn 23.0.0. PostgreSQL remains the normal database; SQLite is allowed only for isolated local smoke testing.
- Master Product MVP uses Adit-uploaded `.xlsx`/`.csv` exports of the verified canonical Bank Data, with private raw evidence, checksum duplicate control, staging, preview, explicit approval, and atomic commit. Excel parsing is pinned to openpyxl 3.1.5.
