// App shell: hash router, status stack, navigation (with the client list), display preferences
// and status polling. Pages live in ./pages/*.js and export render(main, ctx) and optionally
// onStatus(status) to refresh themselves when the status changes.

import { connected, getJSON, onConnection, sayError } from './api.js';
import { watchTables } from './coltable.js';
import { h, icon } from './dom.js';
import * as overview from './pages/overview.js';
import * as review from './pages/review.js';
import * as clients from './pages/clients.js';
import * as secrets from './pages/secrets.js';
import * as settings from './pages/settings.js';
import * as session from './pages/session.js';
import * as logPage from './pages/log.js';
import * as wiki from './pages/wiki.js';
import * as setup from './pages/setup.js';
import { activityItems, noticeActivity, seen } from './activity.js';
import { begin, setTiming } from './timing.js';
import { confirmLeave, dirty } from './unsaved.js';
import { isSealed, openTunnel } from './sealed.js';
import { openFeedback } from './feedback.js';

const PAGES = { overview, review, clients, global: clients, secrets, settings, session, log: logPage, wiki, setup };

// Arriving on a page never scrolls it (DECISIONS 35): whoever came to stop a session would be
// chasing the Stop button. A reload used to put the browser's remembered position back - the
// Session page reloaded at the working band, whenever it had rendered tall enough in time.
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
const POLL_MS = 10000;

const app = {
  status: null,
  route: null,
  page: null,
};

// ---- preferences ------------------------------------------------------------------------

function readPref(key, fallback) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch (e) {
    return fallback;
  }
}

function writePref(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    /* storage unavailable: the choice lasts for this page only */
  }
}

const darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

function applyTheme(choice) {
  const dark = choice === 'dark' || (choice === 'system' && darkQuery && darkQuery.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.themeChoice = choice;
}

// A phone gets the top layout whatever was chosen: the side nav's 228px column left a 390px
// screen 162px of page (D48, D49, D66). Same query as prefs.js, which applies it before paint.
const NARROW = window.matchMedia ? window.matchMedia('(max-width: 40rem)') : null;

function applyLayout(layout) {
  const narrow = Boolean(NARROW && NARROW.matches);
  document.documentElement.dataset.layout = narrow || layout === 'top' ? 'top' : 'side';
}

// The addresses the page is served on locally. Anything else means it came through the tunnel.
const LOOPBACK_HOSTS = ['127.0.0.1', 'localhost', '[::1]', '::1'];

// Log out belongs only to the tunnel: Cloudflare Access answers /cdn-cgi/access/logout at its edge,
// and on this machine there is no Access in front, so the link would lead nowhere.
export function showsSignOut(hostname) {
  return Boolean(hostname) && !LOOPBACK_HOSTS.includes(String(hostname).toLowerCase());
}

// A clipped cell shows its whole value on hover (the CSS clips every table cell to its column).
// Set on first hover, and only where something was actually cut off.
//
// Never on anything inside a control (D53). A control names itself - `iconButton` gives it a
// title and an aria-label - and the innermost title is the one a browser shows. The emoji in a
// row button sits in a fixed 1.15em box, a glyph drawn wider than that overflows it, and this
// used to give the glyph a title of itself: hovering "Edit" showed a pencil.
// Links stay out of this list on purpose: a clipped link in a cell still wants its whole text.
const CONTROLS = 'button, input, select, textarea, summary';

function setupClipTitles() {
  document.addEventListener('mouseover', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const cell = target && target.closest('td');
    if (!cell || target.closest(CONTROLS)) return;
    for (const el of [target, cell]) {
      if (el.hasAttribute('title') || el.querySelector('input, select, textarea, button')) continue;
      if (el.scrollWidth > el.clientWidth) el.setAttribute('title', el.textContent.trim());
    }
  });
}

// Esc backs out of an open confirmation: its cancel button is the one that says Keep or Cancel.
function setupConfirmEscape() {
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const open = document.querySelector('td.actions .confirm');
    const cancel = open && open.querySelector('button[class^="cancel"], button[class*=" cancel"]');
    if (cancel) cancel.click();
  });
}

function setupSignOut() {
  const link = document.getElementById('signout');
  if (!link || !showsSignOut(window.location.hostname)) return;
  link.prepend(icon('signout'));
  link.hidden = false;
}

