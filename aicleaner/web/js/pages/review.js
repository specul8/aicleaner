// Review: the inbox of identifiers seen on the wire that the mapping does not know. Map each to a
// client (or several at once), create a client in the page, or ignore for one client or everywhere.

import { getJSON, postJSON, say, sayError } from '../api.js';
import { bulkGroup, busyButton, focusFirst, h, icon, iconButton, kindLabel, pickBox, pickCell } from '../dom.js';
import { displayName } from '../app.js';

// `filter` and `sort` survive a reload, so mapping one batch of "acme" leaves the list as it was.
// `pending` is the queue as last listed, by value: what an undo puts back (D45).
const view = { main: null, ctx: null, lastClient: null, pendingNew: null, filter: '', sort: { key: '', dir: 1 },
  pending: new Map() };

// Natural order: vm2 before vm10, and case does not decide it.
const NATURAL = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
const SORT_KEYS = ['value', 'kind', 'count', 'seenwith'];

export function compareRows(a, b, key) {
  if (key === 'count') return Number(a.dataset.sortCount) - Number(b.dataset.sortCount);
  const field = `sort${key[0].toUpperCase()}${key.slice(1)}`;
  return NATURAL.compare(a.dataset[field] || '', b.dataset[field] || '');
}

function sortRows(table) {
  const { key, dir } = view.sort;
  for (const th of table.querySelectorAll('th[data-sort]')) {
    th.setAttribute('aria-sort', th.dataset.sort === key ? (dir > 0 ? 'ascending' : 'descending') : 'none');
  }
  if (!SORT_KEYS.includes(key)) return;
  const body = table.tBodies[0];
  // Moved, not rebuilt: ticks and choices in the Map to column stay where they were.
  const rows = [...body.rows].sort((a, b) => dir * compareRows(a, b, key) || NATURAL.compare(a.dataset.value, b.dataset.value));
  body.append(...rows);
}

function sortHeader(label, key, table, className) {
  const button = h('button', { type: 'button', class: 'sort', title: `Sort by ${label.toLowerCase()}` }, label);
  button.addEventListener('click', () => {
    view.sort = { key, dir: view.sort.key === key ? -view.sort.dir : 1 };
    sortRows(table());
  });
  // The sort key doubles as the column key: both name the same column, and a saved width that
  // outlives a column is discarded by name rather than applied to whatever now sits in its place.
  return h('th', { class: className || null, dataset: { sort: key, col: key }, 'aria-sort': 'none' }, button);
}

// What a row is matched on: the identifier first, and what else is shown beside it.
function searchText(candidate, status) {
  return [candidate.value, candidate.kind, kindLabel(candidate.kind), candidate.placeholder,
    candidate.client, ...(candidate.sources || []),
    ...seenWith(candidate).flatMap((cid) => [cid, labelOf(status, cid)]),
    ...(candidate.projects || []), ...(candidate.sessions || [])].filter(Boolean).join(' ').toLowerCase();
}

// The clients whose names were in the requests it appeared in, most often first.
function seenWith(candidate) {
  return Object.entries(candidate.seen_with || {}).sort((a, b) => b[1] - a[1]).map(([cid]) => cid);
}

function labelOf(status, cid) {
  const found = (status.clients || []).find((r) => r.id === cid);
  return found ? displayName(found) : cid;
}

// The client it most likely belongs to: the one whose names it came with, when that is clear.
export function likelyClient(candidate) {
  const ranked = Object.entries(candidate.seen_with || {}).sort((a, b) => b[1] - a[1]);
  if (!ranked.length) return '';
  if (ranked.length === 1 || ranked[0][1] >= 2 * ranked[1][1]) return ranked[0][0];
  return '';
}

// Why it is thought to be a client's: it contains the client's name, or it is one letter from a
// word of it (a misread name) - which says what the word is, so an ordinary word is easy to spot.
function whyClient(candidate) {
  if (!candidate.client) return null;
  if (candidate.near) {
    return h('div', { class: 'hint' }, 'one letter from ', h('strong', {}, candidate.near), ' in the names of ',
      h('strong', {}, candidate.client));
  }
  return h('div', { class: 'hint' }, 'carries the name of ', h('strong', {}, candidate.client));
}

