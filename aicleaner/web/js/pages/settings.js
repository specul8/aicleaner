// Settings: cards, each saved on its own (logging, notifications, auto-add, tool arguments),
// import and export, and read-only contact and advanced details. Secrets mode lives on the
// Secrets page only.

import { getJSON, postJSON, say, sayError } from '../api.js';
import { h, icon, kindLabel, savingButton, shortTime } from '../dom.js';
import { displayName, setMenuLayout } from '../app.js';
import { rebase, track } from '../unsaved.js';
import { forgetKey, isSealed } from '../sealed.js';
import { forgetPreviewChoice } from '../previewjump.js';
import { openFeedback } from '../feedback.js';

const view = { main: null, ctx: null, config: null, host: null, cards: [], watching: false };

// Below this the page is one column. Kept in step with the media query in app.css.
const TWO_COLUMNS_FROM = 1000;

// Cards are dealt across the columns in order - 1 left, 2 right, 3 left - so the page reads the
// way it always has. What changes is that each column then packs on its own, in a flex column
// with one fixed gap: a grid builds rows, and a row is as tall as its tallest card, so a short
// card beside a long one left a pocket of dead space and the gap between two cards depended on
// what happened to be next to them.
//
// Narrow, it is one column, and the dealing has to be redone rather than left to CSS: with the
// cards already split between two elements, collapsing them would read 1, 3, 5, then 2, 4, 6.
function dealCards(host, cards) {
  const count = window.innerWidth >= TWO_COLUMNS_FROM ? 2 : 1;
  const columns = Array.from({ length: count }, () => h('div', { class: 'settings-column' }));
  cards.filter(Boolean).forEach((section, index) => columns[index % count].append(section));
  host.replaceChildren(...columns);
}

// #/settings/<card> opens at that card. A refusal elsewhere can then link straight to the control
// that answers it, instead of naming a card among eighteen and leaving you to scroll for it - the
// folders list sits two thirds of the way down one of two columns, which is how it got reported as
// missing. Unknown names are ignored, so an old link is never worse than the plain page.
function showCard(name) {
  if (!name || !view.host) return;
  const section = view.host.querySelector(`section[data-card="${CSS.escape(name)}"]`);
  if (!section) return;
  section.scrollIntoView({ block: 'center' });
  section.classList.add('found');
  // Long enough to catch the eye on a page you have just landed on, then gone: a highlight that
  // stays becomes part of the furniture and stops meaning anything.
  setTimeout(() => section.classList.remove('found'), 2500);
  const field = section.querySelector('textarea, input, select');
  if (field) field.focus({ preventScroll: true });
}

// The cards that are red only while their switch is on, and what each one says when it is.
const DANGEROUS = [
  ['bypass', 'proxy.bypass', 'BYPASS IS ON: NOTHING IS FILTERED'],
  ['fail-open', 'canary.fail_open', 'FAIL-OPEN IS ON: THE CANARY IS NOT STOPPING ANYTHING'],
  ['images', 'attachments.allow_images', 'IMAGES ARE SENT UNFILTERED'],
];

// Updated in place after a save rather than by rebuilding the page: a rebuild detaches every
// other card, losing whatever was half typed into one of them. The indicator still has to be
// live, though - a danger sign that describes the last page load is worse than none.
function refreshDangerous() {
  if (!view.host) return;
  for (const [key, setting, saying] of DANGEROUS) {
    const section = view.host.querySelector(`section[data-card="${key}"]`);
    if (!section) continue;
    const active = Boolean(view.config.settings[setting]);
    section.classList.toggle('bad', active);
    section.classList.toggle('emergency', active);
    const chip = section.querySelector(':scope > p.chip.bad');
    if (active && !chip) section.prepend(h('p', { class: 'chip bad' }, saying));
    if (!active && chip) chip.remove();
  }
}

function paintCards() {
  const s = view.config.settings;
  const status = view.ctx.status;
  const kinds = view.config.kinds || [];
  // The same page near and far while the filter is being debugged (see ui.REMOTE_SETTINGS).
  const everyday = [
    view.config.remote ? null : setupCard(),
    menuLayoutCard(),
    loggingCard(s), notificationsCard(s, Boolean(view.config.remote)), kindsCard(s, kinds),
    nearMissCard(s), sessionCard(s), tunnelCard(s), highlightCard(s),
    view.config.windows ? bashGuardCard(s) : null,
    view.config.windows && !view.config.remote ? codeTabCard(s) : null,
    toolsCard(s), routingCard(), restartCard(), updatesCard(s), feedbackCard(),
    foldersCard(s), scanMarkersCard(s),
    uninspectedCard(view.config.allow_non_json_paths), importExportCard(status), publicListCard(),
    contactCard(status.contact), editorCard(s), autocorrectCard(s), diagnosticsCard(s, status),
    advancedCard(status, view.config, view.config.dialects || []),
  ];
  // Last, so each lands at the foot of its column and none of them is the first thing the page
  // offers. Every one turns a protection off - forwarding untouched, sending pictures unfiltered,
  // sending what the canary would refuse - and a control like that should take deciding to reach,
  // not be sitting at the top where a settings page is usually skimmed. They still turn red and
  // say so when they are on (DANGEROUS, above), which is the half that has to be impossible to miss.
  const dangerous = [bypassCard(s), imagesCard(s), failOpenCard(s)];
  view.cards = [...everyday, ...dangerous].filter(Boolean);   // a card with nothing to show is null
  dealCards(view.host, view.cards);
}

// A button per card at the top of the page (the user, 2026-10-15: "too hard to find things"): its
// title on the button, its description on hover, and a click goes straight to it - as a link to
// #/settings/<card> does. In the page's own order, so the dangerous ones are last here too.
function paintJump() {
  if (!view.jump) return;
  view.jump.replaceChildren(...view.cards.map((section) => {
    const key = section.dataset.card;
    const title = (section.querySelector(':scope > h2') || {}).textContent || key;
    const hint = (section.querySelector(':scope > p.hint') || {}).textContent || '';
    const button = h('button', { type: 'button', class: `chip settings-jump-to${section.classList.contains('bad') ? ' bad' : ''}`,
      dataset: { card: key }, title: hint || title }, title);
    button.addEventListener('click', () => showCard(key));
    return button;
  }));
}

// Where the menu sits (the user, 2026-10-15: moved here from the header's display button). A choice
// for this browser, kept in its storage like the theme, so it applies at once and needs no Save. A
// phone has the menu across the top whatever is chosen (D48).
function menuLayoutCard() {
  let current = 'side';
  try { current = localStorage.getItem('aicleaner.layout') === 'top' ? 'top' : 'side'; } catch (e) { /* default */ }
  const choice = (value, label) => h('label', {}, h('input', { type: 'radio', name: 'menu-layout', value,
    checked: current === value ? true : null }), label);
  const group = h('div', { class: 'segmented' }, choice('side', 'Side'), choice('top', 'Top'));
  group.addEventListener('change', (event) => {
    setMenuLayout(event.target.value);
    say(`The menu is ${event.target.value === 'top' ? 'across the top' : 'down the side'} now (this browser).`, 'ok');
  });
  return card('menu-layout', 'Menu layout', 'Where the menu sits: down the side, or across the top. For this browser; applies at once.',
    [group], null);
}

// The setup wizard (feature 106), opened again from here: on this machine only, as it runs its scripts.
function setupCard() {
  return card('setup', 'Set up', 'Step by step: route your AI tools through aicleaner, add a client, and set up remote access.',
    [h('div', { class: 'form-row' }, h('a', { class: 'btn primary', href: '#/setup' }, 'Open the setup wizard'))], null);
}