// The theme button shows the theme in use (the user, 2026-10-15): sun, moon, or half of each for
// "as the system does".
const THEME_ICON = { light: 'sun', dark: 'moon', system: 'sun-moon' };
const THEME_NAME = { light: 'Light', dark: 'Dark', system: 'As the system' };

function paintThemeButton(theme) {
  const cog = document.getElementById('cog');
  cog.replaceChildren(icon(THEME_ICON[theme] || 'sun-moon'));
  cog.title = `Theme: ${THEME_NAME[theme] || theme}`;
  cog.setAttribute('aria-label', cog.title);
}

// Menu layout (Side / Top) is a setting now (2026-10-15); Settings calls this to apply a choice.
export function setMenuLayout(layout) {
  writePref('aicleaner.layout', layout);
  applyLayout(layout);
}

function setupPrefs() {
  const cog = document.getElementById('cog');
  const panel = document.getElementById('prefs');
  const theme = readPref('aicleaner.theme', 'system');
  paintThemeButton(theme);
  panel.querySelector(`input[name=theme][value=${theme}]`).checked = true;
  const close = () => { panel.hidden = true; cog.setAttribute('aria-expanded', 'false'); };
  cog.addEventListener('click', (event) => {
    event.stopPropagation();
    panel.hidden = !panel.hidden;
    cog.setAttribute('aria-expanded', String(!panel.hidden));
  });
  panel.addEventListener('click', (event) => event.stopPropagation());
  document.addEventListener('click', close);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  panel.addEventListener('change', (event) => {
    const input = event.target;
    if (input.name === 'theme') {
      writePref('aicleaner.theme', input.value);
      applyTheme(input.value);
      paintThemeButton(input.value);
    }
  });
  if (darkQuery) {
    darkQuery.addEventListener('change', () => applyTheme(readPref('aicleaner.theme', 'system')));
  }
  // Turning a tablet, or narrowing a window, crosses the line both ways.
  if (NARROW) NARROW.addEventListener('change', () => applyLayout(readPref('aicleaner.layout', 'side')));
}

// ---- status stack and navigation ----------------------------------------------------------

export function clientHref(id) {
  return id === 'global' ? '#/global' : `#/clients/${encodeURIComponent(id)}`;
}

export function displayName(row) {
  return row.id === 'global' ? 'Global' : (row.label || row.id);
}

function problemRows(status) {
  const rows = [...(status.global ? [status.global] : []), ...status.clients];
  return rows.filter((r) => r.state.some((s) => ['broken', 'fallback', 'conflict-copy', 'held', 'missing'].includes(s)));
}