// The bin: off the list for now, not ignored - if it is seen again, it comes back to Review.
const DISMISS_LABEL = 'Remove from Review (not ignored: it comes back if it is seen again)';

const items = (n) => `${n} ${n === 1 ? 'item' : 'items'}`;

// The way back from an action, offered on its message (D45 - D42's undo, which reached the client
// pages in v0.9.58 and never reached this one). Built from the records as they were listed before
// the action, because the server discards them when it acts: the count, where each was seen, and
// the placeholder a conversation under way was already sent. `inverse` names what has to be taken
// back first - `unignore` a scope, or `unmap` a client - and nothing for a plain dismiss.
function undoAction(values, inverse) {
  const records = values.map((value) => view.pending.get(value)).filter(Boolean);
  if (!records.length) return null;
  return {
    label: 'Undo',
    run: async () => {
      const answer = await postJSON('/api/candidates/restore', { candidates: records, ...inverse });
      const back = (answer.done || []).length;
      const stuck = answer.refused || [];
      say(stuck.length
        ? `Put ${items(back)} back in Review. ${stuck.length} refused - ${stuck[0].value}: ${stuck[0].why}`
        : `Put ${items(back)} back in Review.`, stuck.length ? 'warn' : 'ok');
      await reload();
    },
  };
}

// Try every value and keep what held. Stopping at the first refusal left the ones before it
// changed with nothing to say so, and nothing to undo them by (D23 on the client pages).
async function eachValue(values, send) {
  const done = [];
  const refused = [];
  for (const value of values) {
    try {
      await send(value);
      done.push(value);
    } catch (error) {
      refused.push({ value, why: error && error.message ? error.message : String(error) });
    }
  }
  return { done, refused };
}

// A bulk action from the bar: run it, say what happened with the undo attached, and reload so the
// table shows what is true now whatever happened.
async function bulkRun(values, send, words, inverse) {
  try {
    const { done, refused } = await eachValue(values, send);
    const action = undoAction(done, inverse);
    if (!refused.length) {
      say(words(done.length), 'ok', { action });
    } else {
      const first = refused[0];
      say(`${words(done.length)} ${refused.length} refused - ${first.value}: ${first.why}`
        + (refused.length > 1 ? ` (and ${refused.length - 1} more)` : ''), done.length ? 'info' : 'error', { action });
    }
  } finally {
    await reload();
  }
}

const dismissOne = (value) => postJSON('/api/candidates/resolve', { value, action: 'dismiss' });

function binCell(candidate) {
  const bin = iconButton('trash', DISMISS_LABEL, { class: 'icon dismiss' });
  bin.addEventListener('click', async () => {
    try {
      await dismissOne(candidate.value);
      say(`${candidate.value} removed from Review.`, 'ok', { action: undoAction([candidate.value], {}) });
      await reload();
    } catch (error) {
      sayError(error);
    }
  });
  return h('td', { class: 'bin' }, bin);
}

function contextCell(candidate, status) {
  const counts = candidate.seen_with || {};
  const clients = seenWith(candidate).map((cid) => h('span', { class: 'chip' }, `${labelOf(status, cid)} ${counts[cid]}x`));
  const where = [...(candidate.projects || []).map((p) => `project ${p}`),
    ...(candidate.sessions || []).map((s) => `session ${s}`)];
  if (!clients.length && !where.length) return h('td', { class: 'seen-with' }, h('span', { class: 'muted' }, 'no client'));
  return h('td', { class: 'seen-with' }, ...clients, where.length ? h('div', { class: 'hint' }, where.join(', ')) : null);
}

function targets(status) {
  const list = status.clients.map((r) => ({ id: r.id, label: displayName(r) }));
  list.push({ id: 'global', label: 'Global' });
  return list;
}

function clientSelect(status, attrs, withNew) {
  const choices = targets(status);
  const selected = choices.some((c) => c.id === view.lastClient) ? view.lastClient : (choices[0] || {}).id;
  return h('select', attrs,
    choices.map((c) => h('option', { value: c.id, selected: c.id === selected }, c.label)),
    withNew ? h('option', { value: '__new' }, '+ New client...') : null);
}

