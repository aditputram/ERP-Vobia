const initProductPicker = (picker) => {
  if (picker.dataset.ready) return;
  picker.dataset.ready = "true";
  const select = picker.querySelector("select");
  const options = [...select.options]
    .filter((option) => option.value)
    .map((option) => ({ value: option.value, text: option.text }));
  const toggle = document.createElement("button");
  const panel = document.createElement("div");
  const search = document.createElement("input");
  const list = document.createElement("div");

  toggle.type = "button";
  toggle.className = "product-picker-toggle";
  toggle.textContent = select.selectedOptions[0]?.value
    ? select.selectedOptions[0].text
    : "Pilih Product…";
  toggle.setAttribute("aria-expanded", "false");

  panel.className = "product-picker-panel";
  panel.hidden = true;
  search.type = "search";
  search.placeholder = "Cari nama produk…";
  search.setAttribute("aria-label", "Cari Product");
  list.className = "product-picker-options";
  list.setAttribute("role", "listbox");
  panel.append(search, list);
  picker.append(toggle, panel);
  select.hidden = true;

  const close = () => {
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  };
  const render = () => {
    const query = search.value.trim().toLocaleLowerCase("id");
    const matches = options.filter((option) =>
      option.text.toLocaleLowerCase("id").includes(query)
    );
    list.replaceChildren(
      ...matches.map((option) => {
        const choice = document.createElement("button");
        choice.type = "button";
        choice.textContent = option.text;
        choice.setAttribute("role", "option");
        choice.addEventListener("click", () => {
          select.value = option.value;
          select.dispatchEvent(new Event("change", { bubbles: true }));
          toggle.textContent = option.text;
          close();
        });
        return choice;
      })
    );
    if (!matches.length) {
      const empty = document.createElement("span");
      empty.textContent = "Produk tidak ditemukan";
      list.append(empty);
    }
  };

  toggle.addEventListener("click", () => {
    const opening = panel.hidden;
    document.querySelectorAll(".product-picker-panel").forEach((item) => {
      item.hidden = true;
    });
    panel.hidden = !opening;
    toggle.setAttribute("aria-expanded", String(opening));
    if (opening) {
      search.value = "";
      render();
      search.focus();
    }
  });
  search.addEventListener("input", render);
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      close();
      toggle.focus();
    }
  });
  document.addEventListener("click", (event) => {
    if (!picker.contains(event.target)) close();
  });
};

document.querySelectorAll("[data-rupiah]").forEach((input) => {
  const format = () => {
    const digits = input.value.replace(/\D/g, "");
    input.value = digits ? `Rp. ${digits.replace(/\B(?=(\d{3})+(?!\d))/g, ".")}` : "";
  };
  input.addEventListener("input", format);
  format();
});

document.querySelectorAll("[data-product-picker]").forEach(initProductPicker);

const addProduct = document.querySelector("#add-campaign-product");
const productRows = document.querySelector("#campaign-products");
const productTemplate = document.querySelector("#campaign-product-template");
const totalForms = document.querySelector("#id_products-TOTAL_FORMS");

if (addProduct && productRows && productTemplate && totalForms) {
  addProduct.addEventListener("click", () => {
    const index = Number(totalForms.value);
    const fragment = productTemplate.content.cloneNode(true);
    const row = fragment.querySelector("[data-campaign-product-row]");
    row.innerHTML = row.innerHTML.replaceAll("__prefix__", String(index));
    productRows.append(fragment);
    totalForms.value = String(index + 1);
    initProductPicker(productRows.lastElementChild.querySelector("[data-product-picker]"));
  });
}