export function statusItems(status) {
  const red = [];
  // First and red: with bypass on nothing is filtered, so "Filtering on" would be untrue (D148).
  if (status.bypass) red.push({ level: 'red', text: 'Filtering OFF - bypass is on', href: '#/settings/bypass' });
  for (const row of [...(status.global ? [status.global] : []), ...status.clients]) {
    const name = displayName(row);
    const href = clientHref(row.id);
    // Written by a newer aicleaner (F14): the answer is an update, not a repair.
    if (row.state.includes('newer')) red.unshift({ level: 'red', text: `${name}: update aicleaner to read it`, href });
    else if (row.state.includes('broken')) red.unshift({ level: 'red', text: `${name}: file invalid`, href });
    // Its file is gone - unsynced, unshared, not downloaded - and last-good protects it (DECISIONS 67).
    else if (row.state.includes('missing')) red.unshift({ level: 'red', text: `${name}: client file missing`, href });
    else if (row.state.includes('fallback')) red.push({ level: 'red', text: `${name}: edit rejected`, href });
    if (row.state.includes('conflict-copy')) red.push({ level: 'red', text: `${name}: conflict copy`, href });
    // Changed without the client key (DECISIONS 51): last-good in use, someone has to look.
    if (row.state.includes('held')) red.push({ level: 'red', text: `${name}: a change is waiting for review`, href });
  }
  const items = [...red];
  // What the health check last found (feature 17). Every part of the local wiring can stop being
  // true without anyone noticing, and each one fails OPEN - the screenshot keeps working, it
  // just sends an image again. This is the one place that says so.
  const health = status.health;
  if (health && !health.stale) {
    for (const check of (health.checks || []).filter((c) => !c.ok)) {
      items.push({ level: 'red', text: check.why, href: '#/overview' });
    }
  } else if (health && health.stale) {
    // Said rather than hidden: a state file from three days ago describes a machine that has
    // been switched on since, and showing it as current would be worse than showing nothing.
    items.push({ level: 'amber', text: 'the wiring check has not run recently', href: '#/overview' });
  }
  // A project whose own .claude settings send Claude Code somewhere else (H10): its traffic never
  // reaches the filter, whatever the user settings say.
  for (const found of ((status.routing || {}).overrides || [])) {
    items.push({ level: 'red', text: `a project routes around aicleaner: ${found.file} ${found.why}`, href: '#/overview' });
  }
  // Remote MCP servers the audit found and nobody has allowed yet (H11): safe - they get tokens -
  // but a tool that needs the real name will not work until someone decides.
  const waiting = (status.mcp || []).filter((s) => s.approvable).length;
  if (waiting) {
    items.push({ level: 'amber', text: waiting === 1 ? '1 MCP server gets tokens until allowed'
      : `${waiting} MCP servers get tokens until allowed`, href: '#/overview' });
  }
  // Each client's folder is what is shared with its engagement in OneDrive (DECISIONS 51).
  if (status.clients_in_onedrive === false) {
    items.push({ level: 'amber', text: 'client files are not in OneDrive: nothing syncs to colleagues', href: '#/settings' });
  }
  // Folders other people on this PC could write to (H18): said, never enforced.
  for (const folder of status.open_folders || []) {
    items.push({ level: 'amber', text: `other users on this PC can write to ${folder}`, href: '#/overview' });
  }
  if (status.pending > 0) items.push({ level: 'amber', text: `${status.pending} to Review`, href: '#/review' });
  if (!items.length) items.push({ level: 'green', text: 'Filtering on', href: '#/overview' });
  // Work that landed while you were elsewhere (feature 44): under the filter's own state - which,
  // with the encryption badge above it, is what the stack is for (the user, 2026-10-13; they were
  // on top). Marked as notices, drawn without a frame, so a finished session's tick cannot be taken
  // for "Filtering on" when the menu is collapsed to its icons. Added after the check above, so a
  // notice never stands in for "Filtering on".
  return [...items, ...activityItems().map((notice) => ({ ...notice, notice: true }))];
}

const LEVEL_ICON = { green: 'check', amber: 'alert', red: 'cross' };

// Through the tunnel, always in view (the user, 2026-10-08): a closed lock and "End-to-end
// Encrypted" when the page talks in sealed requests, an open lock and "Traffic can be Observed" when
// it does not - Cloudflare ends the TLS connection at its edge, and an inspecting proxy on the way can
// read it too. Clicking it opens Settings at the Encrypted tunnel card. Hidden on the machine itself.
function renderTunnelState(status) {
  const badge = document.getElementById('tunnel-state');
  if (!badge) return;
  if (!status || !status.remote) { badge.hidden = true; return; }
  const sealed = isSealed();
  badge.hidden = false;
  badge.classList.toggle('sealed', sealed);
  badge.classList.toggle('open', !sealed);
  badge.title = sealed
    ? 'Everything this page sends and receives is encrypted between your browser and the aicleaner machine; Cloudflare sees only sealed envelopes. Click for details.'
    : 'Cloudflare decrypts this connection at its edge (SSL termination), and a proxy on the way that inspects HTTPS can read it too. Click to turn on the encrypted tunnel.';
  badge.replaceChildren(h('span', { class: 'tunnel-lock-icon', 'aria-hidden': 'true' }, icon(sealed ? 'padlock' : 'padlock-open')),
    h('span', { class: 'tunnel-lock-text' }, sealed ? 'End-to-end Encrypted' : 'Traffic can be Observed'));
}

function renderStatusStack(status) {
  const stack = document.getElementById('status-stack');
  stack.replaceChildren(...statusItems(status).map((item) =>
    h('a', { class: item.notice ? 'status notice' : 'status', href: item.href, title: item.text, dataset: { level: item.level } }, icon(LEVEL_ICON[item.level]), h('span', { class: 'status-text' }, item.text))));
}

