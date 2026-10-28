// Settings: cards, each saved on its own (logging, notifications, auto-add, tool arguments),
// import and export, and read-only contact and advanced details, in groups chosen from a sidebar
// (GROUPS) with a search across all of them. Secrets mode lives on the Secrets page only.

import { getJSON, postJSON, say, sayError } from '../api.js';
import { h, icon, kindLabel, savingButton, shortTime, timeAgo } from '../dom.js';
import { displayName, setMenuLayout } from '../app.js';
import { rebase, track } from '../unsaved.js';
import { forgetKey, isSealed } from '../sealed.js';
import { forgetPreviewChoice } from '../previewjump.js';
import { openFeedback } from '../feedback.js';
import { aiToolsTable } from '../aitools.js';
import { casualFolderField } from '../chatnow.js';
import { arrangeSheet } from '../sheet.js';

const view = { main: null, ctx: null, config: null, host: null, cards: [], group: null, side: null };

// The groups down the left, in this order, and the cards in each, in this order (Brad, 2026-10-27:
// "a sidebar of groups, like Windows Settings"). A card is named by its data-card key, which never
// changes, so #/settings/<card> links and the tests that find a card by key outlive a new title.
// The Danger zone is last: every card in it turns a protection off, and a control like that should
// take deciding to reach. Pictures is dangerous too (one choice sends pictures unfiltered), but it
// is a filtering choice first, so it lives in Filtering and keeps its red state there.
const GROUPS = [
  { key: 'aicleaner', label: 'aicleaner',
    cards: ['setup', 'updates', 'service', 'restart', 'contact', 'feedback', 'menu-layout', 'importexport'] },
  { key: 'sessions', label: 'Sessions', cards: ['session', 'editor', 'autocorrect', 'highlight', 'notifications'] },
  { key: 'filtering', label: 'Filtering',
    cards: ['kinds', 'near-miss', 'public-list', 'tools', 'images', 'ocr', 'scan-markers'] },
  { key: 'ai-apps', label: 'AI apps', cards: ['aitools', 'routing', 'bash-guard', 'codetab', 'claudeai', 'copilot'] },
  { key: 'remote-access', label: 'Remote access', cards: ['tunnel', 'folders'] },
  { key: 'logs', label: 'Logs', cards: ['logging', 'diagnostics', 'advanced'] },
  { key: 'danger-zone', label: 'Danger zone', cards: ['bypass', 'fail-open', 'uninspected'] },
];
// A card added later and not yet given a group is still shown, in the first group, rather than lost.
const FALLBACK_GROUP = GROUPS[0].key;
// The group last chosen, for an address that names none (this browser only, like the menu layout).
const LAST_GROUP = 'aicleaner.settingsGroup';

// Other words people use for a card (feature 153, Brad 2026-10-27: "so that it can be found based on
// what I think it should be called"). "Find a setting" matches these as well as what the card says;
// your own words (ui.setting_keywords) add to them. Every card has some - a test checks.
const BUILTIN_WORDS = {
  setup: ['wizard', 'getting started', 'install', 'first run', 'onboarding'],
  updates: ['upgrade', 'version', 'release', 'auto-update', 'new version'],
  service: ['startup', 'boot', 'windows service', 'scheduled task', 'autostart'],
  restart: ['reboot', 'reload', 'stop', 'start proxy', 'relaunch'],
  contact: ['profile', 'account', 'email', 'my name', 'who am i'],
  feedback: ['bug report', 'issue', 'suggestion', 'support', 'help'],
  'menu-layout': ['navigation', 'sidebar', 'top bar', 'menu position'],
  importexport: ['backup', 'restore', 'migrate', 'transfer', 'download'],
  session: ['chat sessions', 'project sessions', 'conversations', 'claude code', 'permissions', 'profiles'],
  editor: ['code editor', 'monaco', 'minimap', 'completion', 'suggestions', 'markdown preview'],
  autocorrect: ['spelling', 'spell check', 'typos', 'replacements', 'dictionary'],
  highlight: ['colour', 'color', 'terminal', 'green text', 'rehydrated'],
  notifications: ['alerts', 'toast', 'footer', 'popups', 'messages'],
  kinds: ['detection', 'auto-add', 'new names', 'identifiers', 'placeholders', 'quiet'],
  'near-miss': ['typos', 'misreadings', 'misspellings', 'fuzzy', 'similar names'],
  'public-list': ['shared identifiers', 'global list', 'common names', 'public names'],
  tools: ['tool calls', 'MCP', 'tool arguments', 'tokens in tools'],
  images: ['pictures', 'screenshots', 'vision', 'photos', 'attachments', 'redact images',
    // D216: what the Session page does with a picture
    'attach', 'upload', 'paste', 'clipboard', 'image to text', 'session pictures'],
  ocr: ['scanned', 'text in images', 'PDF', 'scans', 'character recognition'],
  'scan-markers': ['build output', 'generated files', 'node_modules', 'bin', 'skip folders'],
  aitools: ['sign in', 'login', 'accounts', 'AI tools', 'Claude Code', 'Codex'],
  routing: ['proxy', 'base URL', 'endpoints', 'redirect', 'environment variables'],
  'bash-guard': ['shell', 'bash', 'commands', 'PowerShell tool', 'guard'],
  codetab: ['Claude Desktop', 'desktop app', 'code tab', 'forward proxy'],
  claudeai: ['claude.ai', 'Claude Desktop', 'chat', 'web chat', 'route B'],
  copilot: ['VS Code', 'Visual Studio Code', 'GitHub', 'code completion', 'certificate'],
  tunnel: ['remote access', 'VPN', 'phone', 'Cloudflare', 'away from desk', 'mobile', 'encryption'],
  folders: ['directories', 'paths', 'file picker', 'OneDrive', 'allowed folders'],
  logging: ['logs', 'audit', 'history', 'retention', 'detail log'],
  diagnostics: ['telemetry', 'tracing', 'performance', 'timings', 'slow', 'speed', 'debug'],
  advanced: ['config file', 'port', 'listening', 'aicleaner-config.json', 'technical'],
  bypass: ['turn off filtering', 'disable', 'pass through', 'off switch', 'unfiltered'],
  'fail-open': ['canary', 'emergency', 'false positive', 'locked out', 'override'],
  uninspected: ['unchecked', 'non-JSON', 'endpoints', 'paths not filtered', 'allow list'],
};

function groupOf(cardKey) {
  const found = GROUPS.find((group) => group.cards.includes(cardKey));
  return found ? found.key : FALLBACK_GROUP;
}

function groupLabel(groupKey) {
  return (GROUPS.find((group) => group.key === groupKey) || GROUPS[0]).label;
}

// The groups that have something to show: a group whose cards are all null here (AI apps off
// Windows has only the read-only ones, but a group could be empty) is not offered.
function shownGroups() {
  const present = new Set(view.cards.map((section) => groupOf(section.dataset.card)));
  return GROUPS.filter((group) => present.has(group.key));
}

// The cards in group order, each after a small "In <group>" line that is shown only while
// searching, when cards from every group are listed together. All of them stay in the page and
// are hidden rather than removed: a card out of sight still holds what was typed into it, and the
// unsaved-changes check (D98) still sees it.
function placeCards() {
  const rank = (section) => {
    const key = section.dataset.card;
    const group = GROUPS.findIndex((g) => g.key === groupOf(key));
    const within = GROUPS[group].cards.indexOf(key);
    return group * 100 + (within < 0 ? 99 : within);
  };
  const ordered = [...view.cards].sort((a, b) => rank(a) - rank(b));
  view.host.replaceChildren(...ordered.flatMap((section) => {
    const group = groupOf(section.dataset.card);
    const label = h('p', { class: 'settings-found-in', dataset: { group }, hidden: true }, 'In ',
      h('a', { href: `#/settings/${section.dataset.card}` }, groupLabel(group)),
      h('span', { class: 'settings-matched' }));
    label.querySelector('a').addEventListener('click', (event) => {
      event.preventDefault();
      clearSearch();
      openGroup(group);
      showCard(section.dataset.card);
    });
    return [label, section];
  }));
}

// What a search reads on a card: its title, its description and the words beside its controls. Not
// the Words line at its foot: keywords are matched on their own, so a match on one can say so.
function searchText(section) {
  const parts = section.querySelectorAll(':scope > h2, :scope > p.hint, label, legend, th, summary, option, button');
  return Array.from(parts).filter((el) => !el.closest('.setting-words'))
    .map((el) => el.textContent).join(' ').toLowerCase();
}

function cardTitle(section) {
  const title = section.querySelector(':scope > h2');
  return title ? title.textContent : section.dataset.card;
}

// A card's own words (ui.setting_keywords), as saved; the config always sends the map, maybe empty.
function ownWords(cardKey) {
  const map = (view.config && view.config.settings && view.config.settings['ui.setting_keywords']) || {};
  return Array.isArray(map[cardKey]) ? map[cardKey] : [];
}

