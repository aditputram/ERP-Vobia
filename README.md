# VOBIA ERP

Private cloud-based ERP for Vobia SNOP.

## Current phase

The local MVP now has a verified end-to-end operational flow:

- secure local login for `vobiasuperadmin`, lockout, CSRF, secure production settings, and audit trail;
- canonical Master Product import with private raw evidence, staging, validation, preview, approval, and atomic commit;
- Shopee/TikTok Sales import, status reconciliation, import-period requirements, manual non-marketplace Sales, immutable financial snapshots, and Vobia GPM/GPM Rate;
- product-level Traffic import with period completeness/re-open controls and duplicate-variation blocking;
- Merchandising Projection Builder, Final Approved Projection, Incoming Plan, and automatic PPIC Requirement revision;
- Review/Create PO, manual new-product PO, safe monthly numbering, frozen PO COGS, print view, cancellation controls, and tracking;
- partial QC and Inbound, immutable movement ledger, FIFO opening/inbound layers, Sales Out allocation, traceable correction, and open exception handling;
- Expected Return, physical return conditions, sellable FIFO restoration, PO close/re-open, and PO Aging;
- record-level reconciliation plus an in-app UAT guide.

No live workbook or production data has been changed or imported. Google Sheets remains the operational source until UAT and seven consecutive parallel-run cycles pass.

## Project folders

- `docs/` — product blueprint and requirements
- `design/` — UI/UX artifacts
- `app/` — application source code
- `database/` — schema and migration assets
- `tests/` — acceptance and automated tests

## Start locally for the first time

The project has an isolated Python environment in `.venv/`. From Terminal:

```bash
cd "/Users/aditya/Documents/VOBIA ERP"
./scripts/start-local.sh
```

On the first run, the script asks for the password for `vobiasuperadmin` through a hidden terminal prompt. The password is never written to source code or documentation. Then open `http://127.0.0.1:8000/`.

The local starter uses SQLite only as a smoke-test/UAT database. PostgreSQL remains required for staging/production.

## Verification commands

```bash
cd "/Users/aditya/Documents/VOBIA ERP/app"
VOBIA_USE_SQLITE=1 DJANGO_DEBUG=1 ../.venv/bin/python manage.py check
VOBIA_USE_SQLITE=1 DJANGO_DEBUG=1 ../.venv/bin/python manage.py test
```

## Canonical business knowledge

Business rules are maintained in `/Users/aditya/Documents/VOBIA - SAMUEL/VOBIA KNOWLEDGE/` and must be reviewed before implementation changes.

## Current boundary

This is a local development/UAT MVP, not a production deployment. Real source acceptance still requires Adit's representative Master, Sales, and Traffic files plus opening-stock evidence. Production hosting/vendor, domain, backup retention, restore evidence, and cutover approval remain separate decisions.
