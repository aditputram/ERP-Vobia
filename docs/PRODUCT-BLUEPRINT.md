# VOBIA ERP — Product Blueprint

Status: Discovery draft 0.1  
Date: 19 August 2026  
Owner: Aditya  
Assistant: Samuel

## 1. Product goal

Build a private cloud web application that translates the proven Vobia Sales and Operation workflows into a traceable, auditable ERP. Google Sheets remains operational in parallel until reconciliation and cutover are approved.

## 2. MVP boundary

The first MVP covers the end-to-end flow:

`Sales/Traffic Ingestion → Merchandising Projection → Incoming Plan → PPIC Requirement → Review/Create or Manual PO → QC → Inbound → Inventory/FIFO → Return → PO Aging → Reconciliation/Audit`

Implementation is incremental. Each slice must pass acceptance tests before the next slice is treated as complete.

## 3. User and authentication

- MVP is single-user.
- Initial username: `vobiasuperadmin`.
- The user has full access to every module.
- Authentication uses a local username and password, not Google login.
- Passwords are never stored as plaintext or committed to source control.
- The data model must remain ready for future multi-user role-based access.

## 4. Application and deployment

- Private browser-based cloud web application.
- Built and tested locally/private before cloud deployment.
- Cloud release requires security, backup, migration, reconciliation, and rollback verification.
- Google Sheets continues in parallel until cutover acceptance is signed off.

## 5. Navigation

1. Dashboard
2. Master Data
3. Sales
4. Merchandising
5. PPIC
6. Purchase Order
7. QC & Inbound
8. Inventory
9. PO Aging
10. Reconciliation & Audit
11. Settings

## 6. Dashboard

- Default period: current month through the latest actual-data cutoff.
- Shows Sales KPIs, FIFO inventory value, PPIC/PO/QC/inbound progress, PO Aging, reconciliation status, and critical exceptions.
- Planning and actual data must be visually distinct.
- Every KPI must drill down to its source records.
- Dashboard is a summary, not a source of truth.
- Negative stock, FIFO short, missing mapping, duplicate, over-QC, over-inbound, pending return, and reconciliation differences cannot be hidden.

## 7. Master Product

- The canonical source is the original Vobia Bank Data managed by Operation.
- The Bank Data tabs in Vobia Sales 2026 and Vobia MD 2026 are IMPORTRANGE derivatives from that single source.
- Source spreadsheet ID, tab, header, and keys must be verified read-only before an import contract is created.
- SKU detail is the transaction and inventory key.
- Historical transactions, frozen opening COGS, and old PO COGS snapshots cannot change when master data changes.
- Used SKUs cannot be hard-deleted; they may be deactivated.
- MVP flow: Upload canonical `.xlsx`/`.csv` export → Parse → Stage → Validate → Preview → Adit Approve → Atomic Commit.
- Raw file, checksum, parser version, issues, approval, and commit audit are retained.
- Missing Parent SKU is never guessed; preview isolates that SKU and raises a quality warning.
- Missing COGS/Retail Price does not block Master import, but affected SKU cannot be used for financial posting until resolved by an approved rule.
- Marketplace identifiers are text. Numeric TikTok values with unsafe precision block commit.

## 8. Sales and traffic ingestion

### Marketplace transaction upload

- Adit uploads original Shopee and TikTok Excel/CSV exports.
- Parser uses verified header names rather than fixed spreadsheet column letters.
- Flow: Upload → Parse → Preview → Validate → Reconcile → Approve → Commit.
- Transaction key: `Source + No. Pesanan + SKU`.
- Preview separates new records, status changes, pure cancellations, returns, duplicates, missing/unknown SKU, conflicts, and negative price/net sales.
- Raw file identity/checksum, upload time, parsing result, validation result, approval, and actor are retained for audit.

### Import Requirement

