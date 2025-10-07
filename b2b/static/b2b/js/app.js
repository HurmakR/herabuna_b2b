// Quantity stepper: works for elements with data-stepper wrapper
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-stepper-btn]');
  if (!btn) return;
  const wrap = btn.closest('[data-stepper]');
  const input = wrap.querySelector('input[type="number"]');
  if (!input) return;

  const step = parseInt(input.getAttribute('step') || '1', 10);
  const min = parseInt(input.getAttribute('min') || '0', 10);
  const max = parseInt(input.getAttribute('max') || '999999', 10);
  let v = parseInt(input.value || '0', 10);

  if (btn.dataset.stepperBtn === 'minus') v -= step;
  if (btn.dataset.stepperBtn === 'plus') v += step;

  if (v < min) v = min;
  if (v > max) v = max;
  input.value = v;
});