async function mapValues(values, client) {
  for (const value of values) {
    await postJSON('/api/candidates/resolve', { value, action: 'map', client });
  }
  view.lastClient = client;
}

async function ignoreValue(value, scope) {
  await postJSON('/api/ignore', { value, scope });
}

function newClientForm(values) {
  const form = h('form', { id: 'new-client', class: 'card form-row' },
    h('label', { class: 'field' }, 'New client id', h('input', { type: 'text', name: 'id', required: true, pattern: '[A-Za-z0-9](?:[A-Za-z0-9_]|-){0,63}', placeholder: 'e.g. acme' })),
    h('label', { class: 'field' }, 'Display name (optional)', h('input', { type: 'text', name: 'label', maxlength: '80', placeholder: 'e.g. ACME Pty Ltd' })),
    h('label', { class: 'field' }, 'Index (optional)', h('input', { type: 'number', name: 'index', min: '1', placeholder: 'automatic' })),
    h('button', { type: 'submit', class: 'primary' }, `Create and map ${values.length === 1 ? 'it' : `all ${values.length}`}`),
    h('button', { type: 'button', class: 'cancel' }, 'Cancel'),
    h('span', { class: 'hint' }, 'Letters, digits, - and _. The index decides the tokens and is derived from the id unless you set one.'));
  form.querySelector('.cancel').addEventListener('click', () => { form.remove(); });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = form.elements.id.value.trim();
    const index = form.elements.index.value.trim();
    const label = form.elements.label.value.trim();
    try {
      await postJSON('/api/mapping/client', { id, index: index ? Number(index) : null, label: label || null });
      await mapValues(values, id);
      say(`Created ${id} and mapped ${values.length} ${values.length === 1 ? 'item' : 'items'}.`, 'ok');
      await reload();
    } catch (error) {
      sayError(error);
    }
  });
  return form;
}

function showNewClient(values) {
  const old = document.getElementById('new-client');
  if (old) old.remove();
  const form = newClientForm(values);
  document.getElementById('inbox-wrap').after(form);
  form.elements.id.focus();
}