function card(key, title, hint, fields, onSave, afterSave = null) {
  // The confirmation belongs beside the button, not in the toolbar: this page is taller than the
  // window, so a message at the top was off-screen for every card below the fold.
  const { button: save, note } = savingButton('Save', async () => {
    const before = view.config.settings;
    const result = await postJSON('/api/config', { settings: onSave() });
    view.config.settings = result.settings;
    if (afterSave) afterSave(before, result.settings);
    // The status bar reflects a change at once (D148), not at the next ten-second poll.
    if (view.ctx && view.ctx.refresh) view.ctx.refresh().catch(() => {});
    refreshDangerous();
    // When the save also rewrote CLAUDE.md, say which way and to which file (feature 36). The
    // server sends nothing here unless it actually touched it, so this stays quiet otherwise.
    if (result.rules) say(result.rules, result.rules.startsWith('the setting was saved') ? 'warn' : 'ok');
    rebase(section);                    // what is on screen is what is stored now (D98)
  });
  const section = h('section', { class: 'card', dataset: { card: key } }, h('h2', {}, title),
    hint ? h('p', { class: 'hint' }, hint) : null, ...fields,
    onSave ? h('div', { class: 'form-row' }, save, note) : null);
  if (onSave) {
    lockIfLocalOnly(section, Object.keys(onSave()));
    track(section, onSave);             // leaving with it changed and not saved asks first (D98)
  }
  return section;
}

// Through the tunnel, a card whose settings can only be changed on the machine itself is greyed
// out and says why, instead of taking an edit and refusing it on Save (D50). The server's refusal
// stays - the page is not the boundary - this is the page agreeing with it. The allow-list already
// comes with the config (`remote`, `remote_settings`); nothing new crosses the wire.
const LOCAL_ONLY = 'Can only be changed where aicleaner is deployed';

function lockIfLocalOnly(section, keys) {
  const config = view.config || {};
  if (!config.remote || !keys.length) return;
  const allowed = new Set(config.remote_settings || []);
  if (keys.some((k) => allowed.has(k))) return;
  section.classList.add('local-only');
  section.title = LOCAL_ONLY;
  for (const control of section.querySelectorAll('input, select, textarea, button')) {
    control.disabled = true;
    control.title = LOCAL_ONLY;
  }
  section.querySelector('h2').after(h('p', { class: 'chip local-only-note' }, LOCAL_ONLY));
}

function checkbox(name, label, checked) {
  return h('label', { class: 'check' }, h('input', { type: 'checkbox', name, checked: Boolean(checked) }), ' ', label);
}

// A card that switches something dangerous off and on is amber at rest and red while it is on,
// with the state said in words at the top. These cards used to be red either way, which is the
// same as not being red at all: a warning that is always showing is scenery.
function danger(section, active, saying) {
  section.classList.add('warn');
  if (!active) return section;
  section.classList.add('bad', 'emergency');
  section.prepend(h('p', { class: 'chip bad' }, saying));
  return section;
}

function loggingCard(s) {
  const detail = checkbox('detail', 'Write the substitution log', s['logs.detail']);
  const every = checkbox('detail-every', 'Log every value on every request (diagnosis)', s['logs.detail_every_request']);
  const retention = h('input', { type: 'number', name: 'retention', min: '0', value: String(s['logs.detail_retention_days']) });
  return card('logging', 'Logging',
    'The substitution log records what was replaced and by what, whose it is, what was done, and who was driving - real names, kept on this machine, read on the Log page. A value is logged the first time a session sends it and again when what happens to it changes. The evidence log (counts only) is always written.',
    [h('div', { class: 'form-row' }, detail, every), h('div', { class: 'form-row' },
      h('label', { class: 'field' }, 'Keep substitution log files for (days; 0 keeps them forever)', retention))],
    () => ({ 'logs.detail': detail.querySelector('input').checked,
      'logs.detail_every_request': every.querySelector('input').checked,
      'logs.detail_retention_days': Number(retention.value) }));
}

function notificationsCard(s, remote) {
  // Learning mode is detection itself, so through the tunnel it is shown but not offered (H3).
  const learning = remote ? null : checkbox('learning', 'Learning mode (find identifiers the mapping does not know)', s['detect.enabled']);
  const footer = checkbox('footer', "Footer on the model's reply", s['detect.footer']);
  const toast = checkbox('toast', 'Windows toast', s['detect.toast']);
  return card('notifications', 'Notifications', 'How you hear about new identifiers to review.',
    [learning ? h('div', { class: 'form-row' }, learning) : null, h('div', { class: 'form-row' }, footer, toast)],
    () => ({ ...(learning ? { 'detect.enabled': learning.querySelector('input').checked } : {}),
      'detect.footer': footer.querySelector('input').checked, 'detect.toast': toast.querySelector('input').checked }));
}

function imagesCard(s) {
  const allow = checkbox('allow_images', 'Send images to the AI service', s['attachments.allow_images']);
  const section = card('images', 'Images',
    'Off (recommended): the image itself is never sent. Its text is read on this machine, rows and columns intact, and that text is filtered like anything else. On: JPEG, PNG, GIF and WebP are sent as they are, unfiltered, so any client name in them reaches the provider (replies are still filtered). Other image types are always withheld. PDFs and Office documents are read to text and filtered either way; a scanned PDF is read page by page.',
    [h('div', { class: 'form-row' }, allow),
     // Saying this plainly is part of the decision to do it at all (feature 36). A proxy that
     // edits the agent's own instructions from a settings page should never be a surprise.
     h('p', { class: 'hint' },
       'This switch also rewrites your user-level CLAUDE.md. The setting governs the pipe and '
       + 'that file governs the agent, and only the file is in front of the model - so when they '
       + 'disagreed, the agent went on refusing to open a screenshot the proxy would have passed, '
       + 'and this setting appeared to do nothing. Off: the "convert locally, never send '
       + 'binaries" rule is written into the block between the aicleaner markers. On: the rule is '
       + 'removed. Nothing outside those markers is touched, a backup is taken first, and a file '
       + 'whose markers are damaged is left alone and reported.')],
    () => ({ 'attachments.allow_images': allow.querySelector('input').checked }));
  // On, this one sends pictures to the provider unfiltered, which is the same class of thing.
  return danger(section, s['attachments.allow_images'], 'IMAGES ARE SENT UNFILTERED');
}

function nearMissCard(s) {
  const on = checkbox('near_miss', 'Correct misreadings in text read from images and documents',
    s['detect.near_miss']);
  return card('near-miss', 'Misreadings',
    'An OCR engine turns rn into m and l into I, and a mangled client name would walk straight past the filter. With this on, a word one letter away from a mapped identifier is corrected back to it - but only in text read from an image, a scan or a document, which is the only place a misreading is expected. Typed text is never touched: there, almost every match would be an ordinary long word being replaced by a client token.',
    [h('div', { class: 'form-row' }, on)],
    () => ({ 'detect.near_miss': on.querySelector('input').checked }));
}