function builtinWords(cardKey) {
  return BUILTIN_WORDS[cardKey] || [];
}

// Every word typed has to be on the card or in one of its keywords, in any order. Returns null for
// no match; otherwise the keywords that were needed (none when the card's own text was enough).
function matchCard(section, words) {
  const text = searchText(section);
  const key = section.dataset.card;
  const keywords = [...ownWords(key).map((word) => ({ word, own: true })),
    ...builtinWords(key).map((word) => ({ word, own: false }))];
  const used = [];
  for (const word of words) {
    if (text.includes(word)) continue;
    const hit = keywords.find((k) => k.word.toLowerCase().includes(word));
    if (!hit) return null;
    if (!used.includes(hit)) used.push(hit);
  }
  return used;
}

// Every word typed has to be somewhere on the card, in any order.
function applySearch(query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const searching = words.length > 0;
  let found = 0;
  for (const section of view.cards) {
    const label = section.previousElementSibling;
    const used = searching ? matchCard(section, words) : null;
    const show = searching ? used !== null : groupOf(section.dataset.card) === view.group;
    section.hidden = !show;
    if (label && label.classList.contains('settings-found-in')) {
      label.hidden = !(searching && show);
      // Why a card whose text does not say the word was found: "matched your word telemetry".
      const why = label.querySelector('.settings-matched');
      if (why) {
        why.replaceChildren(...(used && used.length ? [' - matched ',
          ...used.flatMap((k, i) => [i ? ', ' : '', k.own ? 'your word ' : '', h('em', {}, k.word)])] : []));
      }
    }
    if (show) found += 1;
  }
  view.empty.hidden = !(searching && found === 0);
  paintOffer(searching && found === 0 ? query : '');
  paintGroups();
}

// --- Your own words for a setting (feature 153) ---------------------------------------------------

const WORD_MAX = 40;            // config.SETTING_WORD_MAX
const WORDS_PER_CARD = 20;      // config.SETTING_WORDS_PER_CARD

const tidyWord = (text) => String(text || '').trim().split(/\s+/).filter(Boolean).join(' ');

// Saved like any other setting, so the words follow you to every device; the whole map is sent, as
// the server keeps a setting whole rather than merging into it.
async function saveWords(cardKey, words) {
  const map = { ...((view.config.settings || {})['ui.setting_keywords'] || {}) };
  if (words.length) map[cardKey] = words;
  else delete map[cardKey];
  const result = await postJSON('/api/config', { settings: { 'ui.setting_keywords': map } });
  view.config.settings = result.settings;
  window.dispatchEvent(new CustomEvent('aicleaner:settings', { detail: result.settings }));
  for (const section of view.cards) paintWords(section);
  applySearch(view.search ? view.search.value : '');
}

// Adds a word to a card; says why not when it cannot. true when it was added (or was there already).
async function addWord(cardKey, typed) {
  const word = tidyWord(typed);
  if (!word) return false;
  if (word.length > WORD_MAX) {
    say(`A word for a setting is up to ${WORD_MAX} characters.`, 'warn');
    return false;
  }
  const own = ownWords(cardKey);
  const known = [...own, ...builtinWords(cardKey)].map((w) => w.toLowerCase());
  if (known.includes(word.toLowerCase())) {
    say(`"${word}" is already a word for that setting.`, 'info');
    return true;
  }
  if (own.length >= WORDS_PER_CARD) {
    say(`Up to ${WORDS_PER_CARD} of your own words for one setting; remove one first.`, 'warn');
    return false;
  }
  try {
    await saveWords(cardKey, [...own, word]);
    return true;
  } catch (error) {
    sayError(error);
    return false;
  }
}

async function removeWord(cardKey, word) {
  try {
    await saveWords(cardKey, ownWords(cardKey).filter((w) => w !== word));
  } catch (error) {
    sayError(error);
  }
}

// The Words line at a card's foot: the built-in words in grey, your own with an x to remove, and a
// box to add one. Kept out of the unsaved-changes check: a word is saved the moment it is added.
// Added after the card is built, so a card locked through remote access still takes words.
function paintWords(section) {
  const key = section.dataset.card;
  let line = section.querySelector(':scope > .setting-words');
  if (!line) {
    line = h('div', { class: 'setting-words', 'data-unsaved-ignore': true });
    section.append(line);
  }
  const box = h('input', { type: 'text', class: 'setting-word-input', placeholder: 'Add a word',
    maxlength: WORD_MAX, autocomplete: 'off', 'aria-label': `Add your own word for ${cardTitle(section)}` });
  const add = h('button', { type: 'button', class: 'setting-word-add' }, 'Add');
  const submit = async () => {
    add.disabled = true;
    if (await addWord(key, box.value)) box.value = '';
    add.disabled = false;
  };
  add.addEventListener('click', submit);
  box.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); submit(); }
  });
  const own = ownWords(key).map((word) => {
    const remove = h('button', { type: 'button', class: 'setting-word-remove', title: `Remove ${word}`,
      'aria-label': `Remove your word ${word}` }, 'x');
    remove.addEventListener('click', () => removeWord(key, word));
    return h('span', { class: 'setting-word own', dataset: { word } }, word, remove);
  });
  const builtin = builtinWords(key).map((word) => h('span', { class: 'setting-word builtin',
    title: 'Built in', dataset: { word } }, word));
  line.replaceChildren(h('span', { class: 'setting-words-label' }, 'Words:'), ...own, ...builtin, box, add);
}

// When nothing matches, the words typed can become a word for the card you meant (Brad's case:
// "telemetry" for Diagnostics). Offered only for something that would be accepted as a word.
function paintOffer(query) {
  if (!view.offer) return;
  const word = tidyWord(query);
  if (!word || word.length > WORD_MAX) {
    view.offer.hidden = true;
    view.offer.replaceChildren();
    delete view.offer.dataset.word;
    return;
  }
  // Redrawn only when the word changes: a redraw would drop a setting already chosen in the list.
  if (!view.offer.hidden && view.offer.dataset.word === word) return;
  view.offer.dataset.word = word;
  const choose = h('select', { class: 'setting-word-card', 'aria-label': 'The setting this word is for' },
    h('option', { value: '' }, 'Choose a setting'),
    ...shownGroups().map((group) => h('optgroup', { label: group.label },
      ...view.cards.filter((section) => groupOf(section.dataset.card) === group.key)
        .map((section) => h('option', { value: section.dataset.card }, cardTitle(section))))));
  const add = h('button', { type: 'button', class: 'primary setting-word-offer-add', disabled: true }, 'Add');
  choose.addEventListener('change', () => { add.disabled = !choose.value; });
  add.addEventListener('click', async () => {
    add.disabled = true;
    if (!(await addWord(choose.value, word))) add.disabled = false;
  });
  view.offer.hidden = false;
  view.offer.replaceChildren(h('span', {}, 'Add ', h('em', {}, word), ' as a word for:'), choose, add);
}

function clearSearch() {
  if (view.search && view.search.value) view.search.value = '';
  applySearch('');
}

// The address says which group is open, without a navigation: the router is told, as the session
// page tells it (D118), so it does not take the new address for one it still has to route to.
function noteAddress(hash) {
  if (location.hash === hash) return;
  history.replaceState(null, '', hash);
  window.dispatchEvent(new CustomEvent('aicleaner:address', { detail: hash }));
}

function openGroup(groupKey, { address = true } = {}) {
  const offered = shownGroups();
  const chosen = offered.find((group) => group.key === groupKey) || offered[0];
  if (!chosen) return;
  view.group = chosen.key;
  try { localStorage.setItem(LAST_GROUP, chosen.key); } catch (error) { /* not remembered */ }
  if (address) noteAddress(`#/settings/${chosen.key}`);
  applySearch(view.search ? view.search.value : '');
}

// The sidebar (and, on a phone, the drop-down that stands in for it): one entry per group shown,
// the open one marked, and any group holding a card that is red now - a protection switched off -
// marked red too, so it can be seen without opening the group.
function paintGroups() {
  if (!view.side) return;
  const searching = Boolean(view.search && view.search.value.trim());
  const offered = shownGroups();
  const alarmed = new Set(view.cards.filter((section) => section.classList.contains('bad'))
    .map((section) => groupOf(section.dataset.card)));
  view.groupList.replaceChildren(...offered.map((group) => {
    const current = !searching && group.key === view.group;
    const bad = alarmed.has(group.key);
    const button = h('button', { type: 'button', class: `settings-group${bad ? ' bad' : ''}`,
      dataset: { group: group.key }, 'aria-current': current ? 'true' : null,
      title: bad ? `${group.label}: something here is switched on that turns a protection off` : null },
    h('span', { class: 'settings-group-name' }, group.label),
    bad ? h('span', { class: 'settings-group-mark', 'aria-label': 'turned on' }) : null);
    button.addEventListener('click', () => { clearSearch(); openGroup(group.key); window.scrollTo(0, 0); });
    return button;
  }));
  // An option cannot be coloured everywhere (a phone draws its own list), so it says so in words.
  view.groupSelect.replaceChildren(...offered.map((group) => h('option', { value: group.key },
    alarmed.has(group.key) ? `${group.label} (!)` : group.label)));
  view.groupSelect.value = view.group;
  view.groupSelect.classList.toggle('bad', alarmed.has(view.group));
}