function row(candidate, status) {
  const select = clientSelect(status, { class: 'map-to', 'aria-label': `Map ${candidate.value} to` }, true);
  // The value carries this client's own name, so the owner is known: offer it already chosen.
  // Otherwise, the client whose names it was seen with, when one clearly stands out.
  const owner = candidate.client || likelyClient(candidate);
  if (owner && [...select.options].some((o) => o.value === owner)) select.value = owner;
  const actions = h('div', { class: 'actions-wrap' });
  const tr = h('tr', { dataset: {
    value: candidate.value, search: searchText(candidate, status),
    sortValue: candidate.value, sortKind: kindLabel(candidate.kind), sortCount: String(candidate.count || 0),
    sortSeenwith: seenWith(candidate).map((cid) => labelOf(status, cid)).join(' ') || '\uffff',
  } },
    pickCell(`Select ${candidate.value}`),
    h('td', {}, h('span', { class: 'mono' }, candidate.value),
      candidate.placeholder
        ? h('div', { class: 'hint' }, 'held back as ', h('code', {}, candidate.placeholder))
        // "sent as-is" covers two different things, and only one of them is a decision. A
        // low-confidence find is sent deliberately; this one was meant to be held back and no
        // placeholder could be minted, which is a failure and should not read the same (D12).
        : candidate.unprotected
          ? h('div', { class: 'hint bad-text' }, 'sent WITHOUT being replaced: no placeholder was left')
          : h('div', { class: 'hint' }, 'sent as-is'),
      whyClient(candidate)),
    h('td', { class: 'kind' }, h('span', { class: `chip ${candidate.confidence === 'high' ? 'warn' : ''}` },
      `${kindLabel(candidate.kind)} - ${candidate.confidence}`)),
    h('td', {}, `${candidate.count}x`, h('div', { class: 'hint' }, (candidate.sources || []).join(', '))),
    contextCell(candidate, status),
    h('td', { class: 'map' }, select),
    h('td', { class: 'actions' }, actions),
    binCell(candidate));
  const normalActions = () => actions.replaceChildren(
    h('button', { type: 'button', class: 'primary map' }, 'Map'),
    h('button', { type: 'button', class: 'ignore' }, 'Ignore'));
  normalActions();
  select.addEventListener('change', () => {
    if (select.value === '__new') showNewClient([candidate.value]);
    else view.lastClient = select.value;
  });
  actions.addEventListener('click', async (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    try {
      if (button.classList.contains('map')) {
        if (select.value === '__new') { showNewClient([candidate.value]); return; }
        try {
          await mapValues([candidate.value], select.value);
        } catch (mapError) {
          if (mapError.suggest !== 'public') throw mapError;
          // Another client already has it: usually a built-in id or a vendor host, the same everywhere.
          say(`${mapError.message} It may be the same in every tenant.`, 'warn');
          actions.replaceChildren(
            h('button', { type: 'button', class: 'primary public' }, 'Treat as public'),
            h('button', { type: 'button', class: 'icon cancel', 'aria-label': 'Cancel' }, 'x'));
          return;
        }
        say(`Mapped ${candidate.value} to ${select.value === 'global' ? 'Global' : select.value}.`, 'ok',
          { action: undoAction([candidate.value], { unmap: select.value }) });
        await reload();
      } else if (button.classList.contains('public')) {
        const result = await postJSON('/api/candidates/public', { value: candidate.value });
        const from = (result.removed_from || []).join(', ');
        say(`${candidate.value} is treated as public${from ? ` and removed from ${from}` : ''}.`, 'ok');
        await reload();
      } else if (button.classList.contains('ignore')) {
        const client = select.value === '__new' ? null : select.value;
        actions.replaceChildren(...[
          client && client !== 'global' ? h('button', { type: 'button', class: 'ignore-client' }, `For ${client}`) : null,
          h('button', { type: 'button', class: 'ignore-everywhere' }, 'Everywhere'),
          h('button', { type: 'button', class: 'icon cancel', 'aria-label': 'Cancel' }, 'x')].filter(Boolean));
      } else if (button.classList.contains('ignore-client')) {
        await ignoreValue(candidate.value, select.value);
        say(`${candidate.value} is ignored for ${select.value}.`, 'ok',
          { action: undoAction([candidate.value], { unignore: select.value }) });
        await reload();
      } else if (button.classList.contains('ignore-everywhere')) {
        await ignoreValue(candidate.value, 'global');
        say(`${candidate.value} is ignored everywhere.`, 'ok',
          { action: undoAction([candidate.value], { unignore: 'global' }) });
        await reload();
      } else if (button.classList.contains('cancel')) {
        normalActions();
      }
    } catch (error) {
      sayError(error);
    }
  });
  return tr;
}

// Rows the filter is showing. Selecting and mapping only ever act on these: a row hidden by the
// filter is never mapped because it happened to be ticked before the filter changed.
function shownRows(table) {
  return [...table.querySelectorAll('tbody tr[data-value]')].filter((tr) => !tr.classList.contains('hidden-by-filter'));
}

function applyFilter(table, text) {
  const words = text.trim().toLowerCase().split(/\s+/).filter(Boolean);
  for (const tr of table.querySelectorAll('tbody tr[data-value]')) {
    const hit = words.every((w) => tr.dataset.search.includes(w));
    tr.classList.toggle('hidden-by-filter', !hit);
    if (!hit) tr.querySelector('input.pick').checked = false;
  }
  const all = table.querySelector('input.pick-all');
  if (all) all.checked = false;
}