// Read-only on purpose. An entry here is not a setting to tune, it is a hole to notice, and the
// card is red while one exists so it cannot sit in a config file unremarked.
// The folders every page may reach: a scan, a session's project folders, a client's share.
//
// Settable from either side since 2026-09-27. It used to be local-only on the grounds that
// reaching an allowed folder from away is the feature while deciding what is allowed is not -
// but the case that changed it is a real one: realising at a client site that a folder holding
// client content was never added, with no way to fix it until you are back at the desk.
//
// De-risked by a refusal rather than a lock. A system folder - Program Files, ProgramData,
// Windows - is refused by the API from either side, because client work is never in one and a
// hurried entry from a train is exactly how one ends up behind the picker.
function foldersCard(s) {
  const area = h('textarea', { name: 'roots', rows: '3', 'aria-label': 'Folders the pages may reach',
    placeholder: 'C:\\Users\\you\\OneDrive' });
  area.value = (s['folders.roots'] || []).join('\n');
  // Folders already in use when this list arrived stay reachable, because switching the setting on
  // must not stop a session that works today. They are listed here rather than written into the
  // setting, so the list is something somebody chose and not something this code guessed.
  const inUse = h('div', { class: 'in-use-folders' });
  getJSON('/api/folders/roots').then((info) => {
    if (!info.in_use || !info.in_use.length) return;
    inUse.replaceChildren(
      h('p', { class: 'hint' }, 'Also reachable because a profile or a share already points at them. '
        + 'Add one above to keep it when that profile or share changes.'),
      h('ul', { class: 'small muted' }, info.in_use.map((p) => h('li', { class: 'mono' }, p))));
  }).catch(() => {});
  const hint = 'One full path per line. A folder picker can reach these and everything inside them, '
    + 'and can walk down to them from the drive without showing anything else on the way. Empty '
    + 'means no folder can be chosen at all, which is the safe way round: before this list existed, '
    + 'a typed path reached anything this Windows account could read.';
  const refused = h('p', { class: 'hint' },
    'System folders are refused from either side: Program Files, Program Files (x86), ProgramData '
    + 'and Windows, and anything inside them. Client work is never in one, and a root there would '
    + 'put every file under it behind the picker.');
  return card('folders', 'Folders the pages may reach', hint,
    [h('div', { class: 'form-row' }, area), inUse, refused],
    () => ({ 'folders.roots': area.value.split('\n').map((line) => line.trim()).filter(Boolean) }));
}

// Which folders inside a repository hold output rather than something somebody wrote. A scan reads
// them either way, but only keeps a find that visibly belongs to a client.
function scanMarkersCard(s) {
  const area = h('textarea', { name: 'markers', rows: '3', 'aria-label': 'Machine-written folders',
    placeholder: 'scripts/output\ndiaglogs-\nintune-portal-export' });
  area.value = (s['scan.generated_markers'] || []).join('\n');
  return card('scan-markers', 'Folders holding machine-written output',
    'One path fragment per line, matched inside the folder being scanned. In a file a machine wrote - '
    + 'a diagnostic dump, a portal export, a capture - a scan only keeps what carries a name the '
    + 'mapping already knows, because a bare GUID or address in one of those belongs to whoever made '
    + 'the tool, not to your client. Scanning one repository this way went from 1,442 entries to 480 '
    + 'while keeping every one that named the client. Empty means the built-in list; a scan says how '
    + 'much it held back, so a missing client name shows up as a number rather than as silence.',
    [h('div', { class: 'form-row' }, area)],
    () => ({ 'scan.generated_markers': area.value.split('\n').map((line) => line.trim()).filter(Boolean) }));
}

function uninspectedCard(paths) {
  const listed = (paths || []);
  const body = listed.length
    ? [h('ul', { class: 'bad-list' }, ...listed.map((p) => h('li', { class: 'mono' }, p)))]
    : [h('p', { class: 'chip ok' }, 'Empty: every request with a body is inspected.')];
  const section = card('uninspected', 'AI endpoints forwarded with no inspection at all',
    'The filter works on text it can find inside a parsed body, so a request whose body is not JSON - a file upload, a form post, anything binary - is refused rather than sent unread. An endpoint listed here is exempt, and exempt means nothing is done to it: no names replaced, no identifiers detected, nothing held back, and no canary check before it leaves. It is the only path through this proxy with no safety net under it. The exemption is per endpoint and never per file, so listing an upload endpoint exempts every upload through it, not the one you had in mind. Set it in aicleaner-config.json (allow_non_json_paths) and restart; there is no control here because this is not a setting to tune.',
    body, null);
  if (listed.length) section.classList.add('bad');
  return section;
}

// What happens to something new, per kind (walkthrough topic 9, DECISIONS 46). One card in place of
// "Hold back as placeholders" and "Kinds automatically tokenised", which interacted in a way the page
// never showed. Stored in the same settings: quiet_kinds = Auto-tokenise, placeholder_kinds =
// Tokenise & Review, neither = Ignore; owner_add_kinds (and, for a resource name,
// auto_map_client_resources) = the Auto-add ticks, which only apply with Tokenise & Review.
const MODES = [['auto', 'Auto-tokenise'], ['review', 'Tokenise & Review'], ['ignore', 'Ignore']];
// Kinds whose owner the value itself can prove, so Auto-add means something.
const OWNED_KINDS = ['email', 'tenant', 'sharepoint', 'domain', 'devops-org', 'devops-project', 'client-resource'];
const EMAIL_IGNORE_WARNING = 'An address at a client\'s mapped domain still has its domain replaced, but the name before the @ goes to the AI service as written.';
// What proves a value is a particular client's, where it can be said in a phrase.
const OWNER_EXAMPLES = {
  email: 'an address at a client\'s mapped domain',
  sharepoint: 'a SharePoint host of a client\'s tenant',
};

// The net effect of the choices on one row, in words (the user, 2026-10-06: a ? beside each type).
function kindEffect(kind, mode, autoAdd) {
  const what = `A new ${kindLabel(kind).toLowerCase()}`;
  if (mode === 'auto') {
    return `${what} is replaced by the same stand-in every time it is sent, and replies show you the real value. `
      + 'It is never stored and never reaches Review.';
  }
  if (mode === 'ignore') {
    return `${what} is sent to the AI service as written and is not offered in Review.`
      + (kind === 'email' ? ` ${EMAIL_IGNORE_WARNING}` : '');
  }
  let said = `${what} is replaced by a placeholder while it waits in Review for you to add it to a client or ignore it.`;
  if (OWNED_KINDS.includes(kind)) {
    const proof = OWNER_EXAMPLES[kind] ? ` - ${OWNER_EXAMPLES[kind]} -` : '';
    said += autoAdd
      ? ` When the value itself proves whose it is${proof} it is added to that client straight away instead.`
      : ' Auto-add is off, so it always waits in Review.';
  }
  return said;
}

