document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form[data-auto-submit]').forEach(form => {
    const submit = () => {
      if (form.dataset.submitting === 'true') return;
      form.dataset.submitting = 'true';
      form.requestSubmit();
    };
    form.querySelectorAll('input[type="date"], input[type="month"], select').forEach(control => {
      control.addEventListener('change', submit);
    });
    form.querySelectorAll('[data-chip-filter]').forEach(control => {
      control.addEventListener('change', submit);
    });
    form.addEventListener('filters:auto-submit', submit);
  });
});
