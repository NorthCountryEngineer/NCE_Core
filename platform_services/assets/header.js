/* Organization user tools. No access tokens, third-party cookies, or framework dependencies. */
(() => {
  if (customElements.get('core-header')) return;
  const css = `
    :host{display:block;position:relative;z-index:40;font:14px/1.5 var(--core-font,"Segoe UI",system-ui,sans-serif);color:#25332e;--tool-line:#bec9c1;--tool-chrome:#eff2ef;--tool-muted:#52625b}
    *{box-sizing:border-box}[hidden]{display:none!important}button,a,input,textarea,select{font:inherit}
    a{color:inherit;text-decoration:none}a:hover{text-decoration:underline;text-underline-offset:3px}
    button{cursor:pointer;background:transparent;border:1px solid transparent;border-radius:3px;padding:7px 10px;color:inherit;min-height:32px}
    button:hover{background:var(--tool-chrome)}button:disabled{opacity:.65;cursor:default}
    button:focus-visible,a:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:3px solid #00649e;outline-offset:3px}.bar :focus-visible{outline-color:var(--core-foreground,#fff)}
    .shell-status{margin:0;padding:10px 20px;background:#fff;color:#a02c2c;border-bottom:1px solid var(--tool-line)}.sr-only{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap}
    .bar{min-height:60px;padding:8px 20px;display:flex;align-items:center;gap:12px;background:var(--core-background,#193d36);color:var(--core-foreground,#fff);flex-wrap:wrap;border-bottom:1px solid #00000036;box-shadow:inset 0 1px 0 #ffffff20}
    .bar.compact{min-height:48px;padding:5px 14px;gap:8px}.identity{display:flex;align-items:center;gap:10px;max-width:280px;padding-right:14px;border-right:1px solid currentColor}.logo{width:32px;height:32px;object-fit:contain;flex-shrink:0}
    .brand{display:flex;flex-direction:column;min-width:0;max-width:240px}.brand strong,.brand small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.brand strong{font-size:13px;font-weight:650}.brand small{font-size:11px}
    .pinned{display:flex;gap:6px;min-width:0;overflow-x:auto;flex:1}.pinned a,.shared-links a{flex-shrink:0;padding:6px 10px;border:1px solid currentColor;border-radius:2px;font-size:12px}
    .bar button{font-size:12px}.bar button:hover{background:transparent;border-color:currentColor}.tools,.account-widget{display:flex;gap:12px;align-items:center}.account-widget{margin-left:auto}.shared-links{display:flex;gap:6px;overflow-x:auto;max-width:35%;font-size:12px}
    .environment{padding:4px 7px;border:1px solid currentColor;border-radius:2px;font-size:11px;font-family:var(--core-mono,ui-monospace,monospace)}.pinned:empty,.shared-links:empty{display:none}
    .tools a{font-size:12px}.account-toggle{display:none}.badge{background:var(--core-accent,#245e52);color:var(--core-accent-foreground,#fff);border:1px solid currentColor;border-radius:2px;padding:1px 5px;font-size:11px;margin-left:5px}
    .drawer{position:fixed;right:12px;top:70px;width:440px;max-width:calc(100vw - 24px);height:min(720px,calc(100dvh - 82px));background:#fff;border:1px solid #718279;border-radius:3px;box-shadow:2px 3px 0 #25332e20,0 10px 30px #25332e14;display:flex;flex-direction:column;overflow:hidden}
    .top{display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--tool-chrome);border-bottom:1px solid var(--tool-line);box-shadow:inset 0 1px 0 #fff;flex-shrink:0}.top h2{margin:0;font-size:14px;flex:1}.top button{border-color:#718279;background:#f7f9f6;box-shadow:inset 1px 1px 0 #fff;min-width:32px}
    .body{padding:8px 14px;overflow:auto;flex:1;min-height:0}.row{display:flex;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid var(--tool-line)}.row a,.row .room{flex:1;min-width:0}.row small{display:block;color:var(--tool-muted);font-size:11px}.row .room{text-align:left;padding:6px 4px}.row .room:hover{background:var(--tool-chrome)}
    .search,select{border:1px solid #718279;border-radius:3px;padding:8px;width:100%;background:#fff;color:#25332e;box-shadow:inset 0 1px 2px #25332e0e}.search{margin:10px 0}.empty{color:var(--tool-muted);padding:18px 4px;line-height:1.6}
    .message{margin:0;padding:12px 0;border-bottom:1px solid var(--tool-line);overflow-wrap:anywhere}.message small{color:var(--tool-muted);margin-left:8px;font-size:11px}.message p{white-space:pre-wrap;margin:6px 0 0;line-height:1.6}.message strong{font-size:12px}
    .compose{padding:12px;border-top:1px solid var(--tool-line);background:var(--tool-chrome)}.compose textarea{display:block;width:100%;resize:vertical;min-height:72px;max-height:180px;padding:10px;border:1px solid #718279;border-radius:3px;box-shadow:inset 0 1px 2px #25332e0e;background:#fff;color:#25332e}.compose button{background:var(--core-accent,#245e52);color:var(--core-accent-foreground,#fff);border-color:currentColor;float:right;margin-top:8px;font-weight:600}.compose button:hover{text-decoration:underline}
    .status{font-size:12px;color:#a02c2c;min-height:18px;padding:5px 12px}.mode{display:flex;gap:8px;align-items:center;padding:8px 12px;font-size:12px;border-bottom:1px solid var(--tool-line)}.mode select{width:auto;flex:1}
    @media(max-width:700px){.bar,.bar.compact{padding:8px 10px;gap:6px}.bar>button{padding:6px 8px;font-size:12px}.brand{max-width:100px}.identity{padding-right:8px}.tools a{display:none}.account-toggle{display:block}.pinned,.shared-links{order:10;flex-basis:100%;max-width:100%}.drawer{top:64px;right:8px;max-width:calc(100vw - 16px);height:calc(100dvh - 76px)}.tools{margin-left:auto;gap:5px}}
    /* The page consumes its safe-area insets; independent hosts can inherit theirs. */
    .bar,.bar.compact{padding-top:calc(8px + var(--core-header-inset-top,env(safe-area-inset-top,0px)));padding-right:calc(20px + var(--core-header-inset-right,env(safe-area-inset-right,0px)));padding-left:calc(20px + var(--core-header-inset-left,env(safe-area-inset-left,0px)))}
    .bar.compact{padding-top:calc(5px + var(--core-header-inset-top,env(safe-area-inset-top,0px)));padding-right:calc(14px + var(--core-header-inset-right,env(safe-area-inset-right,0px)));padding-left:calc(14px + var(--core-header-inset-left,env(safe-area-inset-left,0px)))}
    .drawer{right:calc(12px + var(--core-safe-area-right,env(safe-area-inset-right,0px)));max-width:calc(100vw - 24px - var(--core-safe-area-left,env(safe-area-inset-left,0px)) - var(--core-safe-area-right,env(safe-area-inset-right,0px)))}
    @media(max-width:700px){.bar,.bar.compact{padding-top:calc(8px + var(--core-header-inset-top,env(safe-area-inset-top,0px)));padding-right:calc(10px + var(--core-header-inset-right,env(safe-area-inset-right,0px)));padding-left:calc(10px + var(--core-header-inset-left,env(safe-area-inset-left,0px)))}input,textarea,select{font-size:16px}.bar button,.pinned a,.shared-links a,.top button,.row button,.compose button{min-height:44px}}
    @media(prefers-reduced-motion:no-preference){.drawer{animation:enter .1s ease-out}@keyframes enter{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:translateY(0)}}}
    @media(forced-colors:active){:focus-visible,.bar :focus-visible{outline-color:Highlight}.drawer,.top button,.search,select,.compose textarea{border-color:CanvasText}}
  `;
  function el(tag, text, attrs = {}) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }
  function link(label, url) {
    const parsed = new URL(url, location.href);
    if (parsed.username || parsed.password || !(['https:'].includes(parsed.protocol) ||
        parsed.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname))) throw Error('Unsafe link');
    return el('a', label, {href: parsed.href});
  }
  class CoreHeader extends HTMLElement {
    connectedCallback() {
      if (this.ready) return;
      this.ready = true; this.attachShadow({mode: 'open'});
      this.endpoint = new URL(this.getAttribute('endpoint'), location.href);
      if (this.endpoint.origin !== location.origin || !this.endpoint.pathname.endsWith('/') ||
          this.endpoint.search || this.endpoint.hash || this.endpoint.username) { this.hidden = true; return; }
      this.root = this.shadowRoot;
      if (this.hasAttribute('preview')) this.root.addEventListener('click', event => event.preventDefault(), true);
      if ('adoptedStyleSheets' in this.root && 'replaceSync' in CSSStyleSheet.prototype) {
        const sheet = new CSSStyleSheet(); sheet.replaceSync(css); this.root.adoptedStyleSheets = [sheet];
      } else this.root.append(el('style', css));
      this.bar = el('nav', undefined, {'aria-label': 'Organization user tools', class: 'bar'});
      this.identity = el('div', undefined, {class: 'identity'});
      this.logo = el('img', undefined, {class: 'logo', alt: 'Organization logo', hidden: '', referrerpolicy: 'no-referrer'});
      this.logo.onerror = () => { this.logo.hidden = true; };
      this.brand = el('div', undefined, {class: 'brand'}); this.brandName = el('strong', this.getAttribute('brand') || 'Core Platform');
      this.orgName = el('small', ''); this.brand.append(this.brandName, this.orgName);
      this.identity.append(this.logo, this.brand);
      this.appsButton = el('button', 'Apps', {'aria-label': 'Applications and pins', 'aria-expanded': 'false', disabled: ''});
      this.appsButton.onclick = () => this.toggle('apps');
      this.pinned = el('div', undefined, {class: 'pinned', 'aria-label': 'Pinned applications'});
      this.tools = el('div', undefined, {class: 'tools'});
      this.sharedLinks = el('div', undefined, {class: 'shared-links', 'aria-label': 'Organization links'});
      this.environment = el('span', '', {class: 'environment', 'aria-label': 'Current deployment environment', hidden: ''});
      this.chatButton = el('button', 'Chat', {'aria-label': 'Open header chat', 'aria-expanded': 'false', hidden: ''});
      this.chatBadge = el('span', '', {class: 'badge', 'aria-label': 'Chat notifications', hidden: ''}); this.chatButton.append(this.chatBadge);
      this.chatButton.onclick = () => this.toggle('chat');
      this.accountButton = el('button', 'Account', {class: 'account-toggle', 'aria-label': 'Core account tools', 'aria-expanded': 'false', disabled: ''});
      this.accountButton.onclick = () => this.toggle('account');
      this.accountWidget = el('div', undefined, {class: 'account-widget'}); this.accountWidget.append(this.tools, this.accountButton);
      this.accountSlot = el('slot', undefined, {name: 'account'});
      this.bar.append(this.identity, this.appsButton, this.pinned, this.sharedLinks, this.accountWidget, this.chatButton, this.environment, this.accountSlot);
      this.drawer = el('section', undefined, {id: 'tools-panel', class: 'drawer', role: 'dialog', 'aria-modal': 'false', 'aria-label': 'Organization tools panel', 'aria-describedby': 'panel-keyboard-help', hidden: ''});
      [this.appsButton, this.chatButton, this.accountButton].forEach(button => { button.setAttribute('aria-controls', 'tools-panel'); button.setAttribute('aria-haspopup', 'dialog'); });
      this.panelTop = el('div', undefined, {class: 'top'}); this.back = el('button', '←', {'aria-label': 'Back to header conversations', hidden: ''});
      this.back.onclick = () => { this.room = null; ++this.epoch; this.showChatList(); this.close.focus(); };
      this.panelTitle = el('h2', ''); this.close = el('button', '×', {'aria-label': 'Close organization tools'});
      this.close.onclick = () => this.closePanel(); this.panelTop.append(this.back, this.panelTitle, this.close);
      this.panelBody = el('div', undefined, {class: 'body'});
      this.status = el('div', '', {class: 'status', role: 'status', 'aria-live': 'polite'});
      this.panelHelp = el('p', 'Escape closes this panel. F6 moves between this panel and its header button. Tab can leave the panel.', {id: 'panel-keyboard-help', class: 'sr-only'});
      this.notice = el('p', '', {class: 'shell-status', role: 'status', hidden: ''});
      this.announcements = el('p', '', {class: 'sr-only', role: 'status'});
      this.drawer.append(this.panelTop, this.panelHelp, this.panelBody, this.status); this.root.append(this.bar, this.notice, this.announcements, this.drawer);
      this.epoch = 0; this.messages = new Set(); this.blocked = false;
      this.keyHandler = event => {
        if (this.drawer.hidden) return;
        if (event.key === 'Escape' && event.composedPath().includes(this)) {
          event.preventDefault(); event.stopImmediatePropagation(); this.closePanel();
        } else if (event.key === 'F6') {
          const otherDialog = event.composedPath().find(node => node instanceof Element && node.matches('[role="dialog"],dialog'));
          if (otherDialog && otherDialog !== this.drawer) return;
          event.preventDefault();
          if (this.drawer.contains(this.root.activeElement)) this.focusOpener(); else this.close.focus();
        }
      };
      document.addEventListener('keydown', this.keyHandler);
      if (this.hasAttribute('preview')) return;
      this.focusHandler = event => {
        // A nonmodal panel must not cover keyboard focus in the application.
        if (!this.drawer.hidden && !event.composedPath().includes(this)) this.closePanel(false);
      };
      document.addEventListener('focusin', this.focusHandler);
      this.resizeHandler = () => this.positionDrawer();
      this.resizeObserver = new ResizeObserver(this.resizeHandler); this.resizeObserver.observe(this.bar);
      window.addEventListener('resize', this.resizeHandler); document.addEventListener('scroll', this.resizeHandler, true);
      this.refresh();
      this.timer = setInterval(() => { if (!document.hidden) { this.refresh(); if (this.room && !this.drawer.hidden) this.read(); } }, 5000);
    }
    disconnectedCallback() {
      clearInterval(this.timer); document.removeEventListener('keydown', this.keyHandler); this.resizeObserver?.disconnect();
      document.removeEventListener('focusin', this.focusHandler);
      window.removeEventListener('resize', this.resizeHandler); document.removeEventListener('scroll', this.resizeHandler, true); ++this.epoch;
    }
    positionDrawer() {
      const top = Math.max(8, Math.min(Math.round(this.bar.getBoundingClientRect().bottom + 8), Math.max(8, innerHeight - 200)));
      if (this.drawerTop === top) return; this.drawerTop = top;
      const text = `.drawer{top:${top}px;height:min(720px,calc(100dvh - ${top + 12}px - var(--core-safe-area-bottom,env(safe-area-inset-bottom,0px))))}`;
      if ('adoptedStyleSheets' in this.root && 'replaceSync' in CSSStyleSheet.prototype) {
        if (!this.placementSheet) { this.placementSheet = new CSSStyleSheet(); this.root.adoptedStyleSheets = [...this.root.adoptedStyleSheets, this.placementSheet]; }
        this.placementSheet.replaceSync(text);
      } else { if (!this.placementStyle) { this.placementStyle = el('style'); this.root.append(this.placementStyle); } this.placementStyle.textContent = text; }
    }
    async request(path = '', values) {
      const result = await fetch(this.endpoint.href + path, {method: values === undefined ? 'GET' : 'POST', credentials: 'same-origin',
        cache: 'no-store', redirect: 'error', headers: {...(this.getAttribute('purpose-code') ? {'X-Core-Purpose': this.getAttribute('purpose-code')} : {}),
          ...(values === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRFToken': this.getAttribute('csrf-token') || ''})},
        body: values === undefined ? undefined : JSON.stringify(values)});
      if (!result.ok) {
        if ([403, 404].includes(result.status) && path.startsWith('chat/')) {
          // A room or widget can be disabled while a read is in flight. Clear
          // private Chat content immediately, then recheck the whole shell.
          ++this.epoch; this.room = null; this.clearChat(); this.panelBody.replaceChildren();
          this.chatButton.hidden = true; this.closePanel();
          this.notice.hidden = false; this.notice.textContent = 'Conversation access changed. Refresh to continue.';
          if (this.state) this.state.chat = {enabled: false, rooms: [], unread: 0, notifications: 0};
          this.refresh();
        } else if ([401, 403, 404].includes(result.status)) this.revoke();
        throw Error(result.status === 503 ? 'Chat is temporarily unavailable.' : 'Request failed. Refresh or check your access.');
      }
      return result.json();
    }
    revoke() {
      const barFocused = this.bar.contains(this.root.activeElement);
      if (this.panelBody.contains(this.root.activeElement)) this.close.focus();
      this.blocked = true; ++this.epoch; this.state = null; this.room = null;
      this.pinned.replaceChildren(); this.sharedLinks.replaceChildren(); this.tools.replaceChildren(); this.environment.hidden = true; this.chatButton.hidden = true;
      this.appsButton.disabled = true; this.accountButton.disabled = true;
      this.clearChat(); this.panelBody.replaceChildren(el('p', 'Access changed. Refresh the page to continue.', {class: 'empty'}));
      this.panelTitle.textContent = 'User tools';
      this.status.textContent = 'Access changed. Refresh the page to continue.';
      this.notice.hidden = false; this.notice.textContent = this.status.textContent;
      if (barFocused) this.focusOpener();
    }
    async refresh() {
      if (this.blocked || !this.isConnected) return;
      if (this.refreshing) { this.refreshPending = true; return; }
      this.refreshing = true;
      try {
        const state = await this.request();
        if (this.blocked || !this.isConnected) return;
        if (state.organization_id !== this.getAttribute('organization-id') || state.version !== 1) { this.revoke(); return; }
        this.renderState(state);
      } catch (error) {
        if (!this.blocked) {
          const barFocused = this.bar.contains(this.root.activeElement);
          if (this.panelBody.contains(this.root.activeElement)) this.close.focus();
          ++this.epoch; this.state = null; this.room = null; this.clearChat(); this.panelBody.replaceChildren();
          this.pinned.replaceChildren(); this.sharedLinks.replaceChildren(); this.tools.replaceChildren(); this.environment.hidden = true; this.chatButton.hidden = true;
          this.appsButton.disabled = true; this.accountButton.disabled = true;
          this.status.textContent = 'User tools are temporarily unavailable.';
          this.panelTitle.textContent = 'User tools'; this.notice.hidden = false; this.notice.textContent = this.status.textContent;
          if (barFocused) this.focusOpener();
        }
      } finally {
        this.refreshing = false;
        if (this.refreshPending) { this.refreshPending = false; queueMicrotask(() => this.refresh()); }
      }
    }
    previewConfiguration(state) { if (this.hasAttribute('preview')) this.renderState(state); }
    appearance(state) {
      const config = state.appearance || {};
      const colors = [config.background || '#193d36', config.foreground || '#ffffff', config.accent || '#245e52'];
      if (colors.some(color => !/^#[0-9a-fA-F]{6}$/.test(color))) throw Error('Invalid colors');
      const channel = color => {
        const values = [1, 3, 5].map(i => parseInt(color.slice(i, i + 2), 16) / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
        return values.reduce((total, value, i) => total + value * [.2126, .7152, .0722][i], 0);
      };
      const accentText = (1.05 / (channel(colors[2]) + .05)) >= 4.5 ? '#ffffff' : '#000000';
      if (this.colorKey !== colors.join(',')) {
        this.colorKey = colors.join(',');
        const text = `:host{--core-background:${colors[0]};--core-foreground:${colors[1]};--core-accent:${colors[2]};--core-accent-foreground:${accentText}}`;
        if ('adoptedStyleSheets' in this.root && 'replaceSync' in CSSStyleSheet.prototype) {
          if (!this.themeSheet) { this.themeSheet = new CSSStyleSheet(); this.root.adoptedStyleSheets = [...this.root.adoptedStyleSheets, this.themeSheet]; }
          this.themeSheet.replaceSync(text);
        } else { if (!this.themeStyle) { this.themeStyle = el('style'); this.root.append(this.themeStyle); } this.themeStyle.textContent = text; }
      }
      this.bar.classList.toggle('compact', config.density === 'compact');
      this.orgName.hidden = config.show_organization === false;
      if (config.logo_url && this.logo.getAttribute('src') !== config.logo_url) {
        try {
          const image = link('', config.logo_url);
          const url = new URL(image.href);
          if (url.search || url.hash) throw Error('Invalid logo');
          this.logo.hidden = false; this.logo.src = url.href;
        } catch { this.logo.removeAttribute('src'); this.logo.hidden = true; }
      } else if (!config.logo_url) { this.logo.removeAttribute('src'); this.logo.hidden = true; }
      const widgets = config.widgets || ['apps', 'pins', 'links', 'account', 'chat'];
      const nodes = {apps: this.appsButton, pins: this.pinned, links: this.sharedLinks, account: this.accountWidget, chat: this.chatButton, environment: this.environment};
      Object.entries(nodes).forEach(([key, node]) => node.hidden = !widgets.includes(key) || key === 'chat' && !state.chat.enabled);
      const widgetKey = widgets.join(',');
      if (this.widgetKey !== widgetKey) {
        this.widgetKey = widgetKey;
        this.bar.replaceChildren(this.identity, ...widgets.filter(key => nodes[key]).map(key => nodes[key]),
          ...Object.keys(nodes).filter(key => !widgets.includes(key)).map(key => nodes[key]), this.accountSlot);
      }
      this.environment.textContent = state.environment || 'Core';
      if (!this.hasAttribute('preview') && !this.drawer.hidden && !widgets.includes(this.panel === 'account' ? 'account' : this.panel === 'apps' ? 'apps' : 'chat')) this.closePanel();
    }
    renderState(state) {
        const previous = this.state;
        const barFocus = this.bar.contains(this.root.activeElement) ? this.root.activeElement :
          this.contains(document.activeElement) && document.activeElement !== this ? document.activeElement : null;
        this.state = state;
        this.hidden = !state.enabled;
        if (!state.enabled) { ++this.epoch; this.room = null; this.clearChat(); this.panelBody.replaceChildren(); this.closePanel(); if (barFocus) this.focusOpener(); return; }
        this.notice.hidden = true; this.notice.textContent = '';
        this.brandName.textContent = state.brand; this.orgName.textContent = state.organization_name;
        this.appearance(state);
        this.appsButton.disabled = false; this.accountButton.disabled = false;
        const appsChanged = JSON.stringify(previous?.applications) !== JSON.stringify(state.applications);
        if (appsChanged || JSON.stringify(previous?.pins) !== JSON.stringify(state.pins) || JSON.stringify(previous?.shortcuts) !== JSON.stringify(state.shortcuts)) {
          this.pinned.replaceChildren(); const byId = new Map(state.applications.map(row => [row.id, row]));
          const shown = new Set();
          (state.shortcuts || []).forEach(row => { this.pinned.append(link(row.name, row.url)); shown.add(row.id); });
          state.pins.forEach(id => { const row = byId.get(id); if (row && !shown.has(id)) this.pinned.append(link(row.name, row.url)); });
        }
        if (JSON.stringify(previous?.links) !== JSON.stringify(state.links)) {
          this.sharedLinks.replaceChildren();
          (state.links || []).forEach(row => {
            try { const anchor = link(row.label, row.url); if (row.new_tab) { anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; anchor.setAttribute('aria-description', 'Opens in a new tab'); } this.sharedLinks.append(anchor); } catch { /* Incomplete preview URL. */ }
          });
        }
        if (JSON.stringify(previous?.tools) !== JSON.stringify(state.tools)) this.tools.replaceChildren(...state.tools.map(row => link(row.name, row.url)));
        this.chatBadge.hidden = !state.chat.notifications; this.chatBadge.textContent = state.chat.notifications >= 200 ? '200+' : String(state.chat.notifications);
        this.chatButton.setAttribute('aria-description', `${state.chat.notifications} notifications, ${state.chat.unread} unread messages`);
        if (previous && (previous.chat.notifications !== state.chat.notifications || previous.chat.unread !== state.chat.unread)) this.announcements.textContent = `${state.chat.notifications} chat notifications, ${state.chat.unread} unread messages.`;
        if (this.room && !state.chat.rooms.some(row => row.id === this.room.id)) {
          this.room = null; ++this.epoch; this.clearChat(); this.showChatList(); this.status.textContent = 'Conversation access changed.';
        }
        if (this.panel === 'apps' && !this.drawer.hidden && appsChanged) this.showApps();
        if (this.panel === 'account' && !this.drawer.hidden && JSON.stringify(previous?.tools) !== JSON.stringify(state.tools)) this.showAccount();
        if (this.panel === 'chat' && !this.drawer.hidden && !this.room && JSON.stringify(previous?.chat) !== JSON.stringify(state.chat)) this.showChatList();
        if (barFocus) {
          const replacement = barFocus.isConnected ? barFocus : Array.from(this.bar.querySelectorAll('a')).find(link => link.href === barFocus.href);
          if (replacement && !replacement.disabled && replacement.getClientRects().length) replacement.focus({preventScroll: true});
          else this.focusOpener();
        }
    }
    toggle(panel) {
      if (this.hasAttribute('preview')) return;
      if (this.blocked || !this.state) return;
      if (!this.drawer.hidden && this.panel === panel) { this.closePanel(); return; }
      this.panel = panel; this.room = null; ++this.epoch; this.clearChat(); this.drawer.hidden = false;
      this.appsButton.setAttribute('aria-expanded', String(panel === 'apps')); this.chatButton.setAttribute('aria-expanded', String(panel === 'chat'));
      this.accountButton.setAttribute('aria-expanded', String(panel === 'account'));
      if (panel === 'apps') this.showApps(); else if (panel === 'account') this.showAccount(); else this.showChatList();
      this.close.focus();
    }
    focusOpener() {
      const trigger = this.panel === 'apps' ? this.appsButton : this.panel === 'account' ? this.accountButton : this.chatButton;
      const fallback = document.querySelector('main');
      if (!trigger.disabled && trigger.getClientRects().length && !trigger.hidden && !this.hidden) trigger.focus();
      else { if (fallback && !fallback.hasAttribute('tabindex')) fallback.tabIndex = -1; fallback?.focus(); }
    }
    closePanel(restoreFocus = true) {
      const wasOpen = !this.drawer.hidden;
      this.drawer.hidden = true; this.room = null; ++this.epoch; this.clearChat();
      this.appsButton.setAttribute('aria-expanded', 'false'); this.chatButton.setAttribute('aria-expanded', 'false');
      this.accountButton.setAttribute('aria-expanded', 'false');
      if (wasOpen && restoreFocus) this.focusOpener();
    }
    clearChat() {
      const active = this.root.activeElement;
      if (!this.drawer.hidden && (this.compose?.contains(active) || this.mode?.contains(active) || active === this.back)) this.close.focus();
      this.compose?.remove(); this.mode?.remove(); this.compose = null; this.mode = null; this.back.hidden = true; this.status.textContent = '';
      ['role', 'aria-live', 'aria-relevant', 'aria-atomic', 'aria-label'].forEach(name => this.panelBody.removeAttribute(name));
    }
    capturePanelFocus() {
      const active = this.root.activeElement;
      return this.panelBody.contains(active) ? {key: active.dataset.focusKey, selection: active.tagName === 'INPUT' ? [active.selectionStart, active.selectionEnd] : null} : null;
    }
    restorePanelFocus(focus) {
      if (!focus) return;
      const target = Array.from(this.panelBody.querySelectorAll('[data-focus-key]')).find(node => node.dataset.focusKey === focus.key);
      if (target) { target.focus({preventScroll: true}); if (focus.selection) target.setSelectionRange(...focus.selection); }
      else { this.close.focus(); this.status.textContent = 'This list changed. Review the available items.'; }
    }
    showAccount() {
      const focus = this.capturePanelFocus();
      this.panelTitle.textContent = 'Your account'; this.panelBody.replaceChildren(el('p', this.state.user.name));
      this.state.tools.forEach(tool => { const row = el('div', undefined, {class: 'row'}); const anchor = link(tool.name, tool.url); anchor.dataset.focusKey = tool.url; row.append(anchor); this.panelBody.append(row); });
      this.restorePanelFocus(focus);
    }
    showApps() {
      const focus = this.capturePanelFocus();
      const search = this.root.querySelector('.search')?.value || '';
      this.panelTitle.textContent = 'Your applications'; this.panelBody.replaceChildren();
      const input = el('input', undefined, {class: 'search', placeholder: 'Find an application', 'aria-label': 'Find an application', 'data-focus-key': 'app-search'}); input.value = search;
      const list = el('div'); this.panelBody.append(input, list);
      const render = () => {
        list.replaceChildren();
        this.state.applications.filter(row => row.name.toLowerCase().includes(input.value.toLowerCase())).forEach(row => {
          const item = el('div', undefined, {class: 'row'}); const app = link(row.name, row.url); app.dataset.focusKey = 'app-' + row.id;
          app.append(el('small', `${row.environment} · ${row.status}`));
          const button = el('button', row.pinned ? '★' : '☆', {'aria-label': `${row.pinned ? 'Unpin' : 'Pin'} ${row.name}`, 'aria-pressed': String(row.pinned), 'data-focus-key': 'pin-' + row.id});
          button.onclick = async () => { button.disabled = true; try { await this.request('pins/', {deployment_id: row.id, pinned: !row.pinned}); await this.refresh(); } catch (error) { this.status.textContent = error.message; } finally { button.disabled = false; } };
          item.append(app, button); list.append(item);
        });
        if (!list.childElementCount) list.append(el('p', 'No accessible application instances. An administrator can deploy apps and assign access.', {class: 'empty'}));
      };
      input.oninput = render; render();
      this.restorePanelFocus(focus);
    }
    showChatList() {
      const focus = this.capturePanelFocus();
      this.clearChat(); this.panelTitle.textContent = 'Chat and notifications'; this.panelBody.replaceChildren();
      const chat = this.state?.chat;
      if (!chat?.enabled) { this.panelBody.append(el('p', 'Chat is unavailable for this account or application.', {class: 'empty'})); this.restorePanelFocus(focus); return; }
      const workspace = link('Open full Chat workspace', chat.url); workspace.dataset.focusKey = 'chat-workspace'; this.panelBody.append(workspace);
      chat.rooms.forEach(room => {
        const row = el('div', undefined, {class: 'row'});
        const button = el('button', room.title, {class: 'room', 'aria-label': `Open header conversation ${room.title}`, 'data-focus-key': 'room-' + room.id});
        button.append(el('small', room.unavailable ? 'Authority unavailable' : `${room.kind} · ${room.unread} unread · ${room.notifications} notifications`));
        button.onclick = () => this.openRoom(room); row.append(button); this.panelBody.append(row);
      });
      if (!chat.rooms.length) this.panelBody.append(el('p', 'No joined conversations. Open the Chat workspace to start or join a conversation.', {class: 'empty'}));
      this.restorePanelFocus(focus);
    }
    openRoom(room) {
      this.room = room; ++this.epoch; this.messages = new Set(); this.cursor = null; this.ackCursor = 0;
      this.clearChat(); this.panelTitle.textContent = room.title; this.back.hidden = false; this.panelBody.replaceChildren();
      this.panelBody.setAttribute('role', 'log'); this.panelBody.setAttribute('aria-live', 'off'); this.panelBody.setAttribute('aria-relevant', 'additions'); this.panelBody.setAttribute('aria-atomic', 'false'); this.panelBody.setAttribute('aria-label', 'Conversation messages');
      this.back.focus();
      this.mode = el('label', 'Notify ', {class: 'mode'}); const mode = el('select', undefined, {'aria-label': 'Header conversation notifications'});
      [['all', 'All messages'], ['mentions', 'Mentions only'], ['muted', 'Muted']].forEach(([value, name]) => mode.append(el('option', name, {value})));
      mode.value = room.notification_mode || 'all'; mode.onchange = async () => { try { await this.request(`chat/${room.id}/preferences/`, {notifications: mode.value}); await this.refresh(); } catch (error) { this.status.textContent = error.message; } };
      this.mode.append(mode); this.drawer.insertBefore(this.mode, this.panelBody);
      this.compose = el('form', undefined, {class: 'compose', 'aria-label': 'Header chat composer', hidden: ''});
      this.text = el('textarea', undefined, {'aria-label': 'Header chat message', placeholder: 'Write a message…', maxlength: '16384', rows: '2', required: ''});
      const send = el('button', 'Send', {type: 'submit', 'aria-label': 'Send header chat message'}); this.compose.append(this.text, send); this.drawer.append(this.compose);
      let requestId = null;
      this.compose.onsubmit = async event => {
        event.preventDefault(); const epoch = this.epoch;
        if (this.blocked || !this.room || !this.text.value.trim()) return;
        const text = this.text; const outgoing = text.value; send.disabled = true; text.readOnly = true;
        requestId ||= crypto.randomUUID();
        try {
          const value = await this.request(`chat/${room.id}/send/`, {text: outgoing, request_id: requestId});
          if (this.blocked || epoch !== this.epoch) return;
          this.appendMessages([value.message]); text.value = ''; requestId = null; this.status.textContent = ''; await this.read(); await this.refresh();
        } catch (error) { if (!this.blocked && epoch === this.epoch) this.status.textContent = error.message; }
        finally { send.disabled = false; text.readOnly = false; }
      };
      this.text.onkeydown = event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); this.compose.requestSubmit(); } };
      this.panelBody.onscroll = () => this.acknowledge(); this.read();
    }
    appendMessages(messages) {
      const atBottom = this.panelBody.scrollHeight - this.panelBody.scrollTop - this.panelBody.clientHeight < 64;
      messages.forEach(message => {
        if (this.messages.has(message.id)) return; this.messages.add(message.id);
        const row = el('article', undefined, {class: 'message'});
        row.append(el('strong', message.sender_name || message.agent_name || 'Participant'), el('small', new Date(message.created).toLocaleString()), el('p', message.text));
        if (message.reply_preview) row.append(el('small', `Reply to: ${message.reply_preview}`));
        this.panelBody.append(row);
      });
      if (atBottom) this.panelBody.scrollTop = this.panelBody.scrollHeight;
    }
    async read() {
      const room = this.room, epoch = this.epoch;
      if (this.blocked || !room || this.reading === epoch || this.drawer.hidden || document.hidden) return;
      this.reading = epoch;
      try {
        const result = await this.request(`chat/${room.id}/messages/${this.cursor === null ? '' : '?after=' + this.cursor}`);
        if (this.blocked || epoch !== this.epoch || !this.isConnected) return;
        if (result.organization_id !== this.getAttribute('organization-id') || result.conversation_id !== room.id) { this.revoke(); return; }
        this.appendMessages(result.messages); this.cursor = result.next_cursor; this.panelBody.setAttribute('aria-live', 'polite');
        if (!result.can_send && this.compose.contains(this.root.activeElement)) this.close.focus();
        this.compose.hidden = !result.can_send;
        if (!result.can_send) this.status.textContent = 'Read-only conversation. Your message draft is preserved.';
        await this.acknowledge();
      } catch (error) {
        if (!this.blocked && epoch === this.epoch) {
          if (this.compose.contains(this.root.activeElement) || this.panelBody.contains(this.root.activeElement)) this.close.focus();
          this.panelBody.replaceChildren(); this.messages.clear(); this.cursor = null; this.compose.hidden = true; this.status.textContent = error.message;
        }
      } finally { if (this.reading === epoch) this.reading = null; }
    }
    async acknowledge() {
      const room = this.room, epoch = this.epoch;
      if (!room || this.blocked || this.acknowledging || document.hidden || this.drawer.hidden || !this.cursor ||
          this.cursor <= this.ackCursor || this.panelBody.scrollHeight - this.panelBody.scrollTop - this.panelBody.clientHeight > 64) return;
      this.acknowledging = true;
      try { await this.request(`chat/${room.id}/preferences/`, {cursor: this.cursor}); if (epoch === this.epoch && !this.blocked) this.ackCursor = this.cursor; }
      catch (error) { if (!this.blocked && epoch === this.epoch) this.status.textContent = error.message; }
      finally { this.acknowledging = false; }
    }
  }
  customElements.define('core-header', CoreHeader);
})();