function kindsCard(s, kinds) {
  const quiet = new Set(s['detect.quiet_kinds'] || []);
  const review = new Set(s['detect.placeholder_kinds'] || []);
  const ownerAdd = new Set(s['detect.owner_add_kinds'] || []);
  if (s['detect.auto_map_client_resources']) ownerAdd.add('client-resource');
  const rows = kinds.filter((kind) => kind !== 'name' && kind !== 'guid');
  const modeOf = (kind) => (quiet.has(kind) ? 'auto' : review.has(kind) ? 'review' : 'ignore');
  const head = h('tr', {}, h('th', {}, 'Type'), MODES.map(([, label]) => h('th', {}, label)), h('th', {}, 'Auto-add to client'));
  const fixed = h('tr', { dataset: { kind: 'guid' } }, h('td', {}, kindLabel('guid')),
    h('td', {}, h('input', { type: 'radio', name: 'mode-guid', checked: true, disabled: true,
      title: 'Always: GUIDs are never proposed, never stored, always replaced' })),
    h('td', {}, ''), h('td', {}, ''), h('td', {}, ''));
  const body = rows.map((kind) => {
    const radios = MODES.map(([value]) => h('input', { type: 'radio', name: `mode-${kind}`, value,
      checked: modeOf(kind) === value, 'aria-label': `${kindLabel(kind)}: ${value}` }));
    const tick = OWNED_KINDS.includes(kind)
      ? h('input', { type: 'checkbox', name: `owner-${kind}`, checked: ownerAdd.has(kind), 'aria-label': `${kindLabel(kind)}: auto-add to client` })
      : null;
    const warn = kind === 'email' ? h('div', { class: 'hint kind-warning', hidden: true }, EMAIL_IGNORE_WARNING) : null;
    // The help mark (an emoji, written as its escape, so it stands out - the user) shows what the row's choices add up to: on hover (title), and on a click or tap, which
    // opens it under the type - a phone has no hover. Kept current as the choices change.
    const effect = h('div', { class: 'hint kind-effect', hidden: true, id: `kind-effect-${kind}` });
    const help = h('button', { type: 'button', class: 'link kind-help', 'aria-expanded': 'false',
      'aria-controls': effect.id, 'aria-label': `What the choices for ${kindLabel(kind)} will do` }, '\u2753');
    const sync = () => {
      const mode = radios.find((r) => r.checked).value;
      if (tick) tick.hidden = mode !== 'review';
      // The effect already carries the email warning when it is open.
      if (warn) warn.hidden = mode !== 'ignore' || !effect.hidden;
      const said = kindEffect(kind, mode, Boolean(tick && tick.checked));
      effect.textContent = said;
      help.title = said;
    };
    help.addEventListener('click', () => {
      effect.hidden = !effect.hidden;
      help.setAttribute('aria-expanded', String(!effect.hidden));
      sync();
    });
    for (const r of radios) r.addEventListener('change', sync);
    if (tick) tick.addEventListener('change', sync);
    const row = h('tr', { dataset: { kind } }, h('td', {}, kindLabel(kind), ' ', help, effect, warn),
      radios.map((r) => h('td', {}, r)), h('td', {}, tick || '-'));
    sync();
    return row;
  });
  const table = h('table', { class: 'kinds-table' }, h('thead', {}, head), h('tbody', {}, fixed, body));
  const read = () => {
    const picked = Object.fromEntries(rows.map((kind) => [kind, table.querySelector(`input[name="mode-${kind}"]:checked`).value]));
    const ticked = (kind) => { const t = table.querySelector(`input[name="owner-${kind}"]`); return Boolean(t && t.checked); };
    return {
      'detect.quiet_kinds': rows.filter((kind) => picked[kind] === 'auto'),
      'detect.placeholder_kinds': rows.filter((kind) => picked[kind] === 'review'),
      'detect.owner_add_kinds': rows.filter((kind) => kind !== 'client-resource' && ticked(kind)),
      'detect.auto_map_client_resources': ticked('client-resource'),
    };
  };
  return card('kinds', 'What happens to something new',
    // Shorter, with the detail behind each row's ? (the user, 2026-10-06).
    'What aicleaner does with identifiers the mapping does not know yet, for each type. Hover over or click the \u2753 beside a type to see what its current choices will do. Private IP addresses and anything on an ignore list are never touched - but an ignore wildcard (admin*, *-prod) never covers a value that contains a client\'s name: admin-<client>-vm still reaches Review unless that exact value is on the list.',
    [h('div', { class: 'kinds-wrap' }, table),
      h('p', { class: 'hint' }, 'Always, whatever is chosen here: a name that carries a client\'s token is added to that client (Client07 Northern Rivers joins the client behind Client07).')],
    read);
}

// The Session page, on or off, now: switching it off stops every session running from it.
function sessionCard(s) {
  const on = checkbox('session', 'Drive Claude Code sessions from the Session page', s['session.enabled']);
  // How many may run at once (the user, 2026-10-16): 4 by default, 0 for no cap.
  const most = h('input', { type: 'number', name: 'max_running', min: '0', max: '64', step: '1',
    value: String(s['session.max_running'] ?? 4) });
  // Titles when a session stops (item E, 2026-10-16): on by default; one short request to a small model.
  const titles = checkbox('auto-titles', 'Write a title for a conversation when its session stops (a short request to a small model, filtered as usual)',
    s['session.auto_titles'] !== false);
  return card('session', 'Session page',
    'On: the Session page can start Claude Code on this machine in your profiles\' folders, with every request filtered and every action waiting for your approval. Through the tunnel it works from your phone. Off: the page and its API refuse, and switching it off stops any session that is running. Takes effect straight away.',
    [h('div', { class: 'form-row' }, on),
      h('div', { class: 'form-row' }, h('label', { class: 'field' },
        'Sessions running at once (0 = no cap; the machine is the limit). Lowering it stops nothing - it only refuses new starts until enough have stopped.', most)),
      h('div', { class: 'form-row' }, titles)],
    () => ({ 'session.enabled': on.querySelector('input').checked,
      'session.auto_titles': titles.querySelector('input').checked,
      // A cleared box is the default, never "no cap" by accident.
      'session.max_running': most.value.trim() === '' ? 4 : Math.max(0, Math.floor(Number(most.value) || 0)) }));
}

// The encrypted tunnel (feature 21, DECISIONS 57 and 60): on or off, where a device keeps the key,
// and the code - always generated here, shown this once (no chosen passphrases: DECISIONS 60). All
// of it on this machine only; through the tunnel the card is greyed out like the other local ones.
function tunnelCard(s) {
  // On and off reach through the tunnel (the user, 2026-10-07); where a device keeps its key, and
  // making a code - shown once - stay on the machine.
  const remote = Boolean(view.config && view.config.remote);
  const on = checkbox('encrypt', 'Encrypt everything that crosses the tunnel', s['remote.encrypt']);
  const storage = h('select', { name: 'key_storage', 'aria-label': 'Where a device keeps the key' },
    h('option', { value: 'tab', selected: s['remote.key_storage'] !== 'device' ? true : null },
      'For the browser tab only - typed again after closing it'),
    h('option', { value: 'device', selected: s['remote.key_storage'] === 'device' ? true : null },
      'On the device - until you forget it here (below) or a new code is generated'));
  const state = h('p', { class: 'hint tunnel-secret-state' }, 'Checking for a code...');
  const generate = h('button', { type: 'button', class: 'tunnel-generate' }, 'Generate a code');
  const shown = h('p', { class: 'mono tunnel-code', hidden: true });
  const look = () => getJSON('/api/tunnel/secret').then((t) => {
    state.textContent = t.set ? 'A code is set. Generating a new one means typing it again on every device.'
      : t.legacy ? 'The passphrase from an earlier version is no longer used: generate a code.'
        : 'No code yet: generate one before turning encryption on.';
    generate.textContent = t.set ? 'Generate a new code' : 'Generate a code';
  }).catch(() => { state.textContent = 'Set on the aicleaner machine itself.'; });
  generate.addEventListener('click', async () => {
    try {
      const made = await postJSON('/api/tunnel/secret', { generate: true });
      shown.textContent = `${made.code}  - shown this once: put it somewhere you can reach from the other device.`;
      shown.hidden = false;
      look();
    } catch (error) { sayError(error); }
  });
  look();
  if (remote) {
    for (const control of [storage, generate]) {
      control.disabled = true;
      control.title = LOCAL_ONLY;
    }
  }
  // This device's own copy of the key (the user, 2026-10-07): only while the page talks through the
  // encrypted tunnel, and usable from there - it changes nothing on the aicleaner machine or on any
  // other device. Afterwards the page asks for the code again.
  const forget = isSealed() ? h('button', { type: 'button', class: 'tunnel-forget' }, 'Forget the key on this device') : null;
  if (forget) {
    forget.addEventListener('click', () => {
      forgetKey();
      say('The key is gone from this device. Enter the code to carry on.', 'ok');
      setTimeout(() => window.location.reload(), 800);
    });
  }
  return card('tunnel', 'Encrypted tunnel',
    'Cloudflare - and anything else that can open HTTPS on the way - decrypts what crosses the tunnel. On: through the tunnel the page asks for the code, then everything it sends and receives is encrypted with a new key for each connection, agreed with this machine and proved by the code. A recording stays sealed even if the code leaks later, and a request cannot be sent twice. On this machine nothing changes. Still visible on the way: that you are using it, when, and how much - and someone able to rewrite the page itself could still take the code as it is typed. While on, the Session page polls instead of streaming, and images, PDFs and downloads open on the machine only.',
    [// What the lock beside the menu says, through the tunnel (the user, 2026-10-08).
      h('dl', { class: 'tunnel-states' },
        h('dt', { class: 'tunnel-state sealed' }, h('span', { class: 'tunnel-lock-icon', 'aria-hidden': 'true' }, icon('padlock')), ' End-to-end Encrypted'),
        h('dd', {}, 'Encryption is on and this page has the code: everything it sends and receives is sealed between your browser and this machine. Cloudflare, and any proxy on the way, sees only sealed envelopes - not the names, files or conversation inside.'),
        h('dt', { class: 'tunnel-state open' }, h('span', { class: 'tunnel-lock-icon', 'aria-hidden': 'true' }, icon('padlock-open')), ' Traffic can be Observed'),
        h('dd', {}, 'Encryption is off. HTTPS protects the trip, but Cloudflare ends the encrypted connection at its edge (SSL termination) and passes it on, so it can read what crosses; so can a proxy on the way that decrypts HTTPS to inspect it (a corporate firewall, some antivirus). What you see here - real names included - is readable at those points.')),
      h('div', { class: 'form-row' }, on),
      h('label', { class: 'field' }, 'Where a device keeps the key (never the code)', storage),
      state, h('div', { class: 'form-row' }, generate, forget), shown],
    () => (remote ? { 'remote.encrypt': on.querySelector('input').checked }
      : { 'remote.encrypt': on.querySelector('input').checked, 'remote.key_storage': storage.value }),
    // Through the tunnel, switching encryption changes how this very page talks: start again.
    (before, after) => {
      if (remote && Boolean(before['remote.encrypt']) !== Boolean(after['remote.encrypt'])) {
        say('Encryption switched - reloading the page to match.', 'ok');
        setTimeout(() => window.location.reload(), 800);
      }
    });
}