// Secrets cannot be used at all through the tunnel (every /api/secrets call is local only), so its
// menu entry stays where it is, greyed and inert, and says why (the user, 2026-10-01).
const LOCAL_ONLY_ROUTES = { secrets: 'Secrets can only be added on the local machine' };

function renderLocalOnly(status) {
  for (const [route, why] of Object.entries(LOCAL_ONLY_ROUTES)) {
    const link = document.querySelector(`#nav a[data-route="${route}"]`);
    if (!link) continue;
    const off = Boolean(status.remote);
    link.classList.toggle('nav-disabled', off);
    link.setAttribute('aria-disabled', String(off));
    link.title = off ? why : '';
    if (off) { link.removeAttribute('href'); link.tabIndex = -1; }
    else { link.setAttribute('href', `#/${route}`); link.removeAttribute('tabindex'); }
  }
}

function renderNav(status) {
  renderLocalOnly(status);
  const count = document.querySelector('#nav a[data-route=review] .count');
  count.textContent = String(status.pending);
  count.hidden = !status.pending;
  const problems = new Set(problemRows(status).map((r) => r.id));
  const list = document.getElementById('nav-clients');
  // Global first, then the clients, then the way to make another one. Global is a row in this
  // list rather than a link beside it, so collapsing the list collapses it too.
  const rows = [{ id: 'global', label: 'Global' }, ...status.clients];
  list.replaceChildren(
    ...rows.map((row) =>
      h('a', { href: clientHref(row.id), class: problems.has(row.id) ? 'bad' : null, title: displayName(row),
        dataset: { clientId: row.id, route: row.id === 'global' ? 'global' : null } },
        problems.has(row.id) ? icon('cross', 'problem') : null,
        // Collapsed to the rail, the first two letters stand in for the name; the title has it whole
        // (the user, 2026-10-15).
        h('span', { class: 'nav-client-initial', 'aria-hidden': 'true' }, displayName(row).slice(0, 2)),
        h('span', { class: 'nav-client-name' }, displayName(row)))),
    // Not "add-client": the Clients page already has a button by that name, and two elements
    // with one id is a lookup that quietly finds the wrong one.
    navAdd('nav-add-client', 'Add new client', 'Add a new client filter'));
  const toggle = document.getElementById('clients-toggle');
  // Global lives in this list now, so being on it is a reason to have the list open.
  const inClients = app.route && (app.route.name === 'clients' || app.route.name === 'global');
  const hasClientProblem = [...problems].some((id) => id !== 'global');
  if (hasClientProblem || inClients) {
    list.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
  }
  renderNavSessions(status);
  document.getElementById('address').textContent = `${status.listen}:${status.port}`;
  markCurrent();
}

// "+ Add new ..." at the foot of a list; on the collapsed rail just "+", its action on hover (the
// user, 2026-10-15).
function navAdd(id, words, action) {
  return h('button', { type: 'button', id, class: 'nav-add', title: action, 'aria-label': action },
    h('span', { class: 'nav-add-plus', 'aria-hidden': 'true' }, '+'), h('span', { class: 'nav-add-text' }, ` ${words}`));
}

// The running sessions under Project Sessions, each a link to its chat (the user, 2026-10-06):
// switching session used to mean scrolling up to the picker and back down to the conversation.
// Redrawn only when the list changes, so a poll does not replace a link under the pointer.
function renderNavSessions(status) {
  const list = document.getElementById('nav-sessions');
  if (!list) return;
  const running = ((status.activity || {}).sessions || []).filter((s) => s.running && s.name)
    .sort((a, b) => a.name.localeCompare(b.name));
  const sig = running.map((s) => `${s.name}|${s.waiting ? 1 : 0}`).join('/');
  if (list.dataset.sig === sig) return;
  list.dataset.sig = sig;
  // The way to make another one, at the foot of the list as under the clients (the user, 2026-10-15).
  const add = navAdd('nav-add-session', 'Add new session', 'Add a new session: a project profile to name, give folders and start');
  list.replaceChildren(...running.map((s) => h('a', {
    href: `#/session/${encodeURIComponent(s.name)}/chat`, class: s.waiting ? 'waiting' : null,
    title: s.waiting ? `${s.name} is waiting on you` : `${s.name} - open its chat`,
    dataset: { session: s.name },
  }, h('span', { class: 'dot', 'aria-hidden': 'true' }),
  // Collapsed to the icon rail, the first letter stands in for the name (the user, 2026-10-13: the
  // sessions vanished); the title gives the whole name on hover.
  h('span', { class: 'nav-session-initial', 'aria-hidden': 'true' }, s.name.slice(0, 1).toUpperCase()),
  h('span', { class: 'nav-session-name' }, s.name))), add);
}