// The bar, built like the client pages' (D45): a count of what is ticked, then the actions under
// headings that name the outcome, so "who they belong to" and "stop watching for them" are never
// side by side. Every action is off until something is ticked, reports that it is working while
// it runs (busyButton), and offers its own undo when it is done.
function bulkBar(status, table) {
  const count = h('span', { class: 'muted bulk-count', id: 'bulk-count' }, '0 selected items');
  const select = clientSelect(status, { id: 'bulk-client', 'aria-label': 'Which client they belong to' }, true);
  const filter = h('input', { type: 'search', id: 'review-filter', value: view.filter,
    placeholder: 'Filter, e.g. acme', 'aria-label': 'Filter identifiers' });
  const map = h('button', { type: 'button', id: 'bulk-map', class: 'primary' }, 'Make them theirs');
  const ignoreHere = h('button', { type: 'button', id: 'bulk-ignore-client' }, 'In ...');
  const ignoreAll = h('button', { type: 'button', id: 'bulk-ignore' }, 'Everywhere');
  const dismiss = h('button', { type: 'button', id: 'bulk-dismiss', class: 'dismiss', title: DISMISS_LABEL },
    icon('trash'), 'From Review');
  const bar = h('div', { id: 'bulk', class: 'card form-row' },
    filter,
    h('div', { class: 'bulk-entries' },
      h('span', { class: 'bulk-count-wrap' }, count),
      bulkGroup('Who they should belong to', select, map),
      bulkGroup('Stop watching for them', ignoreHere, ignoreAll),
      bulkGroup('Remove', dismiss)));

  const picked = () => shownRows(table).filter((tr) => tr.querySelector('input.pick').checked).map((tr) => tr.dataset.value);
  // "In ..." names the client it would act on. "This client" means nothing on Review - there is
  // no client whose page this is, only whatever the select happens to be showing.
  const chosenClient = () => {
    const chosen = select.value;
    return chosen && chosen !== '__new' && chosen !== 'global' ? chosen : null;
  };
  const recount = () => {
    const n = picked().length;
    const shown = shownRows(table).length;
    const total = table.querySelectorAll('tbody tr[data-value]').length;
    count.textContent = `${n} selected item${n === 1 ? '' : 's'}` + (shown < total ? ` of ${shown} shown (${total} in all)` : '');
    const named = chosenClient();
    ignoreHere.textContent = named ? `In ${labelOf(status, named)}` : 'In ...';
    ignoreHere.title = named ? 'Never propose these again for that client; other clients still see them.'
      : 'Choose a client on the left. For Global, use "Everywhere".';
    for (const button of [map, ignoreAll, dismiss]) button.disabled = !n;
    ignoreHere.disabled = !n || !named;
  };
  filter.addEventListener('input', () => {
    view.filter = filter.value;
    applyFilter(table, filter.value);
    recount();
  });
  select.addEventListener('change', recount);
  table.addEventListener('change', (event) => {
    if (event.target.classList.contains('pick-all')) {
      // Select all means all that are shown: with a filter of "acme", the acme rows and no others.
      for (const tr of shownRows(table)) tr.querySelector('input.pick').checked = event.target.checked;
    }
    recount();
  });
  applyFilter(table, view.filter);
  recount();

  busyButton(map, async () => {
    const values = picked();
    if (!values.length) return;
    if (select.value === '__new') { showNewClient(values); return; }
    const client = select.value;
    view.lastClient = client;
    const where = client === 'global' ? 'Global' : labelOf(status, client);
    await bulkRun(values, (value) => postJSON('/api/candidates/resolve', { value, action: 'map', client }),
      (n) => `Mapped ${items(n)} to ${where}.`, { unmap: client });
  }, { busyLabel: 'Mapping...' });
  // Ignoring for one client was already possible a row at a time, and mapping a batch to a client
  // was already possible - but ignoring a batch for one was not, so a list of a client's own noise
  // had to be cleared one row at a time. Scope comes from the same select the mapping uses.
  busyButton(ignoreHere, async () => {
    const values = picked();
    const client = chosenClient();
    if (!values.length || !client) return;
    await bulkRun(values, (value) => ignoreValue(value, client),
      (n) => `Ignored ${items(n)} for ${labelOf(status, client)}.`, { unignore: client });
  }, { busyLabel: 'Ignoring...' });
  busyButton(ignoreAll, async () => {
    const values = picked();
    if (!values.length) return;
    await bulkRun(values, (value) => ignoreValue(value, 'global'),
      (n) => `Ignored ${items(n)} everywhere.`, { unignore: 'global' });
  }, { busyLabel: 'Ignoring...' });
  // No busy label: the icon is part of the label, and swapping the text would drop it.
  busyButton(dismiss, async () => {
    const values = picked();
    if (!values.length) return;
    await bulkRun(values, dismissOne, (n) => `Removed ${items(n)} from Review.`, {});
  });
  return bar;
}

