document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-material-formset]").forEach((formset) => {
    const rows = formset.querySelector("[data-material-rows]");
    const template = formset.querySelector("[data-material-template]");
    const totalForms = formset.querySelector('input[name$="-TOTAL_FORMS"]');

    formset.querySelector("[data-add-material]")?.addEventListener("click", () => {
      const index = Number(totalForms.value);
      const fragment = template.content.cloneNode(true);
      fragment.querySelector("[data-material-row]").innerHTML = fragment
        .querySelector("[data-material-row]")
        .innerHTML.replaceAll("__prefix__", String(index));
      rows.append(fragment);
      totalForms.value = String(index + 1);
      rows.lastElementChild.querySelector("input:not([type=hidden])")?.focus();
    });

    rows.addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-material]");
      if (!button) return;
      const row = button.closest("[data-material-row]");
      const id = row.querySelector('input[name$="-id"]');
      const deleted = row.querySelector('input[name$="-DELETE"]');
      if (id?.value && deleted) {
        deleted.value = "on";
        row.hidden = true;
      } else {
        row.remove();
      }
    });
  });
});