function sidePanel() {
  view.search = h('input', { type: 'search', class: 'settings-search', placeholder: 'Find a setting',
    'aria-label': 'Find a setting in any group', 'data-unsaved-ignore': true, autocomplete: 'off' });
  view.search.addEventListener('input', () => applySearch(view.search.value));
  view.search.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { clearSearch(); view.search.blur(); }
  });
  view.groupList = h('nav', { class: 'settings-groups', 'aria-label': 'Settings groups' });
  view.groupSelect = h('select', { class: 'settings-group-select', 'aria-label': 'Settings group',
    'data-unsaved-ignore': true });
  view.groupSelect.addEventListener('change', () => {
    const chosen = view.groupSelect.value;   // read first: clearing the search redraws the options
    clearSearch();
    openGroup(chosen);
  });
  return h('aside', { class: 'settings-side', 'data-unsaved-ignore': true }, view.search, view.groupSelect, view.groupList);
}

// Where the page opens: the group or card the address names, else the group chosen last time, else
// the first. A card's address opens its group and goes to the card.
function openFromAddress(id) {
  if (id && GROUPS.some((group) => group.key === id)) {
    openGroup(id);
    return;
  }
  if (id && view.cards.some((section) => section.dataset.card === id)) {
    openGroup(groupOf(id), { address: false });
    showCard(id);
    return;
  }
  let last = null;
  try { last = localStorage.getItem(LAST_GROUP); } catch (error) { /* none */ }
  openGroup(last || GROUPS[0].key, { address: false });
}

// #/settings/<card> opens at that card (its group is opened first, by openFromAddress). A refusal
// elsewhere can then link straight to the control that answers it, instead of naming a card among
// thirty-odd and leaving you to find it - the folders list, two thirds of the way down one of two
// columns, was reported as missing. Unknown names are ignored, so an old link is never worse than
// the plain page.
function showCard(name) {
  if (!name || !view.host) return;
  const section = view.host.querySelector(`section[data-card="${CSS.escape(name)}"]`);
  if (!section) return;
  section.scrollIntoView({ block: 'center' });
  // Cards above it fill in as their data arrives (the AI tools list, the casual-chats folders) and push
  // it down after the scroll: kept in view while the page settles, unless the reader scrolls first.
  let moved = false;
  const reader = () => { moved = true; };
  window.addEventListener('wheel', reader, { once: true, passive: true });
  window.addEventListener('touchmove', reader, { once: true, passive: true });
  const settle = new ResizeObserver(() => { if (!moved) section.scrollIntoView({ block: 'center' }); });
  settle.observe(view.host);
  setTimeout(() => {
    settle.disconnect();
    window.removeEventListener('wheel', reader);
    window.removeEventListener('touchmove', reader);
  }, 3000);
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
  paintGroups();                        // the sidebar's red marks follow
}

