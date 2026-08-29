const periodForm = document.getElementById("ig-period-form");
if (periodForm) {
  const period = periodForm.querySelector('[name="period"]');
  const dates = document.getElementById("ig-custom-dates");
  const month = document.getElementById("ig-month");
  const toggleDates = () => {
    const custom = period.value === "custom";
    const monthly = period.value === "month" || period.value === "month_mtd";
    dates.hidden = !custom;
    dates.querySelectorAll("input").forEach(input => {
      input.disabled = !custom;
      input.required = custom;
    });
    month.hidden = !monthly;
    month.querySelectorAll("input").forEach(input => {
      input.disabled = !monthly;
      input.required = monthly;
    });
  };
  period.addEventListener("change", toggleDates);
  toggleDates();
}