// The top menu's fly-outs (the user, 2026-10-15): in the top layout, the running sessions, the
// clients and the wiki's pages are panels under their link rather than rows that widen the bar. CSS
// shows a panel while its link or the panel is hovered (or focused); this lines each one up under its
// link, and fills the wiki's list once.
function setupTopFlyouts() {
  const nav = document.getElementById('nav');
  const pairs = [
    [document.getElementById('group-sessions'), document.getElementById('nav-sessions')],
    [document.getElementById('group-clients'), document.getElementById('nav-clients')],
    [nav.querySelector('a[data-route=wiki]'), document.getElementById('nav-wiki')],
  ];
  const place = (trigger, panel) => {
    if (!trigger || !panel || document.documentElement.dataset.layout !== 'top') return;
    const room = window.innerWidth - nav.getBoundingClientRect().left - 8;
    const left = Math.max(0, Math.min(trigger.offsetLeft, room - panel.offsetWidth));
    panel.style.left = `${left}px`;
  };
  for (const [trigger, panel] of pairs) {
    if (!trigger || !panel) continue;
    for (const kind of ['mouseenter', 'focusin']) trigger.addEventListener(kind, () => place(trigger, panel));
  }
  // The clients panel also opens on its caret, as it always has; it lines up the same way.
  const caret = document.getElementById('clients-toggle');
  if (caret) caret.addEventListener('click', () => requestAnimationFrame(() => place(pairs[1][0], pairs[1][1])));
  const wiki = document.getElementById('nav-wiki');
  if (wiki) {
    getJSON('/api/wiki-index').then((answer) => {
      wiki.replaceChildren(...(answer.pages || []).map((page) => h('a', { href: `#/wiki/${encodeURIComponent(page)}` },
        page.replace(/-/g, ' '))));
    }).catch(() => {});
  }
  // Feedback (2026-10-16): its own menu item, in every layout. A dialog, not a page, so it opens over
  // whatever is being looked at and can say which page and field that was.
  const feedbackItem = document.getElementById('nav-feedback');
  if (feedbackItem) {
    feedbackItem.addEventListener('click', (event) => { event.preventDefault(); openFeedback(); });
  }
}

// "+ Add new session": the Session page, with the profile editor open on a new profile. Delegated,
// as the list is rebuilt when the running sessions change. Said through sessionStorage so it works
// whether the Session page is already showing or is drawn by the navigation.
function setupAddSession() {
  const list = document.getElementById('nav-sessions');
  if (!list) return;
  list.addEventListener('click', (event) => {
    if (!event.target.closest('#nav-add-session')) return;
    event.preventDefault();
    try { sessionStorage.setItem('aicleaner.addSession', '1'); } catch (e) { /* the event still does it */ }
    if (app.route && app.route.name === 'session') window.dispatchEvent(new CustomEvent('aicleaner:add-session'));
    else window.location.hash = '#/session';
  });
}

function markCurrent() {
  for (const link of document.querySelectorAll('#nav a')) link.removeAttribute('aria-current');
  if (!app.route) return;
  if (app.route.name === 'clients' && app.route.id) {
    const link = document.querySelector(`#nav-clients a[data-client-id="${CSS.escape(app.route.id)}"]`);
    if (link) link.setAttribute('aria-current', 'page');
    return;
  }
  const link = document.querySelector(`#nav a[data-route="${app.route.name}"]`);
  if (link) link.setAttribute('aria-current', 'page');
  const shown = app.route.name === 'session' && app.page && typeof app.page.currentSession === 'function'
    ? app.page.currentSession() : '';
  for (const entry of document.querySelectorAll('#nav-sessions a')) {
    if (shown && entry.dataset.session === shown) entry.setAttribute('aria-current', 'page');
  }
}

