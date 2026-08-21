# VOBIA ERP — Recommended Technical Architecture

Status: Approved for MVP  
Date: 19 August 2026

## 1. Architecture style

Use a modular monolith: one deployable web application with clear internal domain modules and one transactional database.

Why:

- Vobia needs strong cross-module transactions and auditability.
- A small MVP team benefits from one codebase, one deployment, and one place to diagnose failures.
- Domain boundaries remain explicit so modules or APIs can be separated later if scale genuinely requires it.
- Microservices are intentionally excluded from MVP because they add distributed transactions, more deployments, and more failure modes without a current business need.

## 2. Recommended stack

| Layer | Recommendation | Purpose |
|---|---|---|
| Application | Python + Django 5.2 LTS | Business rules, authentication, forms, imports, admin/support tools, and server-rendered pages |
| Database | PostgreSQL | Transactional source of truth, constraints, row locking, and reporting queries |
| Interactive UI | Django Templates + HTMX 2.x | Responsive filters, previews, approvals, partial page updates, and file upload without a separate frontend application |
| Styling | Tailwind CSS | Consistent responsive UI design system |
| Excel/CSV processing | Python parsing layer | Shopee/TikTok transaction and traffic ingestion based on verified headers |
| Raw-file storage | Private object storage | Immutable original uploads and import audit evidence |
| Local environment | Containerized application + PostgreSQL | Reproducible development and testing |
| Production shape | One application service + managed PostgreSQL + private object storage | Simple cloud operation with independent backups |

Exact package patch versions are pinned when the project is scaffolded and updated only through tested dependency changes.

## 3. Why Django

- Built-in username/password users, sessions, permissions, password hashing, forms, validation, migrations, and administrative support.
- Python is well suited to spreadsheet ingestion, reconciliation, projection calculations, and report generation.
- Django transaction boundaries can make an approved import all-or-nothing.
- The future role/permission model can be added without replacing the single-user MVP authentication foundation.

## 4. Why PostgreSQL

- ERP data is relational: Product/SKU, order lines, PO lines, QC, inbound, movements, FIFO layers, and returns have strict relationships.
- Unique, foreign-key, check, and exclusion rules belong in the database as well as application validation.
- Row locking and transaction isolation support safe numbering, approvals, allocation, and FIFO processing.
- Monetary fields use exact decimal/numeric columns; binary floating point is forbidden for money and unit cost.

## 5. Security baseline

- Local username/password authentication; initial username `vobiasuperadmin`.
- Password is created through a secure setup command and stored only as a supported password hash.
- HTTPS is mandatory outside local development.
- Login rate limiting/temporary lockout is added explicitly.
- Secure, HTTP-only, same-site session cookies and CSRF protection.
- Secrets live in environment/secret storage, never source control.
- Raw uploads are private, size-limited, type-checked, renamed internally, and never executed or served as application code.
- Production database and raw-file storage are not directly public.

## 6. Domain modules

The Django project is split into internal modules with one-directional service boundaries:

1. `accounts`
2. `audit`
3. `master_data`
4. `imports`
5. `sales`
6. `traffic`
7. `merchandising`
8. `ppic`
9. `purchasing`
10. `quality`
11. `inventory`
12. `returns`
13. `reconciliation`
14. `dashboard`

Modules may read through explicit query services and change cross-module state through application services. UI views must not contain core FIFO, PO numbering, or reconciliation rules.

## 7. Data-integrity rules

- Internal primary keys use UUIDs; human business keys remain separately constrained.
- All timestamps are stored consistently and displayed in Asia/Jakarta.
- Business commits use short database transactions; user preview/review never keeps a transaction open.
- Import approval creates one atomic commit or a complete rollback.
- Raw rows are staged separately from canonical transactions.
- Historical snapshots and movements are append-only in normal application flows.
- Destructive deletion is unavailable for released/posted business records; reversal, amendment, cancellation, or adjustment creates new audited records.
- Audit events contain actor, action, entity, record ID, timestamp, reason, and before/after values where appropriate.

## 8. Import architecture

`Raw File → Upload Record → Parsed Staging Rows → Validation Issues → Reconciliation Preview → Approval → Atomic Canonical Commit`

- Parsing never writes directly to Sales or Inventory.
- Repeated files are detected through file identity/checksum and business keys.
- Parser versions are stored so historical imports can be reproduced.
- Approval is rejected if blocking validation issues remain.
- A committed import can be reversed through an audited compensating workflow; raw evidence remains immutable.

## 9. FIFO execution

- FIFO is a deterministic domain service operating on movement records and cost layers.
- Movement posting and FIFO allocation occur inside a database transaction.
- Allocation locks only the relevant SKU/layers in a consistent order.
- Insufficient layers create explicit short-qty exceptions; they do not fabricate cost.
- Rebuild/reconciliation tools run in read-only comparison mode before any corrective posting.

## 10. Testing layers

- Unit tests for formulas, normalization, status mapping, projection, PO numbering, and FIFO allocation.
- Database constraint tests for duplicate keys and illegal quantities.
- Workflow tests for import approval, PO, QC, inbound, return, and close/reopen.
- Golden-file tests using representative marketplace exports with sensitive values removed where necessary.
- Reconciliation tests against approved workbook baselines.
- Browser acceptance tests for the critical single-user flows.

## 11. Deployment phases

1. Local development with test data.
2. Private staging with sanitized or approved test data.
3. Migration rehearsal and read-only reconciliation.
4. Seven consecutive parallel-run cycles.
5. Approved production cutover.
6. Stabilization with Sheets fallback and monitored backups.

## 12. Decisions intentionally deferred

- Cloud/hosting vendor.
- Object-storage vendor.
- Background job/queue technology; selected after measuring actual import size and runtime.
- Monitoring vendor.
- Domain name and DNS configuration.
- Backup retention and recovery objectives.