- ERP tells Adit which Source and period must be imported before upload.
- All historical nonfinal transactions are scanned, not only the current month.
- `Selesai` and `Retur` are final; every other status keeps its Source + period in the re-import queue.
- New transactions are requested through yesterday even if no nonfinal record exists.
- A requirement shows Source, date range, reason, nonfinal count/statuses, last successful import, and latest data cutoff.
- Requirements are recalculated after an approved import and remain open while nonfinal records remain.

### Traffic upload

- Adit uploads original Shopee and TikTok product-traffic exports.
- Traffic maps by marketplace product/parent-product code, not detail SKU.
- Variant rows cannot duplicate page-level traffic.
- Current-month traffic may be refreshed repeatedly.
- Past months become complete only after Adit confirms finalization; later changes require an audited reopen/re-import.

### Non-marketplace sales

- Entered manually in ERP from the first MVP; no file import.
- Keeps the actual Source name and Source Group `Other`.
- Requires date, unique source invoice/order number, detail SKU, qty, net unit price, and status.
- Uses the same snapshot, validation, movement, and audit rules as committed marketplace transactions.

### Sales profitability metrics

- `GPM` is the rupiah value of gross profit: `Total Net Sales - Total COGS`.
- `GPM Rate = GPM / Total Gross Sales`.
- Total Gross Sales, Total Net Sales, and Total COGS use their immutable transaction snapshots.
- Total Gross Sales is the official Vobia denominator; ERP must not silently substitute Total Net Sales.
- When Total Gross Sales is zero, GPM Rate is blank/not applicable rather than an invented percentage.

## 9. Merchandising projection

### Dashboard indicator definitions

- `Stock Value Ratio = Beginning Gross / Sales Gross`, displayed to two decimal places without a percent sign; this is separate from quantity-based Target Stock Ratio in Projection Builder.
- `ITO YTD = cumulative Sales COGS / average inventory COGS`.
- `GPM = Sales Net - Sales COGS` in rupiah and `GPM Rate = GPM / Sales Gross`.
- `Margin Ratio = Sales Net / Sales COGS`, displayed to two decimal places without a percent sign.
- `Incoming Capital Turnover = Sales Gross / Incoming COGS`, displayed to two decimal places without a percent sign; this is the clarified name for the legacy ROI row.
- A zero denominator renders blank/not applicable.
- Operation/Merchandising Gross is a planning value based on standard Retail Price, while Sales Gross is a transactional value based on immutable Retail Price snapshot and may use the approved net-above-retail special case. Their explained difference is an expected definitional variance; Operation continues to exclude `Retur` and record-level legacy/status exceptions remain visible.
- January–July 2026 is an Accepted Historical Baseline: Operation 75,090 pcs versus Sales excluding `Retur` 75,103 pcs, with an explained 13 pcs variance. Master updates, projections, and ordinary imports cannot rewrite it; any correction requires an approved, audited, versioned restatement.

### Current month

- Uses actual Sales Qty through the latest canonical ERP cutoff; `Retur` is excluded from the Merchandising sales perspective.
- `Beginning Qty M = Ending Qty M-1 + Incoming Qty M`; Incoming month M is assumed sellable from day 1.
- Raw projection is `ROUND(Actual Qty / cutoff day × multiplier based on projection run date, 0)`. The multiplier schedule remains 25, 26, 27, or actual calendar days.
- Final Sales Projection is capped at `MIN(Raw Projection, MAX(Beginning Qty, 0))`. A negative source Beginning remains an auditable exception and is not overwritten.
- Gross, Net, COGS, Ratio, Ending, and MOS are rebuilt consistently from the official current-month Sales Qty; projected Net is rounded to whole rupiah per SKU.
- Merchandising Dashboard Summary and Projection matrix consume the same official projection service. Future months without an official Builder result render blank and are excluded from totals, averages, and YTD-derived indicators.

### Future months