// One real value and the token that replaces it, for something you already know about rather
// than something the queue found.
function addMappingForm() {
  const who = clientSelect(view.ctx.status, { class: 'map-to', 'aria-label': 'Client' }, false);
  const real = h('input', { type: 'text', name: 'real', placeholder: 'real identifier',
    'aria-label': 'Real identifier' });
  const token = h('input', { type: 'text', name: 'token', placeholder: 'token (blank = automatic)',
    'aria-label': 'Token' });
  const form = h('form', { id: 'add-mapping', class: 'form-row' },
    who, real, token, h('button', { type: 'submit', class: 'primary' }, 'Add mapping'));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = real.value.trim();
    if (!value) { real.focus(); return; }
    try {
      await postJSON('/api/mapping/form', { client: who.value, real: value, token: token.value.trim() || null });
      say(`Mapped ${value} for ${who.value}.`, 'ok');
      real.value = '';
      token.value = '';
      await reload();
      focusFirst('#add-mapping');
    } catch (error) {
      sayError(error);
    }
  });
  return form;
}

// A kind ignored far more often than kept (feature 31's last part, DECISIONS 25: 20 or more
// ignores, kept 1 time in 10 or less). Offered, never applied. Accepting takes it out of Review
// and tokenises it automatically - never sends it as written (DECISIONS 43). Undone from the message,
// or on Settings.
function kindOffers(offers) {
  if (!offers.length) return null;
  const setQuiet = async (kinds) => postJSON('/api/config', { settings: { 'detect.quiet_kinds': kinds } });
  const card = h('div', { class: 'card callout', id: 'kind-offers' }, ...offers.map((offer) => {
    const label = kindLabel(offer.kind);
    const stop = h('button', { type: 'button', class: 'kind-quiet', dataset: { kind: offer.kind } },
      `Tokenise ${label} automatically`);
    busyButton(stop, async () => {
      try {
        const before = ((await getJSON('/api/config')).settings || {})['detect.quiet_kinds'] || [];
        await setQuiet([...new Set([...before, offer.kind])]);
        say(`${label} finds are now tokenised automatically and no longer proposed.`, 'ok', {
          action: { label: 'Undo', run: async () => { await setQuiet(before); say(`${label} is proposed again.`, 'ok'); await reload(); } },
        });
        await reload();
      } catch (error) {
        sayError(error);
      }
    });
    // The easier answer, beside the tempting one (DECISIONS 42): hide the offer until another 50
    // ignores, without changing what is proposed or held back.
    const later = h('button', { type: 'button', class: 'kind-snooze', dataset: { kind: offer.kind } }, 'Not now');
    busyButton(later, async () => {
      const snooze = async (undo) => postJSON('/api/candidates/snooze', { kind: offer.kind, undo });
      try {
        const answer = await snooze(false);
        say(`Not now: the ${label} offer comes back after ${answer.back_after - offer.ignored} more ignores.`, 'ok', {
          action: { label: 'Undo', run: async () => { await snooze(true); say(`The ${label} offer is back.`, 'ok'); await reload(); } },
        });
        await reload();
      } catch (error) {
        sayError(error);
      }
    });
    return h('p', { class: 'kind-offer', dataset: { kind: offer.kind } },
      h('b', {}, `${label}: ignored ${offer.ignored} times, kept ${offer.kept}. `), stop, ' ', later,
      h('span', { class: 'hint' }, ' Tokenising automatically still replaces every find, without asking you about it; Settings can turn it back. Not now hides this until 50 more are ignored.'));
  }));
  return card;
}

// Above the queue rather than below it: these are what the page is for when the queue is empty,
// and out of reach under a few hundred rows when it is not.
function makeThings() {
  const addClient = h('button', { type: 'button', id: 'start-new-client', class: 'primary' },
    '+ New client');
  addClient.addEventListener('click', async () => {
    const { askForNewClient } = await import('./newclient.js');
    askForNewClient(async () => { await reload(); });
  });
  return h('div', { class: 'card', id: 'review-make' },
    h('div', { class: 'form-row' }, addClient, addMappingForm()),
    h('p', { class: 'hint' },
      'A client id names the file and is what the command line wants; the display name is what '
      + 'you read here. A mapping is one real value and the token that replaces it everywhere.'));
}

