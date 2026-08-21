# VOBIA ERP — UAT Guide for Adit

Use the in-app **Panduan & UAT** page as the short operating guide. This document is the evidence checklist.

## Scope

Acceptance is performed locally and does not change Google Sheets. Use representative exports and a controlled set of 1–3 SKUs first. Google Sheets remains the operational source throughout UAT.

## Required evidence

- Canonical Bank Data export `.xlsx/.csv`.
- Opening Qty and Frozen Unit COGS evidence as of 1 August 2026 for the chosen SKU(s).
- One actual supplier and one actual warehouse.
- Representative Shopee and TikTok Sales exports.
- Representative Shopee and TikTok product Traffic exports.
- One controlled Incoming/PO/QC/Inbound scenario.
- At least one marketplace Retur scenario or a clearly labelled controlled test record.

## Acceptance script

1. Import Master Product and confirm duplicate file, duplicate SKU, unknown mapping, and unsafe numeric ID controls.
2. Post FIFO Opening for controlled SKUs; confirm frozen cost and no editable balance field.
3. Inspect the Sales Import Requirement table; export the periods it requests.
4. Upload Sales files, review staged changes, approve, and confirm Source + Order + SKU uniqueness.
5. Confirm pending status stays in the next import requirement; Selesai/Retur becomes final.
6. Enter one manual non-marketplace transaction; confirm actual Source and immutable snapshots.
7. Upload Traffic; confirm product mapping and that duplicate product/variation code blocks commit.
8. Build a current/future projection, preview, apply, manually approve whole-unit final qty.
9. Create and approve Incoming; confirm PPIC Requirement appears automatically.
10. Build a multi-line draft PO from requirements, review, release, and confirm number/COGS snapshot.
11. Record partial QC and prove an over-QC attempt is rejected.
12. Record partial Inbound and prove an over-inbound attempt is rejected; confirm actual movement/FIFO layer.
13. Commit Sales that consumes stock; confirm oldest FIFO layer is used and shortage remains a visible exception.
14. Record physical return conditions; prove only Sellable restores stock/cost.
15. Refresh PO Aging; prove close requires zero outstanding and zero PO-layer stock, and Sellable return can reopen.
16. Run Reconciliation; review every record-level issue.

## Pass criteria

- Transaction, movement, and layer business keys have no duplicates.
- SKU and quantities reconcile exactly per record.
- PO Qty, QC Passed, and Received Qty reconcile exactly.
- Inventory quantity per SKU matches the approved comparison source exactly.
- Money uses the same definition/rounding; only an explained rounding difference up to Rp1 is permitted.
- No missing movement, unexplained stock difference, unresolved critical reconciliation issue, or hidden FIFO exception.
- Adit can complete the operating sequence without terminal/admin/database access.

## Cutover gate

One UAT pass does not authorize cutover. Run the same controls for at least seven consecutive daily parallel cycles. Production deployment and stopping the Sheets fallback require separate approval.