- Workbook September–December values remain immutable reference evidence only. Official future cells stay blank until Projection Builder creates them and Adit approves the result.
- Projection Builder filters: Status Product, Category, and Product.
- Methods: Increase by %, Decrease by %, and Target Stock Ratio.
- Each Product may use a different method and parameter.
- Product rules override Category rules; Category rules override Status Product rules.
- Conflicting rules show a warning before override.
- Percentage methods use the previous month's Final Approved Projection per SKU as a chained baseline.
- The same Product percentage applies to each SKU/size; individual sizes can be manually adjusted before approval.
- Target Stock Ratio computes target Sales Qty per SKU as `Beginning Qty / Target Stock Ratio` and also shows Product total.
- Flow: Configure → Preview per SKU/Product → Adjust → Apply Draft → Approve.
- System recommendation, Adit adjustment, final approved value, rule, scope, and audit history remain separate.
- Status-specific guardrails for Discontinue, Seasonal New, Packaging, and stock availability still apply.

## 10. Incoming Plan and PPIC Requirement

- `Minimum Incoming = MAX(Final Sales Projection - Prior Ending Qty, 0)`.
- With target ratio: `Desired Beginning Qty = Final Sales Projection × Target Stock Ratio`.
- `Recommended Incoming = MAX(Desired Beginning Qty - Prior Ending Qty, 0)`.
- Stock Ratio used for Sales Projection is explicitly separated from Stock Ratio used for Incoming Planning.
- The system warns when a sales target increases merely because excess stock exists.
- Only Final Approved Incoming creates or revises a PPIC Requirement.
- Draft projection and draft incoming never enter PPIC.
- Requirement business key: Need Month + SKU.
- Requirement revision history is retained.
- A requirement already ordered cannot be ordered twice; later approved changes become adjustment requirements.
- MOQ automation remains out until the color-level rule and size allocation are agreed.
- Current-month Incoming supports `Projection`, `Actual`, and `Comparison` views. Actual is derived only from physical Inbound records; comparison preserves projected, actual, variance, and realization rate.
- `Close Month & Actualize Incoming` freezes an immutable monthly actual snapshot without overwriting the original projection. Reopening a closed month requires a reason and audit event.
- A projected-versus-actual shortfall becomes next-month `Carryover PO` only when supported by a still-valid outstanding PO/PO WIP at PO + SKU grain.
- `Total Projected Incoming = New Incoming Plan + Carryover PO`. Carryover is already ordered and cannot create a duplicate PPIC Requirement or PO; uncovered shortfall requires Merchandising review.
- Projection shows the carryover component in the main matrix and a traceable detail table with source month, PO, SKU, outstanding qty, arrival target/ETA, status, received qty, and remaining outstanding.

## 11. PPIC and Purchase Order

- PPIC shows approved requirement, ordered qty, adjustment, and remaining requirement.
- Review PO creates a preview only.
- Create PO happens after validation.
- One PO contains one Supplier and one Need Month, but may contain many Products/SKUs/sizes.
- PO format: `PO-VOB-MM/YY-NNN`; MM/YY comes from Need Month and sequence is unique.
- COGS is snapshotted when the PO is created.
- Manual new-product PO source: `Manual – New Product`; Product/SKU must exist first.
- Draft PO may be edited.
- Released PO cannot be deleted; corrections use amendment and cancellations require a reason.
- PO creation and QC Passed do not add inventory.

## 12. QC and Inbound

- Actual purchase orders created by 31 July 2026 with goods still outstanding are migrated as `PO WIP` from verified PO evidence, never from the workbook test rows.
- Quantities physically received by 31 July are already represented in the 31 July ending-stock opening snapshot and are not reposted as inbound.
- From 1 August 2026 onward, Warehouse manually records every physical receipt against the relevant PO WIP in the Inbound tab; these are operational ERP transactions, not migration records.
- Unreceived PO WIP quantity remains open across month-end and does not become actual incoming until physically received.

- Both support partial records for the same PO + SKU.
- QC records Qty Inspected, Qty Passed, and Qty Failed.
- Passed + Failed cannot exceed Inspected; cumulative Inspected cannot exceed PO Qty.
- Failed qty does not add stock and requires a disposition: Waiting Decision, Rework, Replacement Requested, Rejected, or Accepted with Exception.
- Stock/cost treatment occurs only after a valid disposition and remains audited.
- Cumulative Received Qty cannot exceed cumulative Qty QC Passed.
- Every valid physical inbound creates an Incoming movement and FIFO layer using the PO COGS snapshot.