// The nav collapses to an icon rail, remembered between visits (feature 55). `prefs.js` applies
// the saved value before first paint; this only has to flip it and write it down.
function setupNavToggle() {
  const toggle = document.getElementById('nav-toggle');
  if (!toggle) return;
  const say = () => {
    const rail = document.documentElement.getAttribute('data-nav') === 'rail';
    const label = rail ? 'Expand the menu' : 'Collapse the menu';
    toggle.setAttribute('aria-label', label);
    toggle.title = label;
    toggle.setAttribute('aria-expanded', String(!rail));
  };
  say();
  toggle.addEventListener('click', () => {
    const rail = document.documentElement.getAttribute('data-nav') === 'rail';
    document.documentElement.setAttribute('data-nav', rail ? 'full' : 'rail');
    try {
      localStorage.setItem('aicleaner.nav', rail ? 'full' : 'rail');
    } catch (e) { /* private window or policy: it just will not be remembered */ }
    say();
  });
}

// The running sessions fold away on their caret, as the clients do; open unless folded on this
// browser before (the user, 2026-10-15: open by default).
function setupSessionsToggle() {
  const toggle = document.getElementById('sessions-toggle');
  const list = document.getElementById('nav-sessions');
  if (!toggle || !list) return;
  toggle.append(icon('chevron'));
  const paint = () => {
    const open = !list.hidden;
    toggle.setAttribute('aria-expanded', String(open));
    const what = open ? 'Hide running sessions' : 'Show running sessions';
    toggle.setAttribute('aria-label', what);
    toggle.title = what;
  };
  list.hidden = readPref('aicleaner.sessions-open', 'yes') === 'no';
  paint();
  toggle.addEventListener('click', (event) => {
    event.stopPropagation();
    list.hidden = !list.hidden;
    writePref('aicleaner.sessions-open', list.hidden ? 'no' : 'yes');
    paint();
  });
}

function setupClientsToggle() {
  const toggle = document.getElementById('clients-toggle');
  const list = document.getElementById('nav-clients');
  toggle.append(icon('chevron'));
  // Its name says what a click does (the user, 2026-10-01): it read "Show clients" while they showed.
  const name = () => {
    const what = list.hidden ? 'Show clients' : 'Hide clients';
    toggle.setAttribute('aria-label', what);
    toggle.title = what;
  };
  name();
  toggle.addEventListener('click', (event) => {
    event.stopPropagation();
    list.hidden = !list.hidden;
    toggle.setAttribute('aria-expanded', String(!list.hidden));
    name();
  });
  // Delegated: the list is rebuilt on every status poll, so a handler on the button itself would
  // last until the next one.
  list.addEventListener('click', async (event) => {
    if (event.target.closest('#nav-add-client')) {
      event.preventDefault();
      const { askForNewClient } = await import('./pages/newclient.js');
      askForNewClient();
      return;
    }
    if (document.documentElement.dataset.layout === 'top') {
      list.hidden = true;
      toggle.setAttribute('aria-expanded', 'false');
    }
  });
}

// ---- status polling -------------------------------------------------------------------------

export async function refreshStatus() {
  try {
    app.status = await getJSON('/api/status');
    setTiming(app.status.debug_timings);      // Diagnostics (feature 91)
  } catch (error) {
    sayError(error);
    return app.status;
  }
  // Pages that talk about folders on the proxy's machine name it rather than saying "this".
  window.aicleanerHost = (app.status && app.status.host) || '';
  const shown = app.page && typeof app.page.currentSession === 'function' ? app.page.currentSession() : '';
  noticeActivity(app.status.activity, app.route, shown);
  seen(app.route, shown);
  renderStatusStack(app.status);
  renderTunnelState(app.status);
  renderNav(app.status);
  if (app.page && typeof app.page.onStatus === 'function') app.page.onStatus(app.status);
  return app.status;
}

