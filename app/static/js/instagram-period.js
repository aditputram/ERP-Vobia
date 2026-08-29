const periodForm = document.getElementById("ig-period-form");
if (periodForm) {
  const period = periodForm.querySelector('[name="period"]');
  const dates = document.getElementById("ig-custom-dates");
  const toggleDates = () => {
    const custom = period.value === "custom";
    dates.hidden = !custom;
    dates.querySelectorAll("input").forEach(input => {
      input.disabled = !custom;
      input.required = custom;
    });
  };
  period.addEventListener("change", toggleDates);
  toggleDates();
}
