document.querySelectorAll('[data-work-style-form]').forEach(form => {
  const activities = form.querySelector('[data-owner-activity]');
  const teamInfo = form.querySelector('[data-work-team]');
  function update() {
    const team = form.querySelector('input[name="work_style"]:checked')?.value === 'team';
    activities.hidden = !team;
    activities.disabled = !team;
    activities.querySelectorAll('input').forEach(input => { input.required = team; });
    teamInfo.hidden = !team;
  }
  form.querySelectorAll('input[name="work_style"]').forEach(input => input.addEventListener('change', update));
  update();
});