// ---- router -------------------------------------------------------------------------------

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  let name = parts[0] || 'overview';
  if (!Object.prototype.hasOwnProperty.call(PAGES, name)) name = 'overview';
  let id = null;
  if (name === 'global') id = 'global';
  // A second part names a client, or on a page of cards the card to open at. Settings has
  // eighteen of them: a refusal that says "set it under Settings" and leaves you to find which
  // card it meant is only half an answer.
  else if (name === 'clients' || name === 'settings') id = parts[1] || null;
  // ... and on the Project Sessions page, the session to open on (D118), and `/chat` to go
  // straight to its conversation - what a notice and a menu entry ask for (2026-10-06).
  else if (name === 'session') id = parts[1] || null;
  // ... and on the Wiki, the page (2026-10-07).
  else if (name === 'wiki') id = parts[1] || null;
  const chat = name === 'session' && parts[2] === 'chat';
  return { name, id, chat };
}

// The address a page was drawn for, so a guarded move can be put back (D98).
let shownHash = location.hash;

// hashchange fires after the address has changed. With unsaved changes on the page, the old
// address goes back (replaceState fires no event), the person is asked, and Leave sets the new
// address again - by then nothing is unsaved, so it routes.
async function guardedRoute() {
  const wanted = location.hash;
  if (wanted === shownHash) return;
  if (dirty()) {
    history.replaceState(null, '', shownHash || '#/');
    if (!(await confirmLeave())) return;
    location.hash = wanted;
    return;
  }
  await route();
}

async function route() {
  shownHash = location.hash;
  // A page may hold something open (the session page has an event stream); let it close it.
  if (app.page && typeof app.page.leave === 'function') app.page.leave();
  app.route = parseRoute();
  app.page = PAGES[app.route.name];
  const main = document.getElementById('page');
  main.replaceChildren();
  // Which route is showing, so the stylesheet can give the ones that want width the width. The
  // 1280px measure suits prose and forms; it does not suit a table of several hundred entries,
  // and the two controls in the toolbar sat out past the right edge of every card beneath them
  // because the toolbar had no such cap (37).
  document.documentElement.dataset.route = app.route.name;
  markCurrent();
  if (!app.status) await refreshStatus();
  // How long a page takes to draw from its address (feature 91) - the menu's session links come
  // this way, rebuilding the whole Session page, where the Active Sessions buttons only switch.
  const drawn = begin('route', { page: app.route.name });
  try {
    await app.page.render(main, { route: app.route, status: app.status, refresh: refreshStatus });
  } catch (error) {
    sayError(error);
  }
  drawn.end();
  if (app.status) renderNav(app.status);
  // Arriving where a notice points is looking at it (44): the entry goes now, not a poll later.
  if (app.status) {
    seen(app.route, typeof app.page.currentSession === 'function' ? app.page.currentSession() : '');
    renderStatusStack(app.status);
  }
}

async function start() {
  // Through the encrypted tunnel, nothing reaches the API until the key is right (feature 21).
  // A download fetched through the tunnel that failed (sealed.js has no message stack of its own).
  window.addEventListener('aicleaner:error', (event) => sayError(event.detail));
  await openTunnel();
  setupClipTitles();
  setupConfirmEscape();
  setupSignOut();
  setupPrefs();
  setupNavToggle();
  setupClientsToggle();
  setupAddSession();
  setupSessionsToggle();
  setupTopFlyouts();
  watchTables(document.getElementById('page'));
  window.addEventListener('hashchange', guardedRoute);
  // A page that rewrote the address for what it shows (the session page, D118) has already drawn
  // it: that address is the one shown, so a later move to it is not mistaken for nothing to do.
  window.addEventListener('aicleaner:address', (event) => {
    shownHash = event.detail;
    // Switching session inside the page is looking at it too: its notice and toast go.
    if (app.status && app.page && typeof app.page.currentSession === 'function') {
      seen(parseRoute(), app.page.currentSession());
      renderStatusStack(app.status);
      markCurrent();
    }
  });
  await refreshStatus();
  await route();
  // Only while aicleaner answers (D138): during a restart the link's own probe asks, once every few
  // seconds, and the status comes back the moment it does.
  setInterval(() => { if (document.visibilityState === 'visible' && connected()) refreshStatus(); }, POLL_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && connected()) refreshStatus();
  });
  onConnection((up) => { if (up) refreshStatus(); });
}

start();