function highlightCard(s) {
  const terminal = checkbox('terminal', 'Also colour them in Claude Code (terminal and VS Code)', s['highlight.terminal']);
  return card('highlight', 'Restored names in green',
    'Names the proxy restored in a reply are shown in green, so you can see what the filter did and read an odd answer in that light - the AI saw a token where you see the name. The session page always does this. Claude Code gets it as a terminal colour: turn it on and look at a reply, and if the VS Code panel shows raw codes such as [32;22m instead of colour, turn it off again. Claude Desktop is not coloured.',
    [h('div', { class: 'form-row' }, terminal)],
    () => ({ 'highlight.terminal': terminal.querySelector('input').checked }));
}

// Claude Code on Windows (feature 98): its Bash tool halves doubled backslashes, so a hook stops a
// Bash call holding them. Shown on Windows only; saving rewrites the hook in Claude Code's settings.
function bashGuardCard(s) {
  // Named for PowerShell, which is what people look for (D172, the user 2026-10-09).
  const guard = checkbox('bash-guard', 'Use PowerShell, and stop Bash commands that contain a doubled backslash', s['claude.bash_guard']);
  return card('bash-guard', 'Claude Code on Windows: PowerShell',
    'On Windows, Claude Code\'s Bash tool halves every doubled backslash before the shell sees it - even inside a quoted heredoc - so text Claude writes through Bash can silently change (\\\\n becomes a line break). Write, Edit and the PowerShell tool keep backslashes exactly. With this on, a Bash call holding a doubled backslash is stopped and Claude is told to use PowerShell (5.1 preferred, or 7) or Write/Edit; the PowerShell tool is switched on in Claude Code\'s settings if it is not already. It applies to skills and plugins too - a hook cannot tell them apart. Restart Claude Code sessions after changing it.',
    [h('div', { class: 'form-row' }, guard)],
    () => ({ 'claude.bash_guard': guard.querySelector('input').checked }));
}

// The Claude Desktop app's Code tab through aicleaner (feature 117). What is in place is fetched
// separately: asking Claude Code its version takes a moment, and the rest of the page need not wait.
function codeTabCard(s) {
  const on = checkbox('codetab', 'Filter the Code tab in the Claude Desktop app', s['codetab.enabled']);
  const port = h('input', { type: 'number', name: 'codetab_port', min: '1024', max: '65535', value: String(s['codetab.port'] || 8788),
    'aria-label': 'Code tab proxy port', class: 'narrow' });
  const status = h('div', { class: 'hint codetab-status' }, 'Checking...');
  getJSON('/api/codetab').then((info) => {
    const lines = [];
    if (info.enabled) {
      lines.push(info.listening ? `Listening on 127.0.0.1:${info.port}.` : `Not listening${info.error ? `: ${info.error}` : '.'}`);
      const set = info.settings || {};
      lines.push(set.proxy_set && set.ca_set && set.hook ? 'Claude Code\'s settings point at it.' : 'Claude Code\'s settings are not all in place - save again.');
      if (set.mcp_servers) lines.push(`${set.mcp_servers} local MCP server(s) kept off the proxy.`);
      const seen = Object.entries(info.tunnels || {}).map(([host, n]) => `${host} (${n})`).join(', ');
      if (seen) lines.push(`Seen: ${seen}.`);
    } else {
      lines.push(info.error || 'Off.');
    }
    if (info.claude_version && info.powershell_ok === false) {
      lines.push(`Claude Code ${info.claude_version} is older than ${info.powershell_from}: its PowerShell tool will still use the proxy (git and npm there pass through it untouched). Update Claude Code to keep them off it.`);
    }
    status.textContent = lines.join(' ');
  }).catch(() => { status.textContent = ''; });
  return card('codetab', 'Claude Code in the Claude Desktop app (the Code tab)',
    'The Code tab does not use aicleaner\'s usual routing. With this on, Claude Code\'s settings send its traffic through a proxy on this machine: calls to Anthropic are read and filtered like VS Code\'s, sign-in and updates pass through untouched, and its error-log upload is blocked. Only Claude Code trusts the certificate aicleaner uses, and that certificate can only vouch for api.anthropic.com. Commands Claude Code runs and the MCP servers in its own settings connect directly, as before; MCP servers from a project\'s .mcp.json or a plugin pass through the proxy untouched. While aicleaner is stopped the Code tab cannot reach Anthropic - nothing is sent unfiltered. Restart the Claude Desktop app after changing it.',
    [h('div', { class: 'form-row' }, on), h('label', { class: 'field' }, 'Port', port), status],
    () => ({ 'codetab.enabled': on.querySelector('input').checked, 'codetab.port': Number(port.value) || 8788 }));
}

// The file tabs on the Session page (feature 92): what a double-click in a Markdown preview does.
// Saved on the server, so every device follows it (the user, 2026-10-08).
function editorCard(s) {
  const choices = [
    ['ask', 'Ask the first time'],
    ['source', 'Jump to the source with that word selected'],
    ['off', 'Nothing - double-click selects text, as usual'],
  ];
  const pick = h('select', { name: 'preview_dblclick', 'aria-label': 'Double-click in a Markdown preview' },
    ...choices.map(([value, label]) => h('option', { value, selected: s['editor.preview_dblclick'] === value ? true : null }, label)));
  return card('editor', 'File editor',
    'Double-clicking a word in a Markdown preview can take you to the same word in the source, selected '
    + 'and ready to edit - handy for reading in the preview and then changing what you read.',
    [h('label', { class: 'field' }, 'Double-click in a Markdown preview', pick)],
    () => ({ 'editor.preview_dblclick': pick.value }),
    () => forgetPreviewChoice());
}

