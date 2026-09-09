MODULES = (
    ("sales", "Sales"),
    ("operation", "Operation"),
    ("rnd", "RnD"),
    ("marketing", "Marketing"),
    ("master_data", "Master Data"),
    ("reconciliation", "Reconciliation"),
    ("guide", "Panduan & UAT"),
)


MODULE_TABS = {
    "sales": (
        ("dashboard", "Dashboard", "Sales", ("sales:dashboard",)),
        (
            "planning",
            "Sales Planning",
            "Sales",
            ("sales:planning_builder", "sales:planning_filter_options"),
        ),
        ("product_performance", "Product Performance", "Sales", ("sales:product_performance",)),
        ("pareto", "Pareto Analysis", "Sales", ("sales:pareto",)),
        ("transactions", "Transaction", "Sales", ("sales:transactions",)),
        ("input_transaction", "Input Transaction", "Sales", ("sales:input_transaction",)),
        (
            "data_import",
            "Data Import",
            "Sales",
            (
                "imports:sales_list",
                "imports:sales_upload",
                "imports:sales_detail",
                "imports:sales_approve",
                "traffic:overview",
                "traffic:detail",
                "traffic:approve",
                "traffic:complete",
                "traffic:reopen",
            ),
        ),
    ),
    "operation": (
        (
            "merchandising_dashboard",
            "Dashboard",
            "Merchandising",
            ("merchandising:overview", "merchandising:dashboard"),
        ),
        ("merchandising_projection", "Projection", "Merchandising", ("merchandising:projection",)),
        (
            "merchandising_planning",
            "Planning Builder",
            "Merchandising",
            (
                "merchandising:planning_builder",
                "merchandising:planning_filter_options",
                "merchandising:edit_scenario",
                "merchandising:delete_scenario",
                "merchandising:update_scenario_draft",
                "merchandising:revise_scenario",
                "merchandising:delete_scenario_draft_items",
                "merchandising:approve_projection",
                "merchandising:make_incoming",
                "merchandising:approve_incoming",
                "merchandising:close_incoming",
            ),
        ),
        ("ppic_requirement", "Requirement", "PPIC", ("purchasing:requirements",)),
        ("ppic_generator", "PO Generator", "PPIC", ("purchasing:overview", "purchasing:generator")),
        (
            "purchase_order",
            "Purchase Order",
            "PPIC",
            (
                "purchasing:purchase_orders",
                "purchasing:tracking",
                "purchasing:po_wip_list",
                "purchasing:po_wip_upload",
                "purchasing:po_wip_detail",
                "purchasing:po_wip_approve",
                "purchasing:po_detail",
                "purchasing:po_release",
                "purchasing:po_cancel",
                "purchasing:po_revise_vendor",
                "purchasing:po_delete_draft",
                "purchasing:po_print",
            ),
        ),
        ("vendors", "Daftar Vendor", "PPIC", ("purchasing:vendors", "purchasing:vendor_delete")),
        ("production_dashboard", "Dashboard", "Production", ("production:dashboard", "inventory:production")),
        ("production_plan", "Production Plan", "Production", ("production:planning",)),
        (
            "production_monitoring",
            "Production Monitoring",
            "Production",
            ("production:monitoring", "production:detail", "production:approve_cogs_finalization"),
        ),
        (
            "production_activity",
            "Production Activity",
            "Production",
            (
                "production:activity",
                "production:activity_correction",
                "production:delivery_order_preview",
                "production:trial_approval",
                "production:quality_control",
            ),
        ),
        ("rejected_goods", "Rejected Goods", "Production", ("production:rejected_goods", "production:qc_follow_up")),
        (
            "inventory_summary",
            "Inventory Summary",
            "Warehouse",
            (
                "inventory:overview",
                "inventory:opening_list",
                "inventory:opening_upload",
                "inventory:opening_detail",
                "inventory:opening_approve",
            ),
        ),
        ("inventory_turnover", "Inventory Turnover", "Warehouse", ("inventory:turnover",)),
        ("inbound", "Inbound", "Warehouse", ("inventory:inbound",)),
        ("return_log", "Return Log", "Warehouse", ("inventory:return_log",)),
        ("outbound", "Outbound", "Warehouse", ("inventory:outbound",)),
    ),
    "rnd": (
        (
            "designing",
            "Designing",
            "RnD",
            (
                "rnd:designing",
                "rnd:design_detail",
                "rnd:design_file",
                "rnd:design_recommend",
                "rnd:design_unrecommend",
                "rnd:design_delete",
            ),
        ),
        (
            "collections",
            "Collection R&D",
            "RnD",
            (
                "rnd:dashboard",
                "rnd:collection_create",
                "rnd:collection_detail",
                "rnd:collection_delete",
                "rnd:collection_start_development",
                "rnd:collection_handover",
                "rnd:collection_marketing_preview",
                "rnd:product_detail",
                "rnd:product_submit",
                "rnd:product_approve",
                "rnd:product_reject",
                "rnd:product_delete",
                "rnd:product_request_revision",
                "rnd:product_revision_file",
                "rnd:product_file",
            ),
        ),
        (
            "development",
            "Development",
            "RnD",
            (
                "rnd:development_list",
                "rnd:development_detail",
                "rnd:product_development_transition",
                "rnd:product_detail",
                "rnd:product_revision_file",
                "rnd:product_file",
            ),
        ),
    ),
    "marketing": (
        (
            "dashboard",
            "Dashboard",
            "Marketing",
            (
                "dashboard:instagram_dashboard",
                "dashboard:instagram_connection",
                "dashboard:tiktok_connection",
                "dashboard:tiktok_oauth_start",
                "dashboard:tiktok_callback",
                "dashboard:tiktok_business_oauth_start",
                "dashboard:tiktok_business_callback",
            ),
        ),
        (
            "campaigns",
            "Campaign/Project",
            "Marketing",
            (
                "dashboard:campaign_list",
                "dashboard:campaign_create",
                "dashboard:campaign_edit",
                "dashboard:campaign_delete",
                "dashboard:campaign_cover",
                "dashboard:campaign_detail",
            ),
        ),
        (
            "partnerships",
            "Partnership",
            "Marketing",
            (
                "dashboard:partnership_list",
                "dashboard:partnership_create",
                "dashboard:partnership_edit",
                "dashboard:partnership_delete",
                "dashboard:partnership_detail",
            ),
        ),
        (
            "upcoming_collections",
            "Upcoming Collection",
            "Marketing",
            (
                "dashboard:upcoming_collection_list",
                "dashboard:upcoming_collection_detail",
                "dashboard:upcoming_collection_recommend",
                "dashboard:upcoming_collection_product_file",
                "dashboard:upcoming_collection_official_approve",
                "dashboard:upcoming_collection_commercial_approve",
            ),
        ),
    ),
    "master_data": (
        (
            "master_data",
            "Master Data",
            "System",
            (
                "master_data:overview",
                "master_data:export_bank_data",
                "imports:master_list",
                "imports:master_upload",
                "imports:master_detail",
                "imports:master_approve",
                "imports:master_cancel",
            ),
        ),
    ),
    "reconciliation": (
        ("reconciliation", "Reconciliation", "System", ("reconciliation:overview", "reconciliation:detail")),
    ),
    "guide": (("guide", "Panduan & UAT", "System", ("dashboard:guide",)),),
}


