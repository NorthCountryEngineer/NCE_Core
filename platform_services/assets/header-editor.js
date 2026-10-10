/* Administrator preview: declarative values only; no requests or persisted changes. */
(() => {
  const form = document.getElementById('header-editor'); if (!form) return;
  const state = JSON.parse(document.getElementById('header-preview-state').textContent);
  const presets = JSON.parse(document.getElementById('header-presets').textContent);
  const defaults = JSON.parse(document.getElementById('header-defaults').textContent);
  const preview = document.getElementById('header-preview');
  const field = name => form.elements.namedItem(name);
  const note = document.getElementById('preview-note');
  function update() {
    const config = {...defaults};
    ['logo_url', 'preset', 'density', 'background', 'foreground', 'accent'].forEach(key => config[key] = field(key).value);
    config.show_organization = field('show_organization').checked;
    // Use DOM catalog order for ties, matching the server.
    config.widgets = Array.from(form.querySelectorAll('input[name^="widget_"]')).filter(input => input.checked)
      .map(input => input.name.slice(7)).sort((a, b) => Number(field('order_' + a).value) - Number(field('order_' + b).value));
    if (presets[config.preset]) [config.background, config.foreground, config.accent] = presets[config.preset];
    const rows = new Map(state.applications.map(row => [row.id, row]));
    const shortcuts = Array.from(field('shortcuts').selectedOptions).map(option => rows.get(option.value)).filter(Boolean);
    const links = [];
    form.querySelectorAll('.link-card').forEach(card => {
      const find = suffix => card.querySelector(`[name$="-${suffix}"]`);
      if (find('DELETE').checked || !find('label').value.trim() || !find('url').value.trim()) return;
      links.push({label: find('label').value, url: find('url').value, new_tab: find('new_tab').checked, order: Number(find('ORDER').value) || 999});
    });
    links.sort((a, b) => a.order - b.order);
    preview.previewConfiguration({...state, enabled: true, brand: field('identity_name').value || state.brand,
      appearance: config, shortcuts, links});
    note.textContent = field('unified_header_enabled').checked ? 'Unsaved appearance preview. Preview controls do not open tools or send requests.' :
      'The organization default is disabled. This preview shows the appearance for apps that explicitly enable the header.';
  }
  customElements.whenDefined('core-header').then(() => {
    update(); form.addEventListener('input', event => {
      if (['background', 'foreground', 'accent'].includes(event.target.name)) field('preset').value = 'custom';
      if (event.target.name === 'preset' && presets[event.target.value]) {
        ['background', 'foreground', 'accent'].forEach((key, index) => field(key).value = presets[event.target.value][index]);
      }
      update();
    });
    form.addEventListener('change', update);
  });
  document.getElementById('add-header-link').onclick = () => {
    const total = field('links-TOTAL_FORMS'), count = Number(total.value);
    if (count >= 8) { note.textContent = 'Up to eight link rows. Remove an existing link and save before adding another.'; return; }
    const fragment = document.getElementById('header-link-template').content.cloneNode(true);
    fragment.querySelectorAll('[name],[id],[for]').forEach(node => ['name', 'id', 'for'].forEach(key => {
      if (node.hasAttribute(key)) node.setAttribute(key, node.getAttribute(key).replaceAll('__prefix__', String(count)));
    }));
    document.getElementById('header-links').append(fragment); total.value = count + 1; update();
  };
})();