// Entries a colleague added to a client you share (DECISIONS 67). A shared file is only as trusted as
// its least careful editor: an ordinary word mapped as the client rewrites everyone's traffic. Keep is
// this PC's answer and stays here; Remove takes it out of the shared file for everyone.
async function othersCard() {
  let answer;
  try { answer = await getJSON('/api/additions'); } catch { return null; }
  const additions = answer.additions || [];
  if (!additions.length) return null;
  const act = (a, action, tr) => async () => {
    try {
      await postJSON('/api/additions', { client: a.client, real: a.real, action });
      tr.remove();
      say(action === 'keep' ? `Kept ${a.real}.` : `Removed ${a.real} from ${labelOf(view.ctx.status, a.client)}.`, 'ok');
    } catch (error) { sayError(error); }
  };
  const rows = additions.map((a) => {
    const tr = h('tr', { dataset: { client: a.client, real: a.real } });
    const keep = h('button', { type: 'button', class: 'keep-addition' }, 'Keep');
    const drop = h('button', { type: 'button', class: 'drop-addition danger' }, 'Remove');
    keep.addEventListener('click', act(a, 'keep', tr));
    drop.addEventListener('click', act(a, 'drop', tr));
    tr.append(h('td', {}, labelOf(view.ctx.status, a.client)), h('td', { class: 'mono' }, a.real),
      h('td', { class: 'mono' }, a.token), h('td', {}, a.by), h('td', { class: 'actions' }, keep, ' ', drop));
    return tr;
  });
  return h('div', { class: 'card', id: 'others-additions' },
    h('h2', {}, 'Added by someone else'),
    h('p', { class: 'hint' }, 'Entries a colleague added to a client you share. Keep the right ones; '
      + 'Remove takes a wrong one out of the shared file for everyone.'),
    h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Client'), h('th', {}, 'Value'),
      h('th', {}, 'Token'), h('th', {}, 'Added by'), h('th', { class: 'actions' }, ''))), h('tbody', {}, rows)));
}

async function paint() {
  const status = view.ctx.status;
  const others = await othersCard();
  const answer = await getJSON('/api/candidates');
  const pending = answer.pending || [];
  view.pending = new Map(pending.map((c) => [c.value, c]));
  const head = h('div', { class: 'page-head' }, h('h1', {}, 'Review'),
    h('span', { class: 'muted' }, 'Identifiers seen on the wire that are not in the mapping. Map them to a client, or ignore them if they are not client-specific.'));
  const offers = kindOffers(answer.kind_offers || []);
  if (!pending.length) {
    view.main.replaceChildren(...[head, offers, others, makeThings(),
      h('div', { class: 'card' }, h('p', { id: 'inbox-empty', class: 'empty' }, 'Nothing to review.'))].filter(Boolean));
    return;
  }
  const find = () => document.getElementById('inbox');
  const table = h('table', { id: 'inbox', dataset: { resizable: 'inbox' } },
    h('thead', {}, h('tr', {},
      h('th', { class: 'pick', dataset: { col: 'pick' } },
        pickBox('Select all', { class: 'pick-all' })),
      sortHeader('Identifier', 'value', find), sortHeader('Kind', 'kind', find, 'kind'), sortHeader('Seen', 'count', find),
      sortHeader('Seen with', 'seenwith', find), h('th', { class: 'map', dataset: { col: 'map' } }, 'Map to'),
      h('th', { class: 'actions', dataset: { col: 'actions' } }, 'Actions'),
      h('th', { class: 'bin', 'aria-label': 'Remove' }))),
    h('tbody', {}, pending.map((c) => row(c, status))));
  sortRows(table);
  view.main.replaceChildren(...[head, offers, others, makeThings(), bulkBar(status, table),
                                h('div', { class: 'card', id: 'inbox-wrap' }, table)].filter(Boolean));
  const box = document.getElementById('review-filter');
  if (box && view.filter) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
}

async function reload() {
  view.ctx.status = await view.ctx.refresh();
  await paint();
}

export async function render(main, ctx) {
  view.main = main;
  view.ctx = ctx;
  main.append(h('div', { class: 'page-head' }, h('h1', {}, 'Review')));
  await paint();
}