function paintCards() {
  const s = view.config.settings;
  const status = view.ctx.status;
  const kinds = view.config.kinds || [];
  // The same page near and far while the filter is being debugged (see ui.REMOTE_SETTINGS).
  const everyday = [
    setupCard(),                                   // shown remotely too (E4): the wizard says what is local
    menuLayoutCard(),
    aiToolsCard(),                                 // item H: sign-in works remotely; routing stays local
    loggingCard(s), notificationsCard(s, Boolean(view.config.remote)), kindsCard(s, kinds),
    nearMissCard(s), ocrCard(s), sessionCard(s), tunnelCard(s), highlightCard(s),
    view.config.windows ? bashGuardCard(s) : null,
    view.config.windows ? codeTabCard(s) : null,   // remotely: shown, locked by its keys (E4)
    view.config.windows ? copilotCard(s) : null,   // remotely too; its certificate waits for the machine
    view.config.windows ? claudeaiCard(s) : null,  // route B: the Copilot rule
    toolsCard(s), routingCard(), restartCard(),
    view.config.windows ? serviceCard() : null,    // feature 107: shown remotely too, locked there
    updatesCard(s), feedbackCard(),
    foldersCard(s), scanMarkersCard(s),
    uninspectedCard(view.config.allow_non_json_paths), importExportCard(status), publicListCard(),
    contactCard(status.contact), editorCard(s), autocorrectCard(s), diagnosticsCard(s, status),
    advancedCard(status, view.config, view.config.dialects || []),
  ];
  // Every one of these turns a protection off - forwarding untouched, sending pictures unfiltered,
  // sending what the canary would refuse. Where each is shown is GROUPS' business (the Danger zone,
  // last; Pictures in Filtering); they turn red and say so when they are on (DANGEROUS, above), and
  // the sidebar marks their group, which is the half that has to be impossible to miss.
  const dangerous = [bypassCard(s), imagesCard(s), failOpenCard(s)];
  view.cards = [...everyday, ...dangerous].filter(Boolean);   // a card with nothing to show is null
  for (const section of view.cards) paintWords(section);
  placeCards();
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
// Every AI tool aicleaner knows (item H, 2026-10-19): found, filtered, signed in, and sign-in. The table
// locks what runs on this machine itself, so the card is not locked as a whole.
function aiToolsCard() {
  return card('aitools', 'Sign-ins',
    'The AI tools aicleaner knows, whether each is on this machine, whether its traffic is filtered, and whether it is signed in. '
    + 'Claude Code can be signed in to from here - from any device. GitHub Copilot CLI and Codex are shown so you can see them; '
    + 'filtering them is not built yet. Microsoft 365 Copilot runs in Microsoft\'s cloud and is not covered.',
    [aiToolsTable()], null);
}

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
    // Anything already on screen that follows a setting (the prompt's autocorrect) hears of it.
    window.dispatchEvent(new CustomEvent('aicleaner:settings', { detail: result.settings }));
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

function viewLogButton() {
  const button = h('button', { type: 'button', class: 'view-log' }, 'View log');
  button.addEventListener('click', () => { window.location.hash = '#/log'; });
  return button;
}

function loggingCard(s) {
  const detail = checkbox('detail', 'Write the substitution log', s['logs.detail']);
  const every = checkbox('detail-every', 'Log every value on every request (diagnosis)', s['logs.detail_every_request']);
  const retention = h('input', { type: 'number', name: 'retention', min: '0', value: String(s['logs.detail_retention_days']) });
  return card('logging', 'Logging',
    'The substitution log records what was replaced and by what, whose it is, what was done, and who was driving - real names, kept on this machine, read on the Log page. A value is logged the first time a session sends it and again when what happens to it changes. The evidence log (counts only) is always written.',
    // The Log page is opened from here since it left the menu (Brad, 2026-10-20).
    [h('div', { class: 'form-row' }, viewLogButton()),
      h('div', { class: 'form-row' }, detail, every), h('div', { class: 'form-row' },
        h('label', { class: 'field' }, 'Keep substitution log files for (days; 0 keeps them forever)', retention))],
    () => ({ 'logs.detail': detail.querySelector('input').checked,
      'logs.detail_every_request': every.querySelector('input').checked,
      'logs.detail_retention_days': Number(retention.value) }));
}

function notificationsCard(s, remote) {
  // Learning mode is detection itself, so through the tunnel it is never changed (H3) - shown, disabled
  // and saying so (E4, Brad 2026-10-19: visible remotely, never hidden), and never sent from there.
  const learning = checkbox('learning', 'Learning mode (find identifiers the mapping does not know)', s['detect.enabled']);
  if (remote) {
    const box = learning.querySelector('input');
    box.disabled = true;
    box.title = LOCAL_ONLY;
    learning.title = LOCAL_ONLY;
  }
  const footer = checkbox('footer', "Footer on the model's reply", s['detect.footer']);
  const toast = checkbox('toast', 'Windows toast', s['detect.toast']);
  return card('notifications', 'Notifications', 'How you hear about new identifiers to review.',
    [h('div', { class: 'form-row' }, learning), h('div', { class: 'form-row' }, footer, toast)],
    () => ({ ...(remote ? {} : { 'detect.enabled': learning.querySelector('input').checked }),
      'detect.footer': footer.querySelector('input').checked, 'detect.toast': toast.querySelector('input').checked }));
}

function imagesCard(s) {
  // Feature 114: three ways for a picture - text only (the default), with client names blacked out, or as it is.
  const now = s['attachments.allow_images'] ? 'as_is' : (s['attachments.redact_images'] ? 'redacted' : 'text');
  const choices = [
    ['text', 'Text only (recommended): the picture is never sent; its text is read here and filtered'],
    ['redacted', 'Redacted picture: sent with client names, identifiers and secrets blacked out, plus its text'],
    ['as_is', 'Picture as it is: sent unfiltered'],
  ];
  const mode = h('div', { class: 'images-mode' }, ...choices.map(([value, label]) => h('label', { class: 'check' },
    h('input', { type: 'radio', name: 'images_mode', value, checked: value === now ? true : null }), ' ', label)));
  const picked = () => (mode.querySelector('input:checked') || {}).value || 'text';
  // Through the tunnel the three above stay as they are (they let pictures leave: local only); what the Session page
  // does with a picture can be changed from anywhere (D216).
  const remote = Boolean(view.config.remote);
  if (remote) {
    for (const radio of mode.querySelectorAll('input')) { radio.disabled = true; radio.title = LOCAL_ONLY; }
  }
  const session = sessionPicturesField(s, picked);
  mode.addEventListener('change', session.follow);
  const section = card('images', 'Pictures',
    'Text only (recommended): the image itself is never sent. Its text is read on this machine, rows and columns intact, and that text is filtered like anything else. Redacted picture: the picture goes too, with every client name, unmapped identifier and secret the reader can see blacked out, and it is read again before it leaves - a name still legible keeps it back. What the reader cannot read (handwriting, a logo, tiny or rotated text) is not blacked out. Picture as it is: JPEG, PNG, GIF and WebP are sent unfiltered, so any client name in them reaches the provider (replies are still filtered). Other image types are always withheld. PDFs and Office documents are read to text and filtered either way.',
    [mode,
     session.field,
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
    () => ({ ...(remote ? {} : { 'attachments.allow_images': picked() === 'as_is', 'attachments.redact_images': picked() === 'redacted' }),
      'session.picture_mode': session.value() }));
  // On, this one sends pictures to the provider unfiltered, which is the same class of thing.
  return danger(section, s['attachments.allow_images'], 'IMAGES ARE SENT UNFILTERED');
}

// D216: what the Session page does with a picture pasted, dropped, uploaded or read from the file tree. With Pictures on
// Text only, attaching would only have the proxy put the picture's text in its place, so the two attach choices are
// off and say so - and Convert to text is what is saved.
function sessionPicturesField(s, pictures) {
  const choices = [
    ['text', 'Convert to text: read on this machine, and the text put in the prompt to check'],
    ['image', 'Attach image only: the picture goes with the prompt, as Pictures above says'],
    ['image_text', 'Attach image and text: the picture goes, and its text is read into the prompt as well'],
  ];
  const saved = s['session.picture_mode'] || 'text';
  const group = h('div', { class: 'images-mode session-picture-mode' }, ...choices.map(([value, label]) => h('label', { class: 'check' },
    h('input', { type: 'radio', name: 'session_picture_mode', value, checked: value === saved ? true : null }), ' ', label)));
  const why = h('p', { class: 'hint session-picture-why', hidden: true },
    'Attaching needs Redacted picture or Picture as it is above: on Text only, the proxy would replace the picture with its text anyway.');
  const field = h('fieldset', { class: 'session-pictures' }, h('legend', {}, 'On the Session page'), group, why);
  const follow = () => {
    const textOnly = pictures() === 'text';
    for (const radio of group.querySelectorAll('input')) {
      if (radio.value === 'text') continue;
      radio.disabled = textOnly;
      radio.title = textOnly ? 'Pictures is on Text only' : '';
      if (textOnly && radio.checked) group.querySelector('input[value="text"]').checked = true;
    }
    why.hidden = !textOnly;
  };
  follow();
  const value = () => (group.querySelector('input:checked') || {}).value || 'text';
  return { field, follow, value };
}

function nearMissCard(s) {
  const on = checkbox('near_miss', 'Correct misreadings in text read from images and documents',
    s['detect.near_miss']);
  return card('near-miss', 'Near-misses',
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
  return card('folders', 'Folders the pages can open', hint,
    [h('div', { class: 'form-row' }, area), inUse, refused],
    () => ({ 'folders.roots': area.value.split('\n').map((line) => line.trim()).filter(Boolean) }));
}

// Which folders inside a repository hold output rather than something somebody wrote. A scan reads
// them either way, but only keeps a find that visibly belongs to a client.
function scanMarkersCard(s) {
  const area = h('textarea', { name: 'markers', rows: '3', 'aria-label': 'Machine-written folders',
    placeholder: 'scripts/output\ndiaglogs-\nintune-portal-export' });
  area.value = (s['scan.generated_markers'] || []).join('\n');
  return card('scan-markers', 'Generated-output folders',
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
  const section = card('uninspected', 'Unchecked endpoints',
    'The filter works on text it can find inside a parsed body, so a request whose body is not JSON - a file upload, a form post, anything binary - is refused rather than sent unread. An endpoint listed here is exempt, and exempt means nothing is done to it: no names replaced, no identifiers detected, nothing held back, and no canary check before it leaves. It is the only path through this proxy with no safety net under it. The exemption is per endpoint and never per file, so listing an upload endpoint exempts every upload through it, not the one you had in mind. Set it in aicleaner-config.json (allow_non_json_paths) and restart; there is no control here because this is not a setting to tune.',
    body, null);
  if (listed.length) section.classList.add('bad');
  return section;
}

// What happens to something new, per kind (walkthrough topic 9, DECISIONS 46). One card in place of
// "Hold back as placeholders" and "Kinds automatically tokenised", which interacted in a way the page
// never showed. Stored in the same settings: quiet_kinds = Auto-tokenise, placeholder_kinds =
// Tokenise & Review, neither = Ignore; owner_add_kinds (and, for a resource name,
// auto_map_client_resources) = the Auto-add ticks with Tokenise & Review; ignore_add_kinds = the same
// tick with Ignore (E3, Brad 2026-10-19: ignore, but add what proves its owner). Each mode keeps its own.
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
    const proof = OWNER_EXAMPLES[kind] ? ` - ${OWNER_EXAMPLES[kind]} -` : '';
    const added = OWNED_KINDS.includes(kind) && autoAdd
      ? ` But when the value itself proves whose it is${proof} it is added to that client and replaced from then on.`
      : '';
    return `${what} is sent to the AI service as written and is not offered in Review.${added}`
      + (kind === 'email' && !autoAdd ? ` ${EMAIL_IGNORE_WARNING}` : '');
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
  const ignoreAdd = new Set(s['detect.ignore_add_kinds'] || []);
  // The tick's state for each mode it appears in, so switching a row never carries one into the other.
  const chosen = Object.fromEntries(kinds.map((kind) => [kind, { review: ownerAdd.has(kind), ignore: ignoreAdd.has(kind) }]));
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
      ? h('input', { type: 'checkbox', name: `owner-${kind}`, checked: chosen[kind][modeOf(kind)] || false, 'aria-label': `${kindLabel(kind)}: auto-add to client` })
      : null;
    const warn = kind === 'email' ? h('div', { class: 'hint kind-warning', hidden: true }, EMAIL_IGNORE_WARNING) : null;
    // The help mark (an emoji, written as its escape, so it stands out - the user) shows what the row's choices add up to: on hover (title), and on a click or tap, which
    // opens it under the type - a phone has no hover. Kept current as the choices change.
    const effect = h('div', { class: 'hint kind-effect', hidden: true, id: `kind-effect-${kind}` });
    const help = h('button', { type: 'button', class: 'link kind-help', 'aria-expanded': 'false',
      'aria-controls': effect.id, 'aria-label': `What the choices for ${kindLabel(kind)} will do` }, '\u2753');
    let shownFor = modeOf(kind);
    const sync = () => {
      const mode = radios.find((r) => r.checked).value;
      if (tick && mode !== shownFor) {
        if (shownFor !== 'auto') chosen[kind][shownFor] = tick.checked;
        tick.checked = mode !== 'auto' && Boolean(chosen[kind][mode]);
        shownFor = mode;
      }
      if (tick) tick.hidden = mode === 'auto';
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
    if (tick) tick.addEventListener('change', () => { if (shownFor !== 'auto') chosen[kind][shownFor] = tick.checked; sync(); });
    const row = h('tr', { dataset: { kind } }, h('td', {}, kindLabel(kind), ' ', help, effect, warn),
      radios.map((r) => h('td', {}, r)), h('td', {}, tick || '-'));
    sync();
    return row;
  });
  const table = h('table', { class: 'kinds-table' }, h('thead', {}, head), h('tbody', {}, fixed, body));
  const read = () => {
    const picked = Object.fromEntries(rows.map((kind) => [kind, table.querySelector(`input[name="mode-${kind}"]:checked`).value]));
    const owned = rows.filter((kind) => OWNED_KINDS.includes(kind));
    return {
      'detect.quiet_kinds': rows.filter((kind) => picked[kind] === 'auto'),
      'detect.placeholder_kinds': rows.filter((kind) => picked[kind] === 'review'),
      'detect.owner_add_kinds': owned.filter((kind) => kind !== 'client-resource' && chosen[kind].review),
      'detect.auto_map_client_resources': Boolean(chosen['client-resource'] && chosen['client-resource'].review),
      'detect.ignore_add_kinds': owned.filter((kind) => chosen[kind].ignore),
    };
  };
  return card('kinds', 'New names found',
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
  // Job cleanup (2026-10-24): off, a session's job only counts its processes; on, a broker that crashes or is
  // killed takes its whole tree with it. Stop still lets work in hand finish (D112).
  // Two ways of working with a session (Brad, 2026-10-27).
  const thinking = checkbox('show-thinking', 'Show thinking: Claude\'s thinking is drawn expanded in the chat (off: folded to one line, opened with a click)',
    s['session.show_thinking'] !== false);
  const summary = checkbox('turn-summary', 'Turn summary: a message sent while Claude works is answered when the task is done, unless it asks for something now ("quickly change x, then carry on")',
    s['session.turn_summary'] === true);
  const cleanup = checkbox('job-cleanup', "End a session's leftover processes when its broker ends (a crash, a kill or a shutdown) - new sessions only",
    s['session.job_cleanup'] === true);
  // Caps on each session as a whole (D208): a runaway - a fork bomb, a leak - is refused inside its own session
  // instead of freezing the machine. 0 = no cap.
  const capProcs = h('input', { type: 'number', name: 'max_processes', min: '0', max: '100000', step: '10',
    value: String(s['session.max_processes'] ?? 400) });
  const capMemory = h('input', { type: 'number', name: 'max_memory_gb', min: '0', max: '1024', step: '1',
    value: String(s['session.max_memory_gb'] ?? 16) });
  // Stability spec S2: the Session page offers to stop a session idle this long. An offer only, never a stop.
  const idleHoursIn = h('input', { type: 'number', name: 'idle_offer_hours', min: '0', max: '720', step: '1',
    value: String(s['session.idle_offer_hours'] ?? 8) });
  // The casual-chats folder (item J): asked for here when Project Sessions is on and none is chosen.
  const casual = casualFolderField(null);
  const casualAsk = h('p', { class: 'hint casual-ask', hidden: true },
    'Choose a folder for casual chats - chats that belong to no client or project, started with Chat now in the menu.');
  const askIfNeeded = () => {
    casualAsk.hidden = !(on.querySelector('input').checked && !s['session.casual_folder'] && !casual.input.value.trim());
  };
  on.querySelector('input').addEventListener('change', askIfNeeded);
  setTimeout(askIfNeeded, 0);
  return card('session', 'Session page',
    'On: the Session page can start Claude Code on this machine in your profiles\' folders, with every request filtered and every action waiting for your approval. Through the tunnel it works from your phone. Off: the page and its API refuse, and switching it off stops any session that is running. Takes effect straight away.',
    [h('div', { class: 'form-row' }, on),
      h('div', { class: 'form-row' }, h('label', { class: 'field' },
        'Sessions running at once (0 = no cap; the machine is the limit). Lowering it stops nothing - it only refuses new starts until enough have stopped.', most)),
      h('div', { class: 'form-row' }, titles),
      h('div', { class: 'form-row' }, thinking),
      h('div', { class: 'form-row' }, summary),
      h('div', { class: 'form-row' }, cleanup),
      h('div', { class: 'form-row' }, h('label', { class: 'field' },
        'Most processes one session may run at once (0 = no cap). New sessions only.', capProcs)),
      h('div', { class: 'form-row' }, h('label', { class: 'field' },
        'Offer to stop a session idle this many hours (0 = never; it is only ever an offer).', idleHoursIn)),
      h('div', { class: 'form-row' }, h('label', { class: 'field' },
        'Most memory one session may use, in GB (0 = no cap). Past it the session\'s own allocations fail, not the machine\'s. New sessions only.', capMemory)),
      h('h3', {}, 'Casual chats'), casualAsk, casual.element],
    () => ({ 'session.enabled': on.querySelector('input').checked,
      'session.auto_titles': titles.querySelector('input').checked,
      'session.job_cleanup': cleanup.querySelector('input').checked,
      'session.show_thinking': thinking.querySelector('input').checked,
      'session.turn_summary': summary.querySelector('input').checked,
      'session.max_processes': capProcs.value.trim() === '' ? 400 : Math.max(0, Math.floor(Number(capProcs.value) || 0)),
      'session.max_memory_gb': capMemory.value.trim() === '' ? 16 : Math.max(0, Math.floor(Number(capMemory.value) || 0)),
      'session.idle_offer_hours': idleHoursIn.value.trim() === '' ? 8 : Math.max(0, Math.floor(Number(idleHoursIn.value) || 0)),
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
  // Where the key is kept reaches since H2 (Brad, 2026-10-19: "more about the storage than the code
  // itself, is protected either way"); making a code stays on the machine.
  if (remote) {
    generate.disabled = true;
    generate.title = LOCAL_ONLY;
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
    () => ({ 'remote.encrypt': on.querySelector('input').checked, 'remote.key_storage': storage.value }),
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
  return card('highlight', 'Real names in green',
    'Names the proxy restored in a reply are shown in green, so you can see what the filter did and read an odd answer in that light - the AI saw a token where you see the name. The session page always does this. Claude Code gets it as a terminal colour: turn it on and look at a reply, and if the VS Code panel shows raw codes such as [32;22m instead of colour, turn it off again. Claude Desktop is not coloured.',
    [h('div', { class: 'form-row' }, terminal)],
    () => ({ 'highlight.terminal': terminal.querySelector('input').checked }));
}

// Claude Code on Windows (feature 98): its Bash tool halves doubled backslashes, so a hook stops a
// Bash call holding them. Shown on Windows only; saving rewrites the hook in Claude Code's settings.
function bashGuardCard(s) {
  // Named for PowerShell, which is what people look for (D172, the user 2026-10-09).
  const guard = checkbox('bash-guard', 'Use PowerShell, and stop Bash commands that contain a doubled backslash', s['claude.bash_guard']);
  return card('bash-guard', 'Claude Code: PowerShell',
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
  return card('codetab', 'Claude Desktop: Code tab',
    'The Code tab does not use aicleaner\'s usual routing. With this on, Claude Code\'s settings send its traffic through a proxy on this machine: calls to Anthropic are read and filtered like VS Code\'s, sign-in and updates pass through untouched, and its error-log upload is blocked. Only Claude Code trusts the certificate aicleaner uses, and that certificate can only vouch for api.anthropic.com. Commands Claude Code runs and the MCP servers in its own settings connect directly, as before; MCP servers from a project\'s .mcp.json or a plugin pass through the proxy untouched. While aicleaner is stopped the Code tab cannot reach Anthropic - nothing is sent unfiltered. Restart the Claude Desktop app after changing it.',
    [h('div', { class: 'form-row' }, on), h('label', { class: 'field' }, 'Port', port), status],
    () => ({ 'codetab.enabled': on.querySelector('input').checked, 'codetab.port': Number(port.value) || 8788 }));
}

// GitHub Copilot in VS Code through aicleaner (Copilot release 6, 2026-10-19). Works remotely too
// (Brad, 2026-10-19), except adding its certificate: Windows asks for that at the machine.
function copilotStatus(info) {
  if (!info.enabled) return 'Off. VS Code connects to Copilot directly, unfiltered.';
  const lines = [];
  if (info.error) lines.push(info.error);
  if (info.waiting) lines.push(`Waiting: ${info.waiting}.`);
  else lines.push(info.trusted ? 'Certificate trusted.' : 'Certificate not trusted.');
  const vscode = info.vscode || {};
  if (!vscode.vscode_found) lines.push('No VS Code found on this machine.');
  else lines.push(`${vscode.pointed} of ${(vscode.files || []).length} VS Code settings file(s) point here.`);
  lines.push(info.last_at ? `Copilot traffic last seen ${timeAgo(info.last_at * 1000)}.` : 'No Copilot traffic since aicleaner started.');
  const telemetry = info.telemetry || {};
  if (telemetry.not_json) lines.push(`${telemetry.not_json} telemetry line(s) were not JSON and went as they were.`);
  return lines.join(' ');
}

function copilotCard(s) {
  const on = checkbox('copilot', 'Filter GitHub Copilot in VS Code', s['copilot.enabled']);
  const status = h('div', { class: 'hint copilot-status' }, 'Checking...');
  getJSON('/api/copilot').then((info) => { status.textContent = copilotStatus(info); })
    .catch(() => { status.textContent = ''; });
  return card('copilot', 'GitHub Copilot',
    'With this on, VS Code\'s proxy setting points at aicleaner on this machine: Copilot\'s chat, agent, inline completions, next edit suggestions, public code matching and telemetry are read and filtered; everything else VS Code sends - sign-in, extensions, Settings Sync - passes through untouched. VS Code needs to trust aicleaner\'s Copilot certificate, which can only vouch for Copilot\'s own hosts; it is added to your Windows certificates the first time this is turned on here, and Windows asks you to confirm. The first time it is turned on is done at the machine, where that confirmation is answered; the certificate then stays installed while Copilot is off, so after that it can be turned on or off from anywhere. While aicleaner is stopped VS Code\'s proxy setting is taken out, so VS Code keeps working - unfiltered - until it starts again. Microsoft 365 Copilot (Word, Teams, Outlook) is not covered: it runs in Microsoft\'s cloud. Reload VS Code after changing it.',
    [h('div', { class: 'form-row' }, on), status],
    () => ({ 'copilot.enabled': on.querySelector('input').checked }));
}

// Claude Desktop's chat, signed in to claude.ai, through aicleaner (route B, 2026-10-22). The Copilot rule: on
// and off from anywhere, the certificate added only at the machine. What claude.ai keeps is said before it is on.
function claudeaiStatus(info) {
  if (!info.enabled) return 'Off. Claude Desktop\'s chat goes to claude.ai directly, unfiltered.';
  const lines = [];
  if (info.error) lines.push(info.error);
  if (info.broken) lines.push(`Refusing the chat: ${info.broken}.`);
  if (info.waiting) lines.push(`Waiting: ${info.waiting}.`);
  else lines.push(info.trusted ? 'Certificate trusted.' : 'Certificate not trusted.');
  const windows = info.windows || {};
  lines.push(windows.pointed ? 'The Windows proxy setting points at aicleaner.' : 'The Windows proxy setting does not point at aicleaner.');
  if (windows.had_pac) lines.push('Your earlier proxy script still decides every other site.');
  else if (windows.had_proxy) lines.push('Your earlier proxy still carries every other site.');
  if (info.safeguards && windows.pointed) {
    lines.push('If aicleaner stops, the chat fails rather than going out unfiltered, and the watchdog starts aicleaner again within 5 minutes and tells you.');
  }
  lines.push(info.last_at ? `claude.ai traffic last seen ${timeAgo(info.last_at * 1000)}.` : 'No claude.ai traffic since aicleaner started.');
  const refused = Object.values(info.refused || {}).reduce((a, b) => a + b, 0);
  if (refused) lines.push(`${refused} request(s) refused (see the Log page).`);
  return lines.join(' ');
}

function claudeaiCard(s) {
  const wasOn = Boolean(s['claudeai.enabled']);
  const on = checkbox('claudeai', 'Filter Claude Desktop\'s chat (signed in to claude.ai)', wasOn);
  const understood = checkbox('claudeai_understood', 'I understand: claude.ai keeps my conversations with tokens in place of client names', false);
  // Said before it can be turned on (spec, section 5): the switch stays greyed until this is ticked.
  const warning = h('div', { class: 'hint claudeai-warning', hidden: wasOn },
    'Before you turn this on: your conversations, project files, titles and memory are stored on claude.ai with tokens in place of client names. On this machine they read with the real names; in claude.ai in another browser, on your phone or on another computer they show the tokens, and claude.ai\'s own search works on the tokens.',
    h('div', { class: 'form-row' }, understood));
  const box = on.querySelector('input');
  const agreed = understood.querySelector('input');
  box.disabled = !wasOn;
  agreed.addEventListener('change', () => {
    box.disabled = !agreed.checked;
    if (!agreed.checked) box.checked = false;
  });
  const status = h('div', { class: 'hint claudeai-status' }, 'Checking...');
  getJSON('/api/claudeai').then((info) => { status.textContent = claudeaiStatus(info); })
    .catch(() => { status.textContent = ''; });
  return card('claudeai', 'Claude Desktop: chat',
    'With this on, the Windows proxy setting sends claude.ai - and only claude.ai - to aicleaner: what you type, project instructions and knowledge, and the files you attach (read on this machine and sent as filtered text) go out with client names replaced, and replies come back with them restored. claude.ai in Edge on this machine is filtered too. Every other site goes where it went before, through your own proxy if you have one. aicleaner\'s claude.ai certificate, which can only vouch for claude.ai, is added to your Windows certificates the first time this is turned on here, and Windows asks you to confirm; after that it can be turned on or off from anywhere. While aicleaner\'s forward proxy is down, Claude Desktop\'s chat fails rather than going out unfiltered. Restart Claude Desktop after changing it. No API key or credit is needed: it is your own claude.ai sign-in.',
    [h('div', { class: 'form-row' }, on), warning, status],
    () => ({ 'claudeai.enabled': box.checked }));
}

// Reading text out of images and scanned PDFs (J2, Brad 2026-10-20: these had no card). Remote or local.
function ocrCard(s) {
  const on = checkbox('ocr', 'Read the text in images and scanned PDFs (so it is filtered as text)', s['attachments.ocr']);
  const pages = h('input', { type: 'number', name: 'ocr_max_pages', min: '1', max: '500', class: 'narrow',
    value: String(s['attachments.ocr_max_pages'] || 10), 'aria-label': 'Most pages read from a scanned PDF' });
  const seconds = h('input', { type: 'number', name: 'ocr_timeout_s', min: '5', max: '600', class: 'narrow',
    value: String(s['attachments.ocr_timeout_s'] || 60), 'aria-label': 'Longest a reading may take, in seconds' });
  return card('ocr', 'Text in pictures and scans',
    'An image or a scanned PDF in a request is read on this machine, and the text it holds is filtered like any other text before anything leaves. With this off, such attachments are replaced by a notice instead. The limits keep a large scan from holding a request up.',
    [h('div', { class: 'form-row' }, on),
      h('div', { class: 'form-row' }, h('label', { class: 'field' }, 'Most pages read from a scanned PDF', pages),
        h('label', { class: 'field' }, 'Longest a reading may take (seconds)', seconds))],
    () => ({ 'attachments.ocr': on.querySelector('input').checked,
      'attachments.ocr_max_pages': Number(pages.value) || 10,
      'attachments.ocr_timeout_s': Number(seconds.value) || 60 }));
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
  // Feature 143: Claude Haiku suggests code when you pause typing in a file tab.
  const inline = checkbox('inline_suggest', 'Suggest code as you pause (Claude Haiku, through aicleaner)',
    s['editor.inline_suggest'] !== false);
  // Which service suggests the code (spec 2026-10-23-editor-suggestion-services.md). VS Code's own Copilot
  // completions cannot be reached from aicleaner (GitHub refuses the token), so "auto" takes Claude, Copilot, Codex.
  const serviceChoices = [
    ['auto', 'Automatic - the first signed in of Claude, Copilot, Codex'],
    ['claude', 'Claude (Haiku) - filtered'],
    ['copilot', 'GitHub Copilot CLI - UNFILTERED, as VS Code sends it'],
    ['copilot_filtered', 'GitHub Copilot CLI - filtered (unsupported)'],
    ['codex', 'Codex (ChatGPT) - filtered'],
    ['off', 'Off'],
  ];
  const service = h('select', { name: 'inline_service', 'aria-label': 'Which service suggests code' },
    ...serviceChoices.map(([value, label]) => h('option', { value, selected: (s['editor.inline_service'] || 'auto') === value ? true : null }, label)));
  const serviceState = h('div', { class: 'hint editor-service-state' }, 'Checking which services are signed in...');
  const describe = (info) => {
    const lines = Object.entries(info.services || {}).map(([key, one]) =>
      `${one.label}: ${one.available ? 'signed in' : 'not available here'} - ${one.cost}.`);
    const active = (info.services || {})[info.active];
    lines.unshift(info.active ? `Answering now: ${active.label}.` : 'No service answers now (off, or none signed in).');
    if (info.active === 'copilot') lines.splice(1, 0, 'Unfiltered: file contents, client names included, go to GitHub as written.');
    serviceState.textContent = lines.join(' ');
  };
  getJSON('/api/editor/services').then(describe).catch(() => { serviceState.textContent = ''; });
  // The minimap down the right of a file tab: off by default (Brad, 2026-10-23).
  const minimap = checkbox('minimap', 'Show the minimap (the file in miniature, down the right edge)', s['editor.minimap'] === true);
  return card('editor', 'File editor',
    'Double-clicking a word in a Markdown preview can take you to the same word in the source, selected '
    + 'and ready to edit - handy for reading in the preview and then changing what you read. With suggestions '
    + 'on, a pause in typing in a file tab asks the chosen service for the code that comes next: it appears in '
    + 'grey a moment later, Tab takes it and typing on leaves it. Each answered pause is one small turn on that '
    + 'service\'s plan. Claude and Codex are filtered like everything else; Copilot unfiltered sends the file as '
    + 'written, as VS Code does. VS Code\'s own Copilot suggestions stay in VS Code: GitHub does not let another '
    + 'program use them. Alt+\\ still asks the session\'s AI on purpose.',
    [h('label', { class: 'field' }, 'Double-click in a Markdown preview', pick), h('div', { class: 'form-row' }, inline),
      h('label', { class: 'field' }, 'Which service suggests code', service), serviceState,
      h('div', { class: 'form-row' }, minimap)],
    () => ({ 'editor.preview_dblclick': pick.value, 'editor.inline_suggest': inline.querySelector('input').checked,
      'editor.inline_service': service.value, 'editor.minimap': minimap.querySelector('input').checked }),
    () => forgetPreviewChoice());
}

// Autocorrect in the Session page's prompt (feature 94). Its only switch since its button left the
// prompt row (the user, 2026-10-18). Code and paths are always left alone; the rest is the user's choice.
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
    [until, h('div', { class: 'form-row' }, timings), processesPanel()].filter(Boolean),
    () => ({ 'debug.timings': timings.querySelector('input').checked }));
}

// Processes (spec 2026-10-24-session-lifecycle-instrumentation.md): memory, each session's processes as its job
// counts them, anything left behind with no owner, the last pause (a host asleep freezes this VM), and the lifecycle
// log's recent events. Read only. The log itself is logs\lifecycle-<host>-<date>.jsonl.
const LIFECYCLE_WORDS = {
  'session-start': 'started', 'stop-requested': 'stop asked', 'stop-result': 'stopped', kill: 'killed',
  'cli-exit': 'Claude Code ended', 'broker-hold': 'held for work in hand', 'broker-hold-timeout': 'hold ended (time limit)',
  'broker-exit': 'broker ended',
  'limit-hit': 'reached its cap', pause: 'paused', orphan: 'left behind', gone: 'ended', 'proxy-start': 'aicleaner started', 'proxy-stop': 'aicleaner stopped',
};

function eventDetail(e) {
  if (e.event === 'stop-result') return `${e.how} in ${e.seconds} s (${e.why})`;
  if (e.event === 'stop-requested') return e.why || '';
  if ((e.event === 'proxy-stop' || e.event === 'broker-exit') && e.why) return `(${e.why})`;
  if (['cli-exit', 'broker-exit', 'broker-hold', 'broker-hold-timeout'].includes(e.event)) {
    const left = (e.left || []).length;
    return `${e.exit_code !== undefined ? `exit ${e.exit_code}, ` : ''}${left} process${left === 1 ? '' : 'es'} still running`;
  }
  if (e.event === 'limit-hit') return `${e.limit} (${(e.job || {}).active} processes, ${(e.job || {}).peak_job_mb} MB peak)`;
  if (e.event === 'pause') return `${Math.round(e.wall_s / 60)} min (${e.kind})`;
  if (e.event === 'orphan') return `${e.name} pid ${e.pid}, ${e.tree_mb} MB with ${(e.tree || []).length} below it, ${e.age_min} min old`;
  if (e.event === 'gone') return `${e.name} pid ${e.pid}`;
  return '';
}

function processesPanel() {
  const body = h('div', { class: 'processes-body' }, h('p', { class: 'hint' }, 'Loading...'));
  const refresh = h('button', { type: 'button', class: 'processes-refresh' }, 'Look again');
  const load = async (fresh) => {
    refresh.disabled = true;
    try {
      const data = await getJSON(`/api/diagnostics/processes${fresh ? '?fresh=1' : ''}`);
      body.replaceChildren(...processesView(data));
    } catch (error) {
      body.replaceChildren(h('p', { class: 'error-text' }, String(error.message || error)));
    } finally {
      refresh.disabled = false;
    }
  };
  refresh.addEventListener('click', () => load(true));
  setTimeout(() => load(false), 0);
  return h('div', { class: 'processes' }, h('h3', {}, 'Processes'), body, refresh);
}

function processesView(data) {
  if (!data.on) return [h('p', { class: 'hint' }, 'Not watching: this aicleaner is not an installed copy.')];
  const snap = data.snapshot || {};
  const mem = snap.memory || {};
  const sessions = snap.sessions || [];
  const orphans = snap.orphans || [];
  const out = [h('p', { class: 'processes-memory' },
    `Memory committed ${Math.round((mem.commit_mb || 0) / 1024)} of ${Math.round((mem.commit_limit_mb || 0) / 1024)} GB `
    + `(peak ${Math.round((mem.commit_peak_mb || 0) / 1024)} GB), ${Math.round((mem.available_mb || 0) / 1024)} GB free; `
    + `${mem.processes || 0} processes, ${mem.handles || 0} handles. Job cleanup ${data.job_cleanup ? 'on' : 'off'}; `
    + `each new session capped at ${data.max_processes || 'unlimited'} processes and `
    + `${data.max_memory_mb ? `${Math.round(data.max_memory_mb / 1024)} GB` : 'unlimited memory'}.`)];
  out.push(h('table', { class: 'processes-sessions' },
    h('thead', {}, h('tr', {}, ['Session', 'Processes', 'Peak MB', 'Broker', 'Claude Code'].map((t) => h('th', {}, t)))),
    h('tbody', {}, sessions.length ? sessions.map((sv) => h('tr', {},
      h('td', {}, sv.name), h('td', {}, sv.job ? String(sv.job.active) : (sv.broker_error ? 'no answer' : '-')),
      h('td', {}, sv.job ? String(sv.job.peak_job_mb) : '-'), h('td', {}, String(sv.broker_pid || '-')),
      h('td', {}, String(sv.cli_pid || '-'))))
      : [h('tr', {}, h('td', { colspan: '5', class: 'muted' }, 'No session running.'))])));
  out.push(orphans.length
    ? h('div', { class: 'processes-orphans' }, h('p', { class: 'chip warn' },
      `${orphans.length} left behind, ${snap.orphan_mb} MB`),
    h('ul', {}, orphans.map((o) => h('li', { class: 'mono' }, eventDetail({ ...o, event: 'orphan' }), ` - ${o.path || ''}`))))
    : h('p', { class: 'hint processes-orphans' }, 'Nothing left behind.'));
  if (snap.last_pause) out.push(h('p', { class: 'hint' }, `Last pause: ${eventDetail(snap.last_pause)} from ${snap.last_pause.since}.`));
  const events = data.events || [];
  if (events.length) {
    out.push(h('details', { class: 'processes-events' }, h('summary', {}, `Recent lifecycle events (${events.length})`),
      h('ul', {}, events.map((e) => h('li', {}, `${(e.ts || '').slice(5, 16).replace('T', ' ')} `,
        h('strong', {}, LIFECYCLE_WORDS[e.event] || e.event), e.session ? ` ${e.session}` : '', ` ${eventDetail(e)}`)))));
  }
  return out;
}

// The escape hatch, and it looks like one: a false positive in the canary refuses every request,
// including the ones to the assistant you would use to fix it. Off is the only safe setting.
// TEMPORARY, while the filter is being debugged: the proxy still carries the traffic, but does
// nothing to it. First card on the page, because everything below it is moot while it is on.
function bypassCard(s) {
  const on = checkbox('bypass', 'TEMPORARY: forward every request untouched (no filtering at all)', s['proxy.bypass']);
  const section = card('bypass', 'Bypass filtering',
    'On: model traffic passes straight through. Nothing is replaced, nothing is held back, secrets are not redacted, images and documents are not withheld, the canary does not check, and replies are not restored - so real client names, and anything else in a prompt, reach the AI provider exactly as written. Every request is logged as PROXY BYPASS. This exists so the filter can be switched off without an uninstall while its own faults are worked through, and is to be removed before the tool is relied on.',
    [h('div', { class: 'form-row' }, on)],
    () => ({ 'proxy.bypass': on.querySelector('input').checked }));
  return danger(section, s['proxy.bypass'], 'BYPASS IS ON: NOTHING IS FILTERED');
}

function failOpenCard(s) {
  const on = checkbox('fail-open', 'EMERGENCY: send requests the canary would refuse', s['canary.fail_open']);
  const section = card('fail-open', 'Send what the canary refuses',
    'Only for getting past a false 403 ("a real client identifier survived tokenisation"). While this is on, a request the canary would refuse is sent anyway - if the match was real, that client name reaches the AI provider. Every such request is written to the evidence log as FAIL-OPEN, and each match - with the text around it - goes to the newest logs/proxy-stderr-*.log and the Log page. It can be turned on from the phone, because a false positive stays in the conversation and would otherwise refuse every request until you are back at this machine. Turn it off as soon as you are past the problem.',
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
  const section = card('restart', 'Restart',
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

// Running aicleaner (feature 107, decided 2026-10-24): started with the computer, before anyone signs in, as you -
// a start-up task with your password kept by Windows. Local only: it needs an administrator prompt and your
// password on this machine. Through remote access the card is shown, locked, saying why ("remote shows everything").
const SERVICE_MODAL = {
  title: 'Start aicleaner when this computer starts?',
  parts: [
    ['What changes', [
      'A start-up task runs aicleaner as you when the computer starts - before anyone signs in - and remote access with it, when that is set up.',
      'Windows keeps your password for the task, encrypted, as it does for any service that runs as a person.',
      'If aicleaner stops, it is started again within seconds; Restart and Stop work as today.',
      'The start at sign-in stays, as a fallback: it finds aicleaner already running and leaves it be.',
    ]],
    ['Benefits', [
      'Remote access works after a reboot without anyone signing in - for a VM or a machine you reach from elsewhere.',
      'It keeps running when you sign out.',
    ]],
    ['Things to consider', [
      'Your Windows password is stored by Windows for the task.',
      'When you change your password, the start-up task cannot sign in until you enter the new one here. Meanwhile aicleaner starts at your sign-in, as now, and says so.',
      'It runs with your limited token, as today - not elevated, even if your account is an administrator.',
      'Windows asks for administrator now, on this machine, and then a window asks for your password.',
    ]],
  ],
};

function askService(on) {
  return new Promise((resolve) => {
    let answered = false;
    const finish = (yes) => {
      if (answered) return;
      answered = true;
      dialog.close();
      dialog.remove();
      resolve(yes);
    };
    const cancel = h('button', { type: 'button', class: 'service-cancel' }, 'Cancel');
    const proceed = h('button', { type: 'button', class: 'primary service-proceed' }, 'Proceed');
    cancel.addEventListener('click', () => finish(false));
    proceed.addEventListener('click', () => finish(true));
    const body = on
      ? SERVICE_MODAL.parts.map(([heading, items]) => h('div', { class: 'service-part' },
        h('h3', {}, heading), h('ul', {}, items.map((item) => h('li', {}, item)))))
      : [h('p', {}, 'The start-up task is removed and aicleaner starts at your sign-in again, as before. Windows forgets '
        + 'the password it kept for the task. Windows asks for administrator on this machine.')];
    const dialog = h('dialog', { class: 'sheet confirm-dialog service-modal', 'aria-label': on ? SERVICE_MODAL.title : 'Stop starting with the computer?' },
      h('h2', {}, on ? SERVICE_MODAL.title : 'Stop starting aicleaner with the computer?'),
      ...body,
      h('div', { class: 'fb-footer' }, cancel, proceed));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); finish(false); });
    document.body.append(dialog);
    arrangeSheet(dialog, { dirty: () => false });
    dialog.showModal();
    cancel.focus();
  });
}

function serviceCard() {
  const remote = Boolean(view.config.remote);
  const status = h('p', { class: 'service-status' }, 'Checking...');
  const box = h('input', { type: 'checkbox', name: 'run_as_service', disabled: true });
  const label = h('label', { class: 'check' }, box, ' ', 'Start aicleaner when this computer starts, before anyone signs in');
  const again = h('button', { type: 'button', class: 'service-password', hidden: true }, 'Enter the password again');
  // Feature 107: the start-up task gives an administrator account its full token; aicleaner lowers it at start. If
  // that ever fails, this says so rather than leaving it to be found in /healthz.
  const elevated = h('p', { class: 'callout bad service-elevated', hidden: true },
    'This copy of aicleaner is running with an administrator token, and so would the sessions it starts. It should '
    + 'have lowered it at start - see the newest logs\\proxy-stderr log, or switch this off until it is fixed.');
  const problem = h('p', { class: 'callout bad service-problem', hidden: true },
    'aicleaner\'s start-up task could not sign in - has your Windows password changed? aicleaner starts at your sign-in '
    + 'meanwhile. Enter the password again to put it right.');
  let current = { mode: 'off' };
  const show = (state) => {
    current = state;
    const on = state.mode === 'user';
    box.checked = on;
    box.disabled = remote;
    const task = state.task || {};
    status.textContent = on
      ? `On: aicleaner starts with this computer, as ${state.user || 'you'}${task.state ? ` (start-up task: ${task.state.toLowerCase()})` : ''}.`
        + (state.running_as_service ? ' This copy was started by it.' : '')
      : 'Off: aicleaner starts when you sign in.';
    problem.hidden = !state.password_problem;
    elevated.hidden = state.elevated !== true;
    again.hidden = !state.password_problem;
    again.disabled = remote;
  };
  const load = () => getJSON('/api/service').then(show)
    .catch((error) => { status.textContent = `Could not check: ${error.message || error}`; });
  const ask = async (on) => {
    const result = await postJSON('/api/service', { on });
    say(result.said, 'ok');
    status.textContent = on ? 'Waiting for the administrator prompt and your password on this machine...'
      : 'Waiting for the administrator prompt on this machine...';
  };
  box.addEventListener('change', async () => {
    const on = box.checked;
    box.checked = !on;                   // stays as it was until the change is confirmed
    if (!(await askService(on))) return;
    box.checked = on;
    try { await ask(on); } catch (error) { box.checked = !on; sayError(error); }
  });
  again.addEventListener('click', async () => {
    try { await ask(true); } catch (error) { sayError(error); }
  });
  load();
  const section = card('service', 'Start with the computer',
    'Normally aicleaner starts when you sign in. It can start with the computer instead, before anyone signs in, still as you - so remote access works after a reboot. Changed on this machine only: Windows asks for administrator and your password.',
    [status, h('div', { class: 'form-row' }, label), elevated, problem, h('div', { class: 'form-row' }, again)], null);
  if (remote) {
    section.classList.add('local-only');
    section.title = LOCAL_ONLY;
    box.title = LOCAL_ONLY;
    again.title = LOCAL_ONLY;
    section.querySelector('h2').after(h('p', { class: 'chip local-only-note' },
      `${LOCAL_ONLY}: it needs an administrator prompt and your Windows password there.`));
  }
  return section;
}

// What each AI client on this machine is currently pointed at. Read-only, and read fresh on every
// status poll: the values change outside the application, which is the whole reason to show them.
function routingCard() {
  const routing = (view.ctx.status && view.ctx.status.routing) || {};
  const rows = routing.clients || [];
  if (!rows.length) {
    return card('routing', 'Where your AI apps point',
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
  return card('routing', 'Where your AI apps point',
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
  const apply = h('button', { type: 'button', class: 'primary updates-apply' }, 'Update now');
  // Always shown (E3 part 9, feedback #2): it appeared only when a newer release existed, so someone up
  // to date could not find it. Works through remote access too since H2 (Brad, 2026-10-19): it is
  // Update automatically on demand - no new risk, and no prompt on the machine.
  const auto = checkbox('update-auto', 'Update automatically (every 6 hours, when no session is mid-turn)', s['update.auto']);
  const show = (info) => {
    const from = info.repo ? ` - from ${info.repo}${info.ref ? ` (${info.ref})` : ''}` : '';
    installedLine.textContent = `Installed: v${info.version}${from}`;
    availableLine.className = `updates-available${info.newer ? ' ok' : ''}`;
    availableLine.textContent = info.error ? `Could not check: ${info.error}`
      : info.updating ? 'An update is under way - see logs/update.log.'
        : info.newer ? `${info.latest} is available.` : info.latest ? `Up to date (newest release: ${info.latest}).` : 'No releases found.';
    checkedLine.textContent = info.checked_at ? `Last checked ${shortTime(new Date(info.checked_at * 1000).toISOString())}.` : '';
    apply.disabled = !info.newer || info.updating;
    apply.textContent = info.newer ? `Update now to ${info.latest}` : info.latest ? `Up to date (${info.latest})` : 'Update now';
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
  return card('tools', 'Names kept in tool calls',
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
  return card('public-list', 'Shared identifiers',
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
  return card('contact', 'Your details', contact.source ? `From ${contact.source}; edit that file to change it.` : null, [body], null);
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
  // One column of cards beside the groups. Two columns were tried for the whole page; with a
  // group open there are two to eight cards, read top to bottom, and the wide ones (the AI tools
  // table, the kinds table) want the width more than a second column wants the space.
  view.host = h('div', { class: 'settings-cards' });
  view.empty = h('p', { class: 'muted settings-none', hidden: true }, 'No setting matches. Try fewer or other words.');
  view.offer = h('div', { class: 'settings-add-word', hidden: true, 'data-unsaved-ignore': true });
  view.side = sidePanel();
  // Through remote access (J4, Brad 2026-10-20): why some controls are shown but cannot be used here.
  const remoteNote = view.config.remote ? h('p', { class: 'callout settings-remote-note' },
    h('b', {}, 'Through remote access: '),
    'every setting is shown, but some cannot be changed from here - those that would let client data reach the AI '
    + 'service unfiltered, those that trigger a prompt on the machine where aicleaner is deployed, and those that could '
    + 'give access to other systems (such as reading stored secrets). They are greyed out, saying so; change them at that machine.')
    : null;
  main.append(...[remoteNote, h('div', { class: 'settings-layout' }, view.side,
    h('div', { class: 'settings-main' }, view.empty, view.offer, view.host))].filter(Boolean));
  paintCards();
  openFromAddress(ctx.route && ctx.route.id);
}