// Autocorrect in the Session page's prompt (feature 94). The prompt's own button is this same
// setting. Code and paths are always left alone; the rest is the user's choice.
function autocorrectCard(s) {
  const on = checkbox('autocorrect', 'Correct spelling in the prompt as you type', s['prompt.autocorrect']);
  const skip = new Set(s['prompt.autocorrect_skip'] || []);
  const skips = [['clients', 'Client names the filter knows'], ['used', 'Words already in the conversation or the prompt'],
    ['capitalised', 'Words starting with a capital']].map(([value, label]) =>
    checkbox(`skip-${value}`, label, skip.has(value)));
  const own = h('textarea', { class: 'mono ac-own-words', rows: '4', 'aria-label': 'Your own words' },
    (s['prompt.autocorrect_words'] || []).join('\n'));
  const swaps = h('textarea', { class: 'mono ac-replacements', rows: '4', 'aria-label': 'Your replacements',
    placeholder: 'keyvault = Key Vault' }, (s['prompt.autocorrect_replace'] || []).join('\n'));
  return card('autocorrect', 'Prompt autocorrect',
    'Misspelt words are corrected at the next space or punctuation and shown in green; click or tap one to put the original back, pick another suggestion, or add it to your words. Turning it on corrects from then on, never what is already typed. Code and paths are always left alone.',
    [h('div', { class: 'form-row' }, on),
      h('p', { class: 'hint' }, 'Also leave alone:'),
      h('div', { class: 'form-row' }, ...skips),
      h('label', { class: 'field' }, 'Your own words, one per line, as they should be spelt (powershell becomes PowerShell)', own),
      h('label', { class: 'field' }, 'Your replacements, one per line as typed = replacement (keyvault = Key Vault). A one-word replacement with capitals (OneDrive) also counts as one of your words', swaps),
      h('p', { class: 'hint' }, 'A word with a known spelling - yours, a replacement\'s, or a product name - always takes it, even where the options above would leave a word alone.')],
    () => ({
      'prompt.autocorrect_replace': swaps.value.split('\n').map((w) => w.trim()).filter(Boolean),
      'prompt.autocorrect': on.querySelector('input').checked,
      'prompt.autocorrect_skip': ['clients', 'used', 'capitalised'].filter((value, i) => skips[i].querySelector('input').checked),
      'prompt.autocorrect_words': own.value.split('\n').map((w) => w.trim()).filter(Boolean),
    }));
}

// Diagnostics (feature 91): timing what the pages ask for, to see where slow moments come from. It
// switches itself off after a day, so it is never left on; while on, the card says until when.
function diagnosticsCard(s, status) {
  const timings = checkbox('timings', 'Record timings', s['debug.timings'] && status && status.debug_timings);
  const until = status && status.debug_timings_until
    ? h('p', { class: 'chip warn diagnostics-on' },
      `Recording until ${new Date(status.debug_timings_until * 1000).toLocaleString()}`)
    : null;
  return card('diagnostics', 'Diagnostics',
    'Record timings: how long each request and each step of the pages takes (switching session, '
    + 'loading a conversation, drawing it, decrypting through the tunnel), written to '
    + 'logs\\timings-<date>.jsonl on this machine. Durations, sizes and request paths only - never '
    + 'content. Switches itself off after 24 hours.',
    [until, h('div', { class: 'form-row' }, timings)].filter(Boolean),
    () => ({ 'debug.timings': timings.querySelector('input').checked }));
}

// The escape hatch, and it looks like one: a false positive in the canary refuses every request,
// including the ones to the assistant you would use to fix it. Off is the only safe setting.
// TEMPORARY, while the filter is being debugged: the proxy still carries the traffic, but does
// nothing to it. First card on the page, because everything below it is moot while it is on.
function bypassCard(s) {
  const on = checkbox('bypass', 'TEMPORARY: forward every request untouched (no filtering at all)', s['proxy.bypass']);
  const section = card('bypass', 'Debugging: bypass the proxy',
    'On: model traffic passes straight through. Nothing is replaced, nothing is held back, secrets are not redacted, images and documents are not withheld, the canary does not check, and replies are not restored - so real client names, and anything else in a prompt, reach the AI provider exactly as written. Every request is logged as PROXY BYPASS. This exists so the filter can be switched off without an uninstall while its own faults are worked through, and is to be removed before the tool is relied on.',
    [h('div', { class: 'form-row' }, on)],
    () => ({ 'proxy.bypass': on.querySelector('input').checked }));
  return danger(section, s['proxy.bypass'], 'BYPASS IS ON: NOTHING IS FILTERED');
}

function failOpenCard(s) {
  const on = checkbox('fail-open', 'EMERGENCY: send requests the canary would refuse', s['canary.fail_open']);
  const section = card('fail-open', 'Emergency: canary fail open',
    'Only for getting past a false 403 ("a real client identifier survived tokenisation"). While this is on, a request the canary would refuse is sent anyway - if the match was real, that client name reaches the AI provider. Every such request is written to the evidence log as FAIL-OPEN, and each match - with the text around it - goes to logs/proxy-stderr.log and the Log page. It can be turned on from the phone, because a false positive stays in the conversation and would otherwise refuse every request until you are back at this machine. Turn it off as soon as you are past the problem.',
    [h('div', { class: 'form-row' }, on)],
    () => ({ 'canary.fail_open': on.querySelector('input').checked }));
  return danger(section, s['canary.fail_open'], 'FAIL-OPEN IS ON: THE CANARY IS NOT STOPPING ANYTHING');
}

// Stop-AiCleaner.ps1, then Start-AiCleaner.ps1 -Detach, without a terminal: settings that are read
// at start (listen, remote access, the page's own files) need it, and so does a stale page.
function restartCard() {
  const button = h('button', { type: 'button', class: 'primary restart' }, 'Restart the proxy');
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await postJSON('/api/restart', {});
      say('Restarting: Stop-AiCleaner.ps1, then Start-AiCleaner.ps1 -Detach. The page reloads when it answers.', 'ok');
      await waitForProxy();
      window.location.reload();
    } catch (error) {
      sayError(error);
      button.disabled = false;
    }
  });
  const since = view.ctx.status && view.ctx.status.started_at;
  const section = card('restart', 'Restart the proxy',
    'Runs Stop-AiCleaner.ps1, then Start-AiCleaner.ps1 -Detach (or the logon task) on this machine. Requests in flight are cut off, and Claude sessions started from the Session page stop. Output goes to logs/restart.log.',
    [h('div', { class: 'form-row' }, button)], null);
  // Beside the title, because Restart is the remedy suggested for several things and a time that
  // has not moved is how you tell whether one actually happened.
  if (since) {
    section.querySelector('h2').append(
      h('span', { class: 'muted since' }, ` - running since ${shortTime(new Date(since * 1000).toISOString())}`));
  }
  return section;
}

// What each AI client on this machine is currently pointed at. Read-only, and read fresh on every
// status poll: the values change outside the application, which is the whole reason to show them.
function routingCard() {
  const routing = (view.ctx.status && view.ctx.status.routing) || {};
  const rows = routing.clients || [];
  if (!rows.length) {
    return card('routing', 'What your AI clients are pointed at',
      'No client settings file was found on this machine, so there is nothing to report.', [], null);
  }
  // A path or URL has no spaces to wrap at: on a phone it pushed the whole page sideways.
  const table = h('table', { class: 'routing-table' }, h('tbody', {}, rows.map((row) => h('tr', {},
    h('th', {}, row.client),
    h('td', {},
      h('span', { class: `chip ${row.routed ? 'ok' : 'bad'}` }, row.routed ? 'through aicleaner' : 'direct to the AI service'),
      h('div', { class: 'mono' }, row.value || '(not set)'),
      h('div', { class: 'hint' }, row.file))))));
  // Project settings that route around aicleaner whatever the user settings say (H10).
  const overrides = (routing.overrides || []).map((o) => h('p', { class: 'callout bad routing-override' },
    h('b', {}, 'Routes around aicleaner: '), h('span', { class: 'mono' }, o.file), ` - ${o.why}.`));
  const seen = (view.ctx.status && view.ctx.status.requests_seen) || 0;
  return card('routing', 'What your AI clients are pointed at',
    'Read from each client\'s own settings file, and from each session folder\'s .claude settings. A client '
    + 'sending directly to the AI service is not filtered by aicleaner, whatever this page says about the mapping.',
    [table, ...overrides, h('p', { class: 'hint' },
      seen ? `${seen} ${seen === 1 ? 'request has' : 'requests have'} come through since the proxy started.`
        : 'Nothing has come through since the proxy started.')], null);
}

