let csrf;
const status = document.getElementById('status');
async function call(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { 'Content-Type': 'application/json',
    'X-Core-Purpose': document.getElementById('purpose').value, 'X-CSRF-Token': csrf || '', ...(options.headers || {}) } });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'Request failed');
  return value;
}
async function refresh() {
  try {
    const spec = await call('/api/spec'); csrf = spec.csrf;
    document.getElementById('title').textContent = spec.name;
    const root = document.getElementById('collections'); root.replaceChildren();
    for (const resource of spec.resources) {
      const section = document.createElement('section'), heading = document.createElement('h2');
      heading.textContent = resource.name; section.append(heading);
      const form = document.createElement('form'), inputs = {};
      for (const [field, kind] of Object.entries(resource.fields)) {
        const label = document.createElement('label'), input = document.createElement('input');
        label.textContent = field + ' '; input.type = kind === 'boolean' ? 'checkbox' : kind === 'integer' ? 'number' : 'text';
        input.maxLength = 2000; label.append(input); form.append(label); inputs[field] = input;
      }
      const submit = document.createElement('button'); submit.textContent = 'Create record'; form.append(submit);
      form.onsubmit = async event => {
        event.preventDefault();
        const data = Object.fromEntries(Object.entries(resource.fields).map(([field, kind]) =>
          [field, kind === 'boolean' ? inputs[field].checked : kind === 'integer' ? Number(inputs[field].value) : inputs[field].value]));
        try { await call('/api/records/' + resource.slug, { method: 'POST', body: JSON.stringify(data) }); await refresh(); }
        catch (error) { status.textContent = error.message; }
      };
      section.append(form);
      try { const records = await call('/api/records/' + resource.slug); const pre = document.createElement('pre');
        pre.textContent = JSON.stringify(records.records, null, 2); section.append(pre); }
      catch (error) { const message = document.createElement('p'); message.textContent = error.message; section.append(message); }
      root.append(section);
    }
    status.textContent = 'Application loaded';
  } catch (error) { status.textContent = error.message; }
}
document.getElementById('refresh').onclick = refresh;
document.getElementById('logout').onclick = async () => {
  try { await call('/logout', { method: 'POST' }); document.getElementById('collections').replaceChildren(); status.textContent = 'Signed out'; }
  catch (error) { status.textContent = error.message; }
};
refresh();
