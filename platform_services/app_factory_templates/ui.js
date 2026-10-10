let csrf;
const status = document.getElementById('status');
async function call(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { 'Content-Type': 'application/json',
    'X-Core-Purpose': document.getElementById('purpose').value, 'X-CSRF-Token': csrf || '', ...(options.headers || {}) } });
  const value = await response.json();
  if (!response.ok) {
    if (value.error === 'quota_exceeded') throw new Error('Your shared ' + (value.metric || 'usage') +
      ' quota is exhausted across applications.' + (value.reset_at ? ' Resets at ' + new Date(value.reset_at).toLocaleString() + '.' : ' Ask your administrator to review your quota.'));
    if (value.error === 'usage_overrun_requires_review') throw new Error('Usage needs administrator review before more operations can start.');
    throw new Error(value.error || 'Request failed');
  }
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
  try { await call('/logout', { method: 'POST' }); document.getElementById('collections').replaceChildren(); document.getElementById('docs-results').replaceChildren(); status.textContent = 'Signed out'; }
  catch (error) { status.textContent = error.message; }
};
document.getElementById('docs-search').onclick = async () => {
  const root = document.getElementById('docs-results'), notice = document.getElementById('docs-status');
  root.replaceChildren();
  try {
    const value = await call('/api/documentation?q=' + encodeURIComponent(document.getElementById('docs-query').value));
    for (const guide of value.documents) {
      const section = document.createElement('section'), button = document.createElement('button');
      button.textContent = guide.title; section.append(button);
      button.onclick = async () => {
        try {
          const page = await call('/api/documentation?document_id=' + encodeURIComponent(guide.id));
          const pre = document.createElement('pre'), citation = document.createElement('p');
          pre.textContent = page.markdown; citation.textContent = page.citation + ' · ' + page.digest;
          section.replaceChildren(button, pre, citation);
        } catch (error) { notice.textContent = error.message; }
      };
      root.append(section);
    }
    notice.textContent = value.next_cursor ? 'More guides are available; narrow your search.' : 'Authorized guides loaded';
  } catch (error) { notice.textContent = error.message; }
};
refresh();
