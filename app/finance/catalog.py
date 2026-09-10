FINANCE_NAV_SECTIONS = (
    {"key": "company", "label": "Company", "items": (
        ("company", "Company", "company"),
        ("recurring", "Recurring", "recurring"),
        ("period_end", "Period End", "period-end"),
        ("tax", "Tax", "tax"),
    )},
    {"key": "general-ledger", "label": "General Ledger", "items": (
        ("accounts", "Chart of Accounts", None),
        ("journals", "Jurnal Voucher", None),
    )},
    {"key": "cash-bank", "label": "Cash & Bank", "items": (
        ("other_payment", "Other Payment", "other-payment"),
        ("other_deposit", "Other Deposit", "other-deposit"),
        ("bank_transfer", "Bank Transfer", "bank-transfer"),
    )},
    {"key": "sales", "label": "Sales", "items": (
        ("sales_invoice", "Sales Invoice", "sales-invoice"),
        ("sales_return", "Sales Return", "sales-return"),
        ("sales_receipt", "Sales Receipt", "sales-receipt"),
        ("customers", "Master Customer", "customers"),
        ("ecommerce", "Smart Link E-commerce", "smart-link-ecommerce"),
    )},
    {"key": "purchase", "label": "Purchase", "items": (
        ("purchase_invoice", "Purchase Invoice", "purchase-invoice"),
        ("purchase_payment", "Purchase Payment", "purchase-payment"),
        ("suppliers", "Master Supplier", "suppliers"),
        ("purchase_return", "Purchase Return", "purchase-return"),
    )},
    {"key": "inventory", "label": "Inventory", "items": (
        ("items", "Master Barang/Jasa", "items-services"),
        ("warehouses", "Warehouse", "warehouses"),
        ("item_adjustment", "Item Adjustment", "item-adjustment"),
    )},
    {"key": "fixed-asset", "label": "Fixed Asset", "items": (
        ("fixed_assets", "Fixed Asset", "fixed-assets"),
        ("asset_purchase", "Purchase Asset", "purchase-asset"),
    )},
    {"key": "report-financial", "label": "Report · Financial", "items": (
        ("profit_loss", "Profit & Loss", None),
        ("balance_sheet", "Balance Sheet", None),
        ("cash_flow", "Cash Flow · Direct", "cash-flow"),
        ("financial_ratio", "Financial Ratio", "financial-ratio"),
    )},
    {"key": "report-gl", "label": "Report · General Ledger", "items": (
        ("gl_summary", "General Ledger Summary", "general-ledger-summary"),
        ("trial_balance", "Trial Balance", None),
    )},
    {"key": "report-receivable", "label": "Report · Receivable", "items": (
        ("outstanding_invoice", "Outstanding Invoice", "outstanding-invoice"),
        ("aging_receivable", "Aging Receivable", "aging-receivable"),
    )},
    {"key": "report-sales", "label": "Report · Sales", "items": (
        ("sales_invoice_report", "Sales Invoice List", "sales-invoice-list"),
        ("sales_return_report", "Sales Return per Item", "sales-return-per-item"),
    )},
    {"key": "report-payable", "label": "Report · Payable", "items": (
        ("outstanding_purchase", "Outstanding Purchase Invoice", "outstanding-purchase-invoice"),
        ("ap_aging", "Account Payable Aging", "account-payable-aging"),
        ("ap_aging_detail", "Account Payable Aging Detail", "account-payable-aging-detail"),
        ("purchase_invoice_report", "Purchase Invoice List", "purchase-invoice-list"),
        ("supplier_payable_month", "Supplier Payable per Month", "supplier-payable-per-month"),
    )},
    {"key": "report-inventory", "label": "Report · Inventory", "items": (
        ("inventory_valuation", "Valuation Inventory", "inventory-valuation"),
        ("inventory_aging", "Aging Inventory", "inventory-aging"),
    )},
)

ROUTES = {
    "accounts": "finance:accounts",
    "journals": "finance:journals",
    "profit_loss": "finance:profit_loss",
    "balance_sheet": "finance:balance_sheet",
    "trial_balance": "finance:trial_balance",
}

FEATURES = {
    slug: {
        "tab": tab,
        "title": label,
        "section": section["label"],
        "workflow": slug
        if tab
        in {
            "other_payment",
            "other_deposit",
            "bank_transfer",
            "sales_invoice",
            "sales_return",
            "sales_receipt",
            "purchase_invoice",
            "purchase_payment",
            "purchase_return",
            "asset_purchase",
        }
        else "",
    }
    for section in FINANCE_NAV_SECTIONS
    for tab, label, slug in section["items"]
    if slug
}