// Version and updates (2026-10-16): what is installed, the newest release on the repository it came
// from, Update now (the installed Deploy-AiCleaner.ps1 -Action Update, handed off as Restart is), and
// whether the proxy installs a newer release on its own.
function updatesCard(s) {
  const installedLine = h('p', { class: 'updates-installed' }, 'Installed: ...');
  const availableLine = h('p', { class: 'updates-available' }, 'Checking for a newer release...');
  const checkedLine = h('p', { class: 'hint updates-checked' });
  const check = h('button', { type: 'button', class: 'updates-check' }, 'Check now');
  const apply = h('button', { type: 'button', class: 'primary updates-apply', hidden: true }, 'Update now');
  const auto = checkbox('update-auto', 'Update automatically (every 6 hours, when no session is mid-turn)', s['update.auto']);
  const show = (info) => {
    const from = info.repo ? ` - from ${info.repo}${info.ref ? ` (${info.ref})` : ''}` : '';
    installedLine.textContent = `Installed: v${info.version}${from}`;
    availableLine.className = `updates-available${info.newer ? ' ok' : ''}`;
    availableLine.textContent = info.error ? `Could not check: ${info.error}`
      : info.updating ? 'An update is under way - see logs/update.log.'
        : info.newer ? `${info.latest} is available.` : info.latest ? `Up to date (newest release: ${info.latest}).` : 'No releases found.';
    checkedLine.textContent = info.checked_at ? `Last checked ${shortTime(new Date(info.checked_at * 1000).toISOString())}.` : '';
    // Not offered through the tunnel: it runs the installer (Update automatically is).
    apply.hidden = !info.newer || info.updating || Boolean(view.config && view.config.remote);
    apply.textContent = info.newer ? `Update now to ${info.latest}` : 'Update now';
  };
  const load = (force) => getJSON(`/api/updates${force ? '?check=1' : ''}`).then(show)
    .catch((error) => { availableLine.textContent = `Could not check: ${error.message || error}`; });
  check.addEventListener('click', async () => { check.disabled = true; await load(true); check.disabled = false; });
  apply.addEventListener('click', async () => {
    apply.disabled = true;
    const was = view.ctx.status && view.ctx.status.started_at;
    try {
      await postJSON('/api/updates/apply', {});
      say('Updating: Deploy-AiCleaner.ps1 -Action Update. The page reloads when the new version answers.', 'ok');
      await waitForNewProxy(was);
      window.location.reload();
    } catch (error) {
      sayError(error);
      apply.disabled = false;
    }
  });
  load(false);
  return card('updates', 'Version and updates',
    'Updates come from the repository this install came from: the newest vX.Y.Z release there. Update now runs the installer\'s own update (Deploy-AiCleaner.ps1 -Action Update) on this machine, which stops the proxy, installs the release and starts it again; sessions carry on. Output goes to logs/update.log.',
    [installedLine, availableLine, h('div', { class: 'form-row' }, check, apply), checkedLine,
      h('div', { class: 'form-row' }, auto)],
    () => ({ 'update.auto': auto.querySelector('input').checked }));
}

// Feedback (2026-10-16): whether this install is connected to the feedback system, where that is, the
// short ID to quote, and the way to open it or disconnect. Connecting is asked for in the dialog.
function feedbackCard() {
  const line = h('p', { class: 'feedback-state' }, 'Checking...');
  const open = h('button', { type: 'button', class: 'feedback-open' }, 'Open feedback');
  const disconnect = h('button', { type: 'button', class: 'feedback-disconnect', hidden: true }, 'Disconnect');
  const show = (state) => {
    line.textContent = state.connected
      ? `Connected to ${state.relay} since ${state.consented_at.replace('T', ' ')} - this install is ${state.client}.`
      : `Not connected. Opening feedback asks first; nothing is sent to ${state.relay} until you say yes.`;
    disconnect.hidden = !state.connected;
  };
  getJSON('/api/feedback/state').then(show).catch(() => { line.textContent = 'Could not check.'; });
  open.addEventListener('click', () => openFeedback());
  disconnect.addEventListener('click', async () => {
    try { show(await postJSON('/api/feedback/disconnect', {})); say('Disconnected. Your tickets stay where they are.', 'ok'); }
    catch (error) { sayError(error); }
  });
  return card('feedback', 'Feedback',
    'Raise tickets anonymously and follow the replies. Each ticket carries which settings are on or off, the page and the version - never folders, client names or anything that identifies you. Connecting again later, even after a reinstall on this machine, finds the same tickets.',
    [line, h('div', { class: 'form-row' }, open, disconnect)], null);
}

// After an update: wait for a proxy that started after the one that was running (the old one keeps
// answering while the installer downloads), for up to ten minutes.
async function waitForNewProxy(startedAt) {
  for (let attempt = 0; attempt < 300; attempt += 1) {
    await new Promise((resolve) => { setTimeout(resolve, 2000); });
    try {
      const response = await fetch('/api/status', { cache: 'no-store' });
      if (response.ok) {
        const status = await response.json();
        if (!startedAt || (status.started_at && status.started_at !== startedAt)) return;
      }
    } catch (error) {
      // stopped for the install, or starting
    }
  }
}

async function waitForProxy() {
  // Give the old process time to go, then wait for the new one to answer.
  await new Promise((resolve) => { setTimeout(resolve, 3000); });
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await fetch('/api/status', { cache: 'no-store' });
      if (response.ok) return;
    } catch (error) {
      // still restarting
    }
    await new Promise((resolve) => { setTimeout(resolve, 1000); });
  }
}

function toolsCard(s) {
  const area = h('textarea', { name: 'tools', rows: '3', 'aria-label': 'Tool name patterns, one per line' });
  area.value = (s.keep_tokenised_tools || []).join('\n');
  return card('tools', 'Tool arguments kept tokenised',
    'Remote MCP servers whose arguments must not carry real names (one glob per line, for example mcp__*learn*__*). Local tools (az, git, Serena) are swapped back to real names.',
    [area], () => ({ keep_tokenised_tools: area.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean) }));
}