## 13. Inventory Movement and FIFO

- Available inventory is derived from the immutable movement ledger; balances are not edited directly.
- Movement types: Opening, Incoming, Sales Out, Return In, Adjustment In, and Adjustment Out.
- Every adjustment requires reason, evidence/reference, timestamp, and actor.
- FIFO cutover snapshot is the end-of-day inventory balance on 31 July 2026. That signed ending balance becomes the opening layer available on 1 August 2026.
- August incoming is excluded from the opening migration and is recorded manually by Warehouse only when physically received.
- Opening uses frozen COGS, inbound uses PO snapshot COGS, sales consumes oldest available layers, and return restores the originating sale layers.
- Master COGS changes never rewrite historical layers or cost allocation.
- Actual sales may commit even when stock/layers are insufficient.
- The system does not invent cost or clamp stock to zero; it creates Negative Stock/FIFO Short exceptions.
- Exceptions close only through traceable source correction or approved adjustment.

## 14. Return Management

- Existing `Return Log` rows in the Vobia MD 2026 workbook are UAT/test data and are not migrated as canonical physical returns.

- Marketplace return status creates an Expected Return but does not add stock.
- Physical Return Receipt records receipt date, Source, order number, SKU, qty, warehouse, condition, and notes.
- Partial physical returns are allowed; cumulative return cannot exceed original Sales Out without approved adjustment.
- Conditions: Sellable, Damaged, Defective, Missing/Lost, Wrong Item, and Waiting Inspection.
- Only Sellable creates Return In and restores qty/cost to the originating FIFO layer.
- Other conditions remain out of available inventory until resolved through a valid disposition.

## 15. PO Aging and closure

- PO Open Date is Created At.
- PO stays open while it has outstanding inbound, remaining FIFO stock, restored return stock, or related stock/FIFO exceptions.
- PO closes automatically only after all PO qty is inbound, all PO stock is depleted, and no return/exception remains open.
- User cannot force close past these conditions.
- A sellable return to a closed PO reopens it automatically.
- `Outstanding Inbound = MAX(PO Qty - Received Qty, 0)`.
- `PO Remaining Qty = Outstanding Inbound + Remaining FIFO Layer Qty`.
- Aging: 0–60 On Target, 61–90 Attention, >90 Over Target; late closure is Closed Late and conflicts are Exception.

## 16. Non-negotiable controls

- Historical transaction and cost snapshots are immutable.
- Every stock movement is traceable and auditable.
- Planning never becomes actual stock.
- Negative stock and cost exceptions remain visible.
- Live workbook edits are outside the ERP build unless separately authorized.
- The historical `2026 Projection Vobia` workbook remains read-only and cannot be opened without explicit authorization.

## 17. Open discovery decisions

- Supplier and warehouse master fields and initial records.
- MOQ per color and size-allocation rules.
- Required Arrival/lead-time rules.
- Detailed treatment of each QC-failed disposition.
- Detailed treatment/cost accounting for non-sellable returns.
- Hosting provider, backup retention, and recovery objectives.

## 18. Reconciliation and cutover acceptance

- Transaction key, SKU, qty, movement key, inventory qty per SKU, PO Qty, QC Passed, and Received Qty must reconcile exactly.
- Money uses identical definitions and rounding; the only tolerance is Rp1 for an explainable rounding difference.
- Reconciliation reports both record-level and total differences; offsetting record errors cannot be hidden by a matching grand total.
- At least seven consecutive daily parallel-run cycles must pass before cutover approval.
- Cutover is blocked by duplicates, missing movements, unexplained stock differences, or unresolved critical exceptions.
- Google Sheets remains a fallback during post-cutover stabilization until Adit separately approves retirement.