TAB_ENTRY_ROUTES = {
    "sales": {
        "dashboard": "sales:dashboard",
        "planning": "sales:planning_builder",
        "product_performance": "sales:product_performance",
        "pareto": "sales:pareto",
        "transactions": "sales:transactions",
        "input_transaction": "sales:input_transaction",
        "data_import": "imports:sales_list",
    },
    "operation": {
        "merchandising_dashboard": "merchandising:overview",
        "merchandising_projection": "merchandising:projection",
        "merchandising_planning": "merchandising:planning_builder",
        "ppic_requirement": "purchasing:requirements",
        "ppic_generator": "purchasing:generator",
        "purchase_order": "purchasing:purchase_orders",
        "vendors": "purchasing:vendors",
        "production_dashboard": "production:dashboard",
        "production_plan": "production:planning",
        "production_monitoring": "production:monitoring",
        "production_activity": "production:activity",
        "rejected_goods": "production:rejected_goods",
        "inventory_summary": "inventory:overview",
        "inventory_turnover": "inventory:turnover",
        "inbound": "inventory:inbound",
        "return_log": "inventory:return_log",
        "outbound": "inventory:outbound",
    },
    "rnd": {
        "designing": "rnd:designing",
        "collections": "rnd:dashboard",
        "development": "rnd:development_list",
    },
    "marketing": {
        "dashboard": "dashboard:instagram_dashboard",
        "campaigns": "dashboard:campaign_list",
        "partnerships": "dashboard:partnership_list",
        "upcoming_collections": "dashboard:upcoming_collection_list",
    },
    "master_data": {"master_data": "master_data:overview"},
    "reconciliation": {"reconciliation": "reconciliation:overview"},
    "guide": {"guide": "dashboard:guide"},
}


VIEW_TABS = {}
for module, tabs in MODULE_TABS.items():
    for tab_key, _label, _section, view_names in tabs:
        for view_name in view_names:
            route_module, tab_keys = VIEW_TABS.setdefault(view_name, (module, []))
            if route_module != module:
                raise ValueError(f"Route {view_name} terdaftar di dua modul berbeda.")
            tab_keys.append(tab_key)


def module_level(user, module):
    default_level = "none" if module == "rnd" else "approve"
    return (getattr(user, "module_access", {}) or {}).get(module, default_level)


def allowed_tab_keys(user, module):
    available = [tab[0] for tab in MODULE_TABS.get(module, ())]
    if getattr(user, "is_superuser", False):
        return available
    configured = getattr(user, "tab_access", {}) or {}
    if module not in configured:
        return available
    selected = set(configured.get(module) or ())
    return [key for key in available if key in selected]


def can_access_tab(user, module, tab_key):
    return getattr(user, "is_superuser", False) or (
        module_level(user, module) != "none" and tab_key in allowed_tab_keys(user, module)
    )


def first_allowed_route(user, module):
    allowed = allowed_tab_keys(user, module)
    default_tabs = {
        "sales": "dashboard",
        "operation": "merchandising_dashboard",
        "rnd": "collections",
        "marketing": "dashboard",
    }
    default_tab = default_tabs.get(module)
    if default_tab in allowed:
        return TAB_ENTRY_ROUTES[module][default_tab]
    for tab_key in allowed:
        return TAB_ENTRY_ROUTES[module][tab_key]
    return None