// The list that ships with the install: identifiers that are the same in every tenant, so they
// identify nobody. It is not editable here - it is generated and shipped - but it silently decides
// what is never queued for review and never tokenised, and that is worth being able to read when
// something you expected to see never turns up.
function publicListCard() {
  const numbers = h('p', { class: 'hint' }, 'Loading...');
  const sources = h('ul', { class: 'small muted' });
  // What the list has actually taken out. It has three times nearly eaten real client data, and
  // each time nothing would have shown it - a suppressed value simply never appears (D13).
  const taken = h('div', { class: 'small' });
  const download = h('a', { class: 'btn', href: '/api/export/public', download: 'public-identifiers.json' },
    icon('download'), 'Download the list');
  getJSON('/api/public').then((info) => {
    numbers.textContent = `${info.guids.toLocaleString()} GUIDs and ${info.domains} domains`
      + (info.generated ? `, built ${info.generated.slice(0, 10)}.` : '.');
    sources.replaceChildren(...(info.sources || []).map((line) => h('li', {}, line)));
    const rows = info.suppressed || [];
    if (!rows.length) {
      taken.replaceChildren(h('p', { class: 'hint' },
        'Nothing has been suppressed since the proxy started.'));
      return;
    }
    taken.replaceChildren(h('details', {},
      h('summary', {}, `Suppressed since the proxy started (${rows.length})`),
      h('p', { class: 'hint' },
        'Seen in traffic and treated as belonging to nobody, so never queued, never stood in for '
        + 'and never tokenised. If one of these really belongs to a client, it is being sent as '
        + 'written - add it to that client by hand.'),
      h('table', {}, h('tbody', {}, rows.map((row) => h('tr', {},
        h('td', {}, h('span', { class: 'mono' }, row.value)),
        h('td', { class: 'kind' }, kindLabel(row.kind)),
        h('td', {}, `${row.count}x`,
          (row.clients || []).length
            ? h('div', { class: 'hint' }, `seen with ${row.clients.join(', ')}`)
            : null)))))));
  }).catch(() => { numbers.textContent = 'The shipped list could not be read.'; });
  return card('public-list', 'Identifiers that belong to nobody',
    'Microsoft first-party application ids, built-in role and policy ids, Graph permission ids, '
    + 'vendor hosts and standards-body domains. Every one is the same in every tenant, so it names '
    + 'no client: these are never queued for review, never stood in for by a placeholder and never '
    + 'tokenised. It is generated by tools/build_public_identifiers.py from the sources below and '
    + 'shipped with the install, so it is the same for everyone and is not edited here. Use your '
    + "own ignore lists for anything specific to you.",
    [numbers, taken, h('div', { class: 'form-row' }, download), h('details', {},
      h('summary', {}, 'Where it comes from'), sources)],
    null);
}

function importExportCard(status) {
  const clients = [...(status.global ? [{ id: 'global', label: 'Global' }] : []), ...status.clients.map((r) => ({ id: r.id, label: displayName(r) }))];
  const select = h('select', { 'aria-label': 'Client to export' }, clients.map((c) => h('option', { value: c.id }, c.label)));
  const exportOne = h('a', { class: 'btn', href: '#' }, icon('download'), 'Export this client');
  const sync = () => {
    exportOne.href = `/api/export/client/${encodeURIComponent(select.value)}`;
    exportOne.setAttribute('download', `${select.value}.json`);
  };
  select.addEventListener('change', sync);
  if (clients.length) sync();
  const exportAll = h('a', { class: 'btn', href: '/api/export/all', download: 'aicleaner-clients.json' }, icon('download'), 'Export all clients');
  const file = h('input', { type: 'file', accept: '.json,application/json', 'aria-label': 'Client file to import' });
  const area = h('textarea', { rows: '4', placeholder: '{"id": "fabrikam", "forms": [{"real": "..."}], "ignore": []}', 'aria-label': 'Client document to import' });
  file.addEventListener('change', async () => { if (file.files[0]) area.value = await file.files[0].text(); });
  // What happens to a client already held (DECISIONS 44). Merge is the default: it is what a
  // teammate's copy of the same client is for.
  const MODES = [
    ['merge', 'Merge into a client already here', 'Adds what the file has and yours lacks; keeps everything of yours. A different token for the same value is reported, never taken.'],
    ['new', 'New clients only', 'Adds clients not held here; skips one held here with the same entries; refuses one that differs.'],
    ['replace', 'Replace a client already here', 'The file wins entirely. Your copy is backed up first.'],
  ];
  const explain = h('p', { class: 'hint', id: 'import-mode-explain' });
  const mode = h('select', { name: 'import-mode', 'aria-label': 'When the client is already here' },
    MODES.map(([value, label]) => h('option', { value }, label)));
  const show = () => { explain.textContent = MODES.find(([value]) => value === mode.value)[2]; };
  mode.addEventListener('change', show);
  show();
  const importButton = h('button', { type: 'button', class: 'primary import' }, 'Import');
  importButton.addEventListener('click', async () => {
    try {
      const result = await postJSON('/api/import', { document: area.value, mode: mode.value });
      const said = result.imported.map((c) => {
        if (c.mode === 'skipped') return `${c.id} (already here, skipped)`;
        if (c.mode === 'new') return `${c.id} (new)`;
        const clash = c.conflicts && c.conflicts.length ? `; ${c.conflicts.length} kept your token over theirs` : '';
        return `${c.id} (${c.mode}d, ${c.added} added${clash}; backup taken)`;
      });
      say(`Imported ${said.join(', ')}.`, 'ok');
      area.value = '';
      await view.ctx.refresh();
    } catch (error) {
      sayError(error);
    }
  });
  return card('importexport', 'Import and export',
    'A client file is the unit of sharing: the same file on a colleague\'s machine gives the same tokens. It contains real identifiers, so share it as you would client work.',
    [h('div', { class: 'form-row' }, select, exportOne, exportAll),
      h('h3', {}, 'Import'), h('div', { class: 'form-row' }, file), area,
      h('div', { class: 'form-row' }, h('label', { class: 'field' }, 'When the client is already here', mode), importButton),
      explain],
    null);
}

function contactCard(contact) {
  const parts = [];
  if (contact.name) parts.push(h('b', {}, contact.name));
  if (contact.organisation) parts.push(parts.length ? ` (${contact.organisation})` : h('b', {}, contact.organisation));
  const links = [];
  if (contact.email) links.push(h('a', { href: `mailto:${contact.email}` }, contact.email));
  if (contact.issues_url) links.push(h('a', { href: contact.issues_url }, 'Report an issue'));
  if (contact.repo_url) links.push(h('a', { href: contact.repo_url }, 'Repository'));
  if (contact.profile_url) links.push(h('a', { href: contact.profile_url }, 'Profile'));
  const body = contact.source
    ? h('p', {}, ...parts, ...links.flatMap((link) => [' | ', link]))
    : h('p', { class: 'muted' }, 'No contact file. Add a contact-*.json beside the aicleaner folder (see contact.example.json).');
  return card('contact', 'Contact', contact.source ? `From ${contact.source}; edit that file to change it.` : null, [body], null);
}

function advancedCard(status, config, dialects) {
  return card('advanced', 'Advanced', 'Read-only. Edit aicleaner-config.json and restart the proxy to change these.',
    [h('table', {}, h('tbody', {},
      h('tr', {}, h('th', {}, 'Config file'), h('td', { class: 'mono' }, config.config_path || 'defaults (no aicleaner-config.json)')),
      h('tr', {}, h('th', {}, 'Listening on'), h('td', { class: 'mono' }, `${status.listen}:${status.port}${status.loopback ? ' (loopback)' : ''}`)),
      h('tr', {}, h('th', {}, 'Model routes'), h('td', { class: 'mono' }, dialects.map((d) => `/${d}/`).join('  ') || 'none'))))],
    null);
}

export async function render(main, ctx) {
  view.main = main;
  view.ctx = ctx;
  main.append(h('div', { class: 'page-head' }, h('h1', {}, 'Settings'),
    h('span', { class: 'muted' }, 'Secrets mode is on the ', h('a', { href: '#/secrets' }, 'Secrets page'), '.')));
  // One fetch, and an /api/ one: through the tunnel only the page and /api/ are served, and a
  // refused /healthz used to leave the whole page empty with "Request failed (HTTP 403)".
  try {
    view.config = await getJSON('/api/config');
  } catch (error) {
    sayError(error);
    return;
  }
  view.host = h('div', { class: 'settings-columns' });
  view.jump = h('nav', { class: 'settings-jump', 'aria-label': 'Go to a setting' });
  main.append(view.jump, view.host);
  paintCards();
  paintJump();
  showCard(ctx.route && ctx.route.id);
  // One listener for the life of the page module, which does nothing once this page has been
  // navigated away from: the alternative is a teardown hook the router does not have.
  if (!view.watching) {
    view.watching = true;
    window.addEventListener('resize', () => {
      if (view.host && view.host.isConnected) dealCards(view.host, view.cards);
    });
  }
}
