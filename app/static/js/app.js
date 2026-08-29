document.addEventListener('DOMContentLoaded', () => {
  const scrollRestoreKey = 'vobia:auto-filter-scroll';
  const saveFilterScroll = destination => {
    try {
      const target = new URL(destination || window.location.href, window.location.href);
      window.sessionStorage.setItem(scrollRestoreKey, JSON.stringify({
        pathname: target.pathname,
        x: window.scrollX,
        y: window.scrollY,
        savedAt: Date.now(),
      }));
    } catch (error) {
      console.warn('Filter scroll position could not be saved.', error);
    }
  };
  const restoreFilterScroll = () => {
    try {
      const savedValue = window.sessionStorage.getItem(scrollRestoreKey);
      if (!savedValue) return;
      window.sessionStorage.removeItem(scrollRestoreKey);
      const saved = JSON.parse(savedValue);
      const isFresh = Date.now() - Number(saved.savedAt || 0) < 60000;
      if (saved.pathname !== window.location.pathname || !isFresh) return;
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
          window.scrollTo(Number(saved.x) || 0, Number(saved.y) || 0);
        });
      });
    } catch (error) {
      try {
        window.sessionStorage.removeItem(scrollRestoreKey);
      } catch (cleanupError) {
        console.warn('Stale filter scroll position could not be cleared.', cleanupError);
      }
      console.warn('Filter scroll position could not be restored.', error);
    }
  };
  restoreFilterScroll();

  document.querySelectorAll('[data-nav-group-toggle]').forEach(toggle => {
    const groupName = toggle.dataset.navGroupToggle;
    const panel = document.querySelector(`[data-nav-group-panel="${groupName}"]`);
    if (!panel) return;

    const storageKey = `vobia-operation-nav:${groupName}`;
    const defaultOpen = toggle.dataset.navGroupActive === 'true';
    let isOpen = defaultOpen;
    try {
      const savedState = window.localStorage.getItem(storageKey);
      if (savedState !== null) isOpen = savedState === 'open';
    } catch (error) {
      console.warn('Sidebar state could not be read.', error);
    }

    const setOpen = (open, persist = true) => {
      isOpen = open;
      panel.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      if (!persist) return;
      try {
        window.localStorage.setItem(storageKey, open ? 'open' : 'closed');
      } catch (error) {
        console.warn('Sidebar state could not be saved.', error);
      }
    };

    toggle.addEventListener('click', () => setOpen(!isOpen));
    setOpen(isOpen);
  });

  document.querySelectorAll('form[method="post"], form[data-single-submit]').forEach(form => {
    form.addEventListener('submit', event => {
      if (form.dataset.submitting === 'true') {
        event.preventDefault();
        return;
      }
      if (event.defaultPrevented) return;

      const submitter = event.submitter;
      if (submitter?.name) {
        const submitterMirror = document.createElement('input');
        submitterMirror.type = 'hidden';
        submitterMirror.name = submitter.name;
        submitterMirror.value = submitter.value;
        submitterMirror.dataset.submitterMirror = 'true';
        form.appendChild(submitterMirror);
      }
      form.dataset.submitting = 'true';
      form.setAttribute('aria-busy', 'true');
      form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach(button => {
        button.disabled = true;
        button.classList.add('is-loading');
        button.setAttribute('aria-disabled', 'true');
        const loadingLabel = button.dataset.loadingLabel || 'Memproses...';
        if (button.tagName === 'INPUT') {
          button.value = loadingLabel;
        } else {
          button.textContent = loadingLabel;
        }
      });
    });
  });

  document.querySelectorAll('form[data-preserve-scroll]').forEach(form => {
    form.addEventListener('submit', event => {
      if (event.defaultPrevented) return;
      saveFilterScroll(form.action || window.location.href);
    });
  });

  document.querySelectorAll('form[data-edit-gated]').forEach(form => {
    const toggle = document.querySelector(`[data-edit-toggle="${form.id}"]`);
    const cancel = document.querySelector(`[data-edit-cancel="${form.id}"]`);
    const controls = [...form.querySelectorAll('input:not([type="hidden"]), select, textarea')];
    if (!toggle || !cancel || !controls.length) return;
    const savedValues = new Map(controls.map(control => [control, control.value]));
    controls.forEach(control => { control.readOnly = true; });
    toggle.addEventListener('click', () => {
      controls.forEach(control => { control.readOnly = false; });
      form.classList.add('is-editing');
      toggle.disabled = true;
      toggle.setAttribute('aria-pressed', 'true');
      cancel.hidden = false;
      controls[0].focus();
    });
    cancel.addEventListener('click', () => {
      controls.forEach(control => {
        control.value = savedValues.get(control);
        control.readOnly = true;
        control.dispatchEvent(new Event('input', { bubbles: true }));
      });
      form.classList.remove('is-editing');
      toggle.disabled = false;
      toggle.setAttribute('aria-pressed', 'false');
      cancel.hidden = true;
      toggle.focus();
    });
  });

  document.querySelectorAll('form[data-dirty-submit]').forEach(form => {
    const submitButtons = [...document.querySelectorAll('[data-dirty-submit-button]')]
      .filter(button => button.form === form);
    const controls = [...form.querySelectorAll('input, select, textarea')].filter(control => (
      !['hidden', 'submit', 'button'].includes(control.type)
      && control.form === form
      && !control.disabled
      && !control.hasAttribute('data-dirty-ignore')
    ));
    if (!submitButtons.length || !controls.length) return;

    const controlValue = control => (
      ['checkbox', 'radio'].includes(control.type) ? String(control.checked) : control.value
    );
    const savedValues = new Map(controls.map(control => [control, controlValue(control)]));
    const syncDirtyState = () => {
      const hasChanges = controls.some(control => controlValue(control) !== savedValues.get(control));
      submitButtons.forEach(button => {
        button.disabled = !hasChanges;
        button.setAttribute('aria-disabled', String(!hasChanges));
      });
    };

    controls.forEach(control => {
      control.addEventListener('input', syncDirtyState);
      control.addEventListener('change', syncDirtyState);
    });
    form.addEventListener('reset', () => window.requestAnimationFrame(syncDirtyState));
    syncDirtyState();
  });

  document.querySelectorAll('form[data-trial-submit-gate]').forEach(form => {
    const trialDate = form.querySelector('input[name="trial_date"]');
    const submitButton = form.querySelector('[data-trial-submit-button]');
    if (!trialDate || !submitButton) return;

    const syncTrialApprovalGate = () => {
      const canSubmit = Boolean(trialDate.value);
      submitButton.disabled = !canSubmit;
      submitButton.setAttribute('aria-disabled', String(!canSubmit));
    };

    trialDate.addEventListener('input', syncTrialApprovalGate);
    trialDate.addEventListener('change', syncTrialApprovalGate);
    syncTrialApprovalGate();
  });

  document.querySelectorAll('form[data-auto-submit]').forEach(form => {
    const submit = () => {
      if (form.dataset.submitting === 'true') return;
      saveFilterScroll(form.action);
      form.dataset.submitting = 'true';
      form.requestSubmit();
    };
    form.querySelectorAll('input[type="date"], input[type="month"], select').forEach(control => {
      control.addEventListener('change', submit);
    });
    form.querySelectorAll('[data-chip-filter]').forEach(control => {
      control.addEventListener('change', submit);
    });
    form.querySelectorAll('[data-auto-search]').forEach(control => {
      let searchTimer;
      control.addEventListener('input', () => {
        window.clearTimeout(searchTimer);
        if (control.list && ![...control.list.options].some(option => option.value === control.value)) return;
        searchTimer = window.setTimeout(submit, 500);
      });
    });
    form.addEventListener('filters:auto-submit', submit);
    form.querySelectorAll('a[href]').forEach(link => {
      link.addEventListener('click', () => saveFilterScroll(link.href));
    });
  });

  document.querySelectorAll('[data-scenario-edit-form]').forEach(form => {
    const submitButton = form.querySelector('[data-scenario-edit-submit]');
    const controls = [...form.querySelectorAll(
      'input[name="name"], input[name="start_month"], input[name="end_month"]',
    )];
    if (!submitButton || !controls.length) return;

    const savedValues = new Map(controls.map(control => [control.name, control.value]));
    const syncEditState = () => {
      const hasChanges = controls.some(
        control => control.value !== savedValues.get(control.name),
      );
      submitButton.disabled = !hasChanges;
      submitButton.setAttribute('aria-disabled', String(!hasChanges));
    };

    controls.forEach(control => {
      control.addEventListener('input', syncEditState);
      control.addEventListener('change', syncEditState);
    });
    form.addEventListener('reset', () => window.requestAnimationFrame(syncEditState));
    syncEditState();
  });

  document.querySelectorAll('form[data-source-cascade]').forEach(form => {
    const groupRoot = form.querySelector('input[name="source_group"]')?.closest('[data-multi-select]');
    const sourceRoot = form.querySelector('[data-cascading-source-filter]');
    if (!groupRoot || !sourceRoot) return;

    const groupChecks = [...groupRoot.querySelectorAll('input[name="source_group"]')];
    const sourceOptions = [...sourceRoot.querySelectorAll('.multi-select-option[data-source-group]')];
    const sourceSummary = sourceRoot.querySelector('[data-filter-summary]');
    const sourceResultCount = sourceRoot.querySelector('[data-filter-result-count]');
    const syncSources = () => {
      const selectedGroups = new Set(groupChecks.filter(check => check.checked).map(check => check.value));
      let available = 0;
      sourceOptions.forEach(option => {
        const allowed = selectedGroups.size === 0 || selectedGroups.has(option.dataset.sourceGroup);
        option.dataset.cascadeHidden = allowed ? 'false' : 'true';
        option.hidden = !allowed;
        const check = option.querySelector('input[type="checkbox"]');
        if (!allowed) check.checked = false;
        if (allowed) available += 1;
      });
      const selectedSources = sourceOptions
        .filter(option => option.querySelector('input[type="checkbox"]').checked);
      sourceSummary.textContent = selectedSources.length === 0
        ? sourceRoot.dataset.allLabel
        : (selectedSources.length === 1
          ? selectedSources[0].querySelector('span').textContent.trim()
          : `${selectedSources.length} selected`);
      sourceResultCount.textContent = `${available} pilihan`;
    };
    groupChecks.forEach(check => check.addEventListener('change', syncSources));
    groupRoot.querySelector('[data-filter-clear]')?.addEventListener('click', syncSources);
    syncSources();
  });

  document.querySelectorAll('[data-planning-builder-form]').forEach(form => {
    const statusSelect = form.querySelector('#id_product_status');
    const categorySelect = form.querySelector('#id_category');
    const subcategorySelect = form.querySelector('#id_subcategory');
    const activitySelect = form.querySelector('#id_planning_activity');
    const targetMonthSelect = form.querySelector('#id_target_month');
    const productSource = form.querySelector('#id_product');
    const productRoot = form.querySelector('[data-builder-product-multi]');
    const productTrigger = productRoot?.querySelector('.multi-select-trigger');
    const productMenu = productRoot?.querySelector('[data-product-menu]');
    const productSearch = productRoot?.querySelector('[data-product-search]');
    const productOptions = productRoot?.querySelector('[data-product-options]');
    const productSummary = productRoot?.querySelector('[data-product-summary]');
    const productResultCount = productRoot?.querySelector('[data-product-result-count]');
    const endpoint = form.dataset.filterOptionsUrl;
    if (!statusSelect || !categorySelect || !subcategorySelect || !activitySelect || !targetMonthSelect || !productSource || !productRoot || !endpoint) return;

    const replaceOptions = (select, rows, emptyLabel, preferredValue) => {
      select.replaceChildren(new Option(emptyLabel, ''));
      rows.forEach(row => select.add(new Option(row.name, row.id)));
      if (preferredValue && rows.some(row => row.id === preferredValue)) {
        select.value = preferredValue;
      } else {
        select.value = '';
      }
    };

    const selectedProductValues = () => new Set(
      [...productSource.options].filter(option => option.selected).map(option => option.value)
    );

    const updateProductSummary = () => {
      const selected = [...productSource.options].filter(option => option.selected);
      productSummary.textContent = selected.length === 0
        ? productRoot.dataset.allLabel
        : (selected.length === 1 ? selected[0].textContent : `${selected.length} products selected`);
    };

    const filterProductOptions = () => {
      const keyword = productSearch.value.trim().toLocaleLowerCase('id');
      let visible = 0;
      productOptions.querySelectorAll('.multi-select-option').forEach(option => {
        const match = option.textContent.toLocaleLowerCase('id').includes(keyword);
        option.hidden = !match;
        if (match) visible += 1;
      });
      productResultCount.textContent = `${visible} pilihan`;
    };

    const renderProductOptions = () => {
      productOptions.replaceChildren();
      [...productSource.options].forEach(sourceOption => {
        if (!sourceOption.value) return;
        const label = document.createElement('label');
        label.className = 'multi-select-option';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = sourceOption.value;
        checkbox.checked = sourceOption.selected;
        const text = document.createElement('span');
        text.textContent = sourceOption.textContent;
        checkbox.addEventListener('change', () => {
          sourceOption.selected = checkbox.checked;
          updateProductSummary();
        });
        label.append(checkbox, text);
        productOptions.append(label);
      });
      updateProductSummary();
      filterProductOptions();
    };

    const replaceProductOptions = (rows, preferredValues) => {
      productSource.replaceChildren();
      rows.forEach(row => productSource.add(new Option(row.name, row.id, false, preferredValues.has(row.id))));
      renderProductOptions();
    };

    const refreshOptions = async ({ preserveCategory = false, preserveSubcategory = false } = {}) => {
      const previousCategory = preserveCategory ? categorySelect.value : '';
      const previousSubcategory = preserveSubcategory ? subcategorySelect.value : '';
      const previousProducts = selectedProductValues();
      const params = new URLSearchParams();
      if (statusSelect.value) params.set('product_status', statusSelect.value);
      if (previousCategory) params.set('category', previousCategory);
      if (previousSubcategory) params.set('subcategory', previousSubcategory);
      params.set('planning_activity', activitySelect.value || 'ACTIVE');
      if (targetMonthSelect.value) params.set('target_month', targetMonthSelect.value);
      try {
        const response = await fetch(`${endpoint}?${params.toString()}`, {
          headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        replaceOptions(categorySelect, data.categories, 'All categories', previousCategory);
        replaceOptions(subcategorySelect, data.subcategories, 'All sub categories', previousSubcategory);
        replaceProductOptions(data.products, previousProducts);
      } catch (error) {
        console.error('Planning Builder filter options could not be loaded.', error);
      }
    };

    statusSelect.addEventListener('change', () => refreshOptions());
    categorySelect.addEventListener('change', () => refreshOptions({ preserveCategory: true }));
    subcategorySelect.addEventListener('change', () => refreshOptions({ preserveCategory: true, preserveSubcategory: true }));
    activitySelect.addEventListener('change', () => refreshOptions({ preserveCategory: true, preserveSubcategory: true }));
    targetMonthSelect.addEventListener('change', () => refreshOptions({ preserveCategory: true, preserveSubcategory: true }));
    productTrigger.addEventListener('click', () => {
      productMenu.hidden = !productMenu.hidden;
      productTrigger.setAttribute('aria-expanded', String(!productMenu.hidden));
      if (!productMenu.hidden) {
        productSearch.focus();
        filterProductOptions();
      }
    });
    productSearch.addEventListener('input', filterProductOptions);
    productRoot.querySelector('[data-product-clear]').addEventListener('click', () => {
      [...productSource.options].forEach(option => { option.selected = false; });
      renderProductOptions();
    });
    document.addEventListener('click', event => {
      if (!productRoot.contains(event.target)) {
        productMenu.hidden = true;
        productTrigger.setAttribute('aria-expanded', 'false');
      }
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        productMenu.hidden = true;
        productTrigger.setAttribute('aria-expanded', 'false');
      }
    });
    refreshOptions({ preserveCategory: true, preserveSubcategory: true });

    const methodSelect = form.querySelector('#id_method');
    const parameterInput = form.querySelector('#id_parameter');
    const syncMethodParameter = () => {
      if (!methodSelect || !parameterInput) return;
      const sameAsLastMonth = methodSelect.value === 'SAME_AS_LAST_MONTH';
      const sellOutMonths = methodSelect.value === 'SELL_OUT_ENDING_MONTHS';
      parameterInput.disabled = sameAsLastMonth;
      parameterInput.required = !sameAsLastMonth;
      parameterInput.min = sellOutMonths ? '1' : '0';
      parameterInput.step = sellOutMonths ? '1' : '0.0001';
      parameterInput.placeholder = sameAsLastMonth
        ? 'Tidak diperlukan'
        : (sellOutMonths ? 'Contoh: 2 bulan' : 'Isi parameter');
      if (sameAsLastMonth) parameterInput.value = '';
    };
    methodSelect?.addEventListener('change', syncMethodParameter);
    syncMethodParameter();
  });

  document.querySelectorAll('[data-projection-preview-row]').forEach(row => {
    const input = row.querySelector('[data-sales-projection-input]');
    const incomingInput = row.querySelector('[data-incoming-recommendation-input]');
    const beginningCell = row.querySelector('[data-beginning-cell]');
    const growthCell = row.querySelector('[data-growth-cell]');
    const endingCell = row.querySelector('[data-ending-cell]');
    const ratioCell = row.querySelector('[data-ratio-cell]');
    if (!input || !incomingInput || !beginningCell || !growthCell || !endingCell || !ratioCell) return;

    const baseBeginning = Number(row.dataset.baseBeginning);
    const baseline = Number(input.dataset.baseline);
    const formatQty = value => Math.round(value).toLocaleString('id-ID');
    const formatRatio = value => value.toLocaleString('id-ID', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const updatePreview = (syncMinimum = false) => {
      const projection = Number(input.value);
      if (!Number.isFinite(projection) || projection < 0) return;
      const currentRatio = projection ? baseBeginning / projection : null;
      const minimumIncoming = (
        !incomingInput.disabled && currentRatio !== null && currentRatio < 1.5
          ? Math.max(Math.ceil((projection * 1.5) - baseBeginning), 0)
          : 0
      );
      incomingInput.min = String(minimumIncoming);
      if (syncMinimum && !incomingInput.disabled && (Number(incomingInput.value) || 0) < minimumIncoming) {
        incomingInput.value = String(minimumIncoming);
      }
      const incoming = incomingInput.disabled ? 0 : Number(incomingInput.value);
      if (!Number.isFinite(incoming) || incoming < 0) return;
      const beginning = baseBeginning + incoming;
      beginningCell.textContent = formatQty(beginning);

      const growth = baseline ? ((projection - baseline) / baseline) * 100 : null;
      growthCell.classList.remove('growth-positive', 'growth-negative', 'growth-neutral');
      if (growth === null) {
        growthCell.textContent = '—';
        growthCell.classList.add('growth-neutral');
      } else {
        growthCell.textContent = `${growth > 0 ? '+' : ''}${growth.toLocaleString('id-ID', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`;
        growthCell.classList.add(growth > 0 ? 'growth-positive' : (growth < 0 ? 'growth-negative' : 'growth-neutral'));
      }

      const ending = beginning - projection;
      endingCell.textContent = formatQty(ending);
      endingCell.classList.toggle('metric-negative', ending < 0);

      const ratio = projection ? beginning / projection : null;
      ratioCell.textContent = ratio === null ? '—' : formatRatio(ratio);
      ratioCell.classList.toggle('ratio-alert', ratio !== null && ratio > 2);

    };

    input.addEventListener('input', () => updatePreview(true));
    incomingInput.addEventListener('input', () => updatePreview(false));
    updatePreview(true);
  });

  const formatPreviewQty = value => Math.round(value).toLocaleString('id-ID');
  const formatPreviewRatio = value => value.toLocaleString('id-ID', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const parentPreviewRows = new Map(
    [...document.querySelectorAll('[data-parent-preview-row]')].map(row => [row.dataset.parentSku, row]),
  );
  const refreshParentPreview = () => {
    const totals = new Map();
    document.querySelectorAll('[data-projection-preview-row]').forEach(row => {
      const input = row.querySelector('[data-sales-projection-input]');
      if (!input) return;
      const parentSku = row.dataset.parentSku;
      const incomingInput = row.querySelector('[data-incoming-recommendation-input]');
      const total = totals.get(parentSku) || { projection: 0, baseline: 0, baseBeginning: 0, incoming: 0 };
      total.projection += Number(input.value) || 0;
      total.baseline += Number(input.dataset.baseline) || 0;
      total.baseBeginning += Number(row.dataset.baseBeginning) || 0;
      total.incoming += incomingInput && !incomingInput.disabled ? (Number(incomingInput.value) || 0) : 0;
      totals.set(parentSku, total);
    });
    totals.forEach((total, parentSku) => {
      const row = parentPreviewRows.get(parentSku);
      if (!row) return;
      const growth = total.baseline ? ((total.projection - total.baseline) / total.baseline) * 100 : null;
      const beginning = total.baseBeginning + total.incoming;
      const ending = beginning - total.projection;
      const ratio = total.projection ? beginning / total.projection : null;
      const beginningCell = row.querySelector('[data-parent-beginning-cell]');
      const projectionCell = row.querySelector('[data-parent-projection-cell]');
      const growthCell = row.querySelector('[data-parent-growth-cell]');
      const endingCell = row.querySelector('[data-parent-ending-cell]');
      const ratioCell = row.querySelector('[data-parent-ratio-cell]');
      const incomingCell = row.querySelector('[data-parent-incoming-cell]');
      beginningCell.textContent = formatPreviewQty(beginning);
      projectionCell.textContent = formatPreviewQty(total.projection);
      growthCell.classList.remove('growth-positive', 'growth-negative', 'growth-neutral');
      if (growth === null) {
        growthCell.textContent = '—';
        growthCell.classList.add('growth-neutral');
      } else {
        growthCell.textContent = `${growth > 0 ? '+' : ''}${growth.toLocaleString('id-ID', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`;
        growthCell.classList.add(growth > 0 ? 'growth-positive' : (growth < 0 ? 'growth-negative' : 'growth-neutral'));
      }
      endingCell.textContent = formatPreviewQty(ending);
      endingCell.classList.toggle('metric-negative', ending < 0);
      ratioCell.textContent = ratio === null ? '—' : formatPreviewRatio(ratio);
      ratioCell.classList.toggle('ratio-alert', ratio !== null && ratio > 2);
      incomingCell.textContent = formatPreviewQty(total.incoming);
    });
  };
  const refreshPreviewTotals = () => {
    let projection = 0;
    let baseline = 0;
    let baseBeginning = 0;
    let incoming = 0;
    document.querySelectorAll('[data-projection-preview-row]').forEach(row => {
      const input = row.querySelector('[data-sales-projection-input]');
      const incomingInput = row.querySelector('[data-incoming-recommendation-input]');
      if (!input) return;
      projection += Number(input.value) || 0;
      baseline += Number(input.dataset.baseline) || 0;
      baseBeginning += Number(row.dataset.baseBeginning) || 0;
      incoming += incomingInput && !incomingInput.disabled ? (Number(incomingInput.value) || 0) : 0;
    });
    const beginning = baseBeginning + incoming;
    const growth = baseline ? ((projection - baseline) / baseline) * 100 : null;
    const ending = beginning - projection;
    const ratio = projection ? beginning / projection : null;
    document.querySelectorAll('[data-preview-total-beginning]').forEach(cell => {
      cell.textContent = formatPreviewQty(beginning);
    });
    document.querySelectorAll('[data-preview-total-projection]').forEach(cell => {
      cell.textContent = formatPreviewQty(projection);
    });
    document.querySelectorAll('[data-preview-total-growth]').forEach(cell => {
      cell.classList.remove('growth-positive', 'growth-negative', 'growth-neutral');
      if (growth === null) {
        cell.textContent = '—';
        cell.classList.add('growth-neutral');
      } else {
        cell.textContent = `${growth > 0 ? '+' : ''}${growth.toLocaleString('id-ID', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`;
        cell.classList.add(growth > 0 ? 'growth-positive' : (growth < 0 ? 'growth-negative' : 'growth-neutral'));
      }
    });
    document.querySelectorAll('[data-preview-total-ending]').forEach(cell => {
      cell.textContent = formatPreviewQty(ending);
      cell.classList.toggle('metric-negative', ending < 0);
    });
    document.querySelectorAll('[data-preview-total-ratio]').forEach(cell => {
      cell.textContent = ratio === null ? '—' : formatPreviewRatio(ratio);
      cell.classList.toggle('ratio-alert', ratio !== null && ratio > 2);
    });
    document.querySelectorAll('[data-preview-total-incoming]').forEach(cell => {
      cell.textContent = formatPreviewQty(incoming);
    });
  };
  const refreshPreviewAggregates = () => {
    refreshParentPreview();
    refreshPreviewTotals();
  };
  document.querySelectorAll('[data-sales-projection-input]').forEach(input => {
    input.addEventListener('input', refreshPreviewAggregates);
  });
  document.querySelectorAll('[data-incoming-recommendation-input]').forEach(input => {
    input.addEventListener('input', refreshPreviewAggregates);
  });
  refreshPreviewAggregates();

  const limitVisiblePreviewRows = tableWrap => {
    const header = tableWrap.querySelector('thead');
    const footer = tableWrap.querySelector('tfoot');
    const rows = [...tableWrap.querySelectorAll('tbody tr')];
    if (!header || rows.length <= 10 || tableWrap.closest('[hidden]')) return;
    const visibleHeight = rows.slice(0, 10).reduce(
      (height, row) => height + row.getBoundingClientRect().height,
      header.getBoundingClientRect().height + (footer?.getBoundingClientRect().height || 0),
    );
    tableWrap.style.maxHeight = `${Math.ceil(visibleHeight) + 1}px`;
  };
  document.querySelectorAll('[data-preview-table-scroll]').forEach(tableWrap => {
    window.requestAnimationFrame(() => limitVisiblePreviewRows(tableWrap));
  });

  const grainControls = [...document.querySelectorAll('[data-preview-grain-selector] input[name="preview_grain"]')];
  const syncPreviewGrain = () => {
    const grain = grainControls.find(control => control.checked)?.value || 'sku';
    document.querySelectorAll('[data-preview-grain-panel]').forEach(panel => {
      panel.hidden = panel.dataset.previewGrainPanel !== grain;
    });
    document.querySelectorAll('[data-preview-grain-heading]').forEach(heading => {
      heading.hidden = heading.dataset.previewGrainHeading !== grain;
    });
    const visiblePanel = document.querySelector(`[data-preview-grain-panel="${grain}"]`);
    window.requestAnimationFrame(() => {
      visiblePanel?.querySelectorAll('[data-preview-table-scroll]').forEach(limitVisiblePreviewRows);
    });
  };
  grainControls.forEach(control => control.addEventListener('change', syncPreviewGrain));
  if (grainControls.length) {
    syncPreviewGrain();
  }

  const draftGrainControls = [...document.querySelectorAll('[data-draft-grain-selector] input[name="draft_grain"]')];
  const draftSelectionForm = document.querySelector('[data-draft-selection-delete-form]');
  const draftSelectionGrain = document.querySelector('[data-draft-selection-grain]');
  const draftDeleteButton = document.querySelector('[data-draft-delete-selected]');
  const draftDeleteTooltip = document.querySelector('[data-draft-delete-tooltip]');
  const draftRowSelections = [...document.querySelectorAll('[data-draft-row-select]')];
  const draftSelectAllControls = [...document.querySelectorAll('[data-draft-select-all]')];

  const syncDraftSelection = () => {
    const grain = draftGrainControls.find(control => control.checked)?.value || 'sku';
    const activeRows = draftRowSelections.filter(control => control.dataset.selectionGrain === grain);
    const selectedRows = activeRows.filter(control => control.checked);
    draftSelectAllControls.forEach(control => {
      if (control.dataset.selectionGrain !== grain) {
        control.checked = false;
        control.indeterminate = false;
        return;
      }
      control.checked = activeRows.length > 0 && selectedRows.length === activeRows.length;
      control.indeterminate = selectedRows.length > 0 && selectedRows.length < activeRows.length;
    });
    if (draftSelectionGrain) draftSelectionGrain.value = grain;
    if (draftDeleteButton) {
      const itemLabel = grain === 'parent_sku' ? 'Parent SKU' : 'SKU';
      const tooltip = `Delete Selected ${itemLabel} From Draft`;
      draftDeleteButton.hidden = selectedRows.length === 0;
      draftDeleteButton.title = tooltip;
      draftDeleteButton.setAttribute('aria-label', tooltip);
      if (draftDeleteTooltip) draftDeleteTooltip.textContent = tooltip;
    }
  };

  const syncDraftGrain = () => {
    const grain = draftGrainControls.find(control => control.checked)?.value || 'sku';
    document.querySelectorAll('[data-draft-grain-panel]').forEach(panel => {
      panel.hidden = panel.dataset.draftGrainPanel !== grain;
    });
    draftRowSelections.forEach(control => {
      if (control.dataset.selectionGrain !== grain) control.checked = false;
    });
    syncDraftSelection();
    const visiblePanel = document.querySelector(`[data-draft-grain-panel="${grain}"]`);
    window.requestAnimationFrame(() => {
      visiblePanel?.querySelectorAll('[data-preview-table-scroll]').forEach(limitVisiblePreviewRows);
    });
  };
  draftGrainControls.forEach(control => control.addEventListener('change', syncDraftGrain));
  if (draftGrainControls.length) {
    syncDraftGrain();
  }
  draftRowSelections.forEach(control => control.addEventListener('change', syncDraftSelection));
  draftSelectAllControls.forEach(control => {
    control.addEventListener('change', () => {
      const grain = control.dataset.selectionGrain;
      draftRowSelections
        .filter(rowControl => rowControl.dataset.selectionGrain === grain)
        .forEach(rowControl => { rowControl.checked = control.checked; });
      syncDraftSelection();
    });
  });
  draftSelectionForm?.addEventListener('submit', event => {
    const grain = draftSelectionGrain?.value || 'sku';
    const selectedCount = draftRowSelections.filter(
      control => control.dataset.selectionGrain === grain && control.checked,
    ).length;
    if (!selectedCount) {
      event.preventDefault();
      return;
    }
    const itemLabel = grain === 'parent_sku' ? 'Parent SKU' : 'SKU';
    const accepted = window.confirm(
      `Delete ${selectedCount} selected ${itemLabel} from this Scenario Draft across all scenario months?`,
    );
    if (!accepted) event.preventDefault();
  });

  document.querySelectorAll('[data-scenario-sales-input]').forEach(salesInput => {
    const projectionId = salesInput.dataset.projectionId;
    const incomingInput = document.querySelector(
      `[data-scenario-incoming-input][data-projection-id="${projectionId}"]`,
    );
    const syncIncomingMinimum = () => {
      const sales = Number(salesInput.value);
      const beginning = Number(salesInput.dataset.beginning) || 0;
      if (!Number.isFinite(sales)) return;
      const currentRatio = sales ? beginning / sales : null;
      const minimum = (
        currentRatio !== null && currentRatio < 1.5
          ? Math.max(Math.ceil((sales * 1.5) - beginning), 0)
          : 0
      );
      if (!incomingInput) return;
      incomingInput.min = String(minimum);
      if ((Number(incomingInput.value) || 0) < minimum) {
        incomingInput.value = String(minimum);
      }
    };
    salesInput.addEventListener('input', syncIncomingMinimum);
    syncIncomingMinimum();
  });

  const setDraftGrowth = (element, growth) => {
    if (!element) return;
    element.classList.remove('growth-positive', 'growth-negative', 'growth-neutral');
    if (growth === null) {
      element.textContent = '—';
      element.classList.add('growth-neutral');
      return;
    }
    element.textContent = `${growth > 0 ? '+' : ''}${growth.toLocaleString('id-ID', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}%`;
    element.classList.add(growth > 0 ? 'growth-positive' : (growth < 0 ? 'growth-negative' : 'growth-neutral'));
  };
  const refreshDraftGrowth = () => {
    const parentMonths = new Map();
    const totalMonths = new Map();
    document.querySelectorAll('[data-draft-sku-row]').forEach(row => {
      row.querySelectorAll('[data-scenario-sales-input]').forEach(input => {
        const sales = Number(input.value) || 0;
        const baseline = Number(input.dataset.baseline) || 0;
        const month = input.dataset.month;
        const growth = baseline ? ((sales - baseline) / baseline) * 100 : null;
        setDraftGrowth(input.closest('.draft-sales-value')?.querySelector('[data-scenario-growth]'), growth);

        const parentKey = `${row.dataset.parentSku}::${month}`;
        const parentTotal = parentMonths.get(parentKey) || { sales: 0, baseline: 0 };
        parentTotal.sales += sales;
        parentTotal.baseline += baseline;
        parentMonths.set(parentKey, parentTotal);

        const total = totalMonths.get(month) || { sales: 0, baseline: 0 };
        total.sales += sales;
        total.baseline += baseline;
        totalMonths.set(month, total);
      });
    });
    document.querySelectorAll('[data-draft-parent-growth]').forEach(element => {
      const total = parentMonths.get(`${element.dataset.parentSku}::${element.dataset.month}`);
      setDraftGrowth(element, total?.baseline ? ((total.sales - total.baseline) / total.baseline) * 100 : null);
    });
    document.querySelectorAll('[data-draft-parent-sales]').forEach(element => {
      const total = parentMonths.get(`${element.dataset.parentSku}::${element.dataset.month}`);
      if (total) element.textContent = formatPreviewQty(total.sales);
    });
    document.querySelectorAll('[data-draft-total-growth]').forEach(element => {
      const total = totalMonths.get(element.dataset.month);
      setDraftGrowth(element, total?.baseline ? ((total.sales - total.baseline) / total.baseline) * 100 : null);
    });
    document.querySelectorAll('[data-draft-total-sales]').forEach(element => {
      const total = totalMonths.get(element.dataset.month);
      if (total) element.textContent = formatPreviewQty(total.sales);
    });
  };
  document.querySelectorAll('[data-scenario-sales-input]').forEach(input => {
    input.addEventListener('input', refreshDraftGrowth);
  });
  refreshDraftGrowth();

  const poCogsInputs = [...document.querySelectorAll('[data-po-cogs-input]')];
  const syncPoCogsReview = () => {
    let grandTotal = 0;
    let invalidCount = 0;
    poCogsInputs.forEach(input => {
      const cogs = Number(input.value);
      const qty = Number(input.dataset.qty);
      const isValid = Number.isInteger(cogs) && cogs > 0 && Number.isFinite(qty);
      const lineOutput = input.closest('tr')?.querySelector('[data-po-cogs-line-total]');
      input.classList.toggle('input-error', !isValid);
      if (!isValid) {
        invalidCount += 1;
        if (lineOutput) lineOutput.textContent = '—';
        return;
      }
      const lineTotal = cogs * qty;
      grandTotal += lineTotal;
      if (lineOutput) lineOutput.textContent = `Rp ${lineTotal.toLocaleString('id-ID')}`;
    });
    const grandTotalOutput = document.querySelector('[data-po-cogs-grand-total]');
    if (grandTotalOutput) grandTotalOutput.textContent = `Rp ${grandTotal.toLocaleString('id-ID')}`;
    const invalidOutput = document.querySelector('[data-po-cogs-invalid-count]');
    if (invalidOutput) invalidOutput.textContent = invalidCount ? String(invalidCount) : '0';
    const statusCopy = document.querySelector('[data-po-cogs-status-copy]');
    if (statusCopy) statusCopy.textContent = invalidCount ? 'Wajib dilengkapi' : 'Siap dikonfirmasi';
    document.querySelector('[data-po-cogs-status-card]')?.classList.toggle('danger-card', invalidCount > 0);
  };
  poCogsInputs.forEach(input => input.addEventListener('input', syncPoCogsReview));
  if (poCogsInputs.length) syncPoCogsReview();

  document.querySelectorAll('[data-po-product-toggle]').forEach(toggle => {
    toggle.addEventListener('click', () => {
      const targetId = toggle.dataset.poProductToggle;
      const detailRow = document.getElementById(targetId);
      if (!detailRow) return;
      const shouldOpen = detailRow.hidden;
      detailRow.hidden = !shouldOpen;
      document.querySelectorAll(`[data-po-product-toggle="${targetId}"]`).forEach(peer => {
        peer.setAttribute('aria-expanded', String(shouldOpen));
      });
    });
  });
});
