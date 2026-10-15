// Log: the substitution log, read here and never downloaded (DECISIONS 38). What was replaced and
// by what, whose it is, what was done, and who was driving - plus every change made through the UI.
// A value appears the first time a session sends it and again when what happens to it changes.

import { getJSON, sayError } from '../api.js';
import { h } from '../dom.js';

const ACTIONS = [
  ['', 'Every action'], ['replaced', 'Replaced'], ['restored', 'Restored'], ['auto-added', 'Auto-added'],
  ['held-back', 'Held back'], ['stand-in', 'Stand-in (GUID)'], ['sent-unprotected', 'Sent unprotected'],
  ['canary-refused', 'Refused by the canary'], ['canary-fail-open', 'Sent anyway (fail-open)'],
  ['rule-declined', 'Let through by a match rule'],
  ['ui', 'Changes made in the UI'],
];

const view = { main: null, filters: {}, next: null, events: [] };

function driver(who) {
  if (!who) return ['', ''];
  const name = who.email || who.user || '';
  const detail = [];
  if (who.source === 'session-page') detail.push('Session page');
  if (who.app) detail.push(who.app);                  // the Claude Desktop app's Code tab (feature 117)
  if (who.via) detail.push(who.via === 'remote' ? 'through the tunnel' : 'on this machine');
  if (who.upn) detail.push(`UPN ${who.upn}`);
  if (who.ssh_from && who.ssh_from.length) detail.push(`SSH open from ${who.ssh_from.join(', ')}`);
  if (who.ip) detail.push(`IP ${who.ip}`);
  if (who.country) detail.push(who.country);
  if (who.agent) detail.push(who.agent);
  const shown = who.ssh_from && who.ssh_from.length ? `${name} (SSH)` : name;
  return [shown, detail.join(' - ')];
}

function time(ts) {
  const at = new Date(ts);
  return Number.isNaN(at.getTime()) ? ts : at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function row(e) {
  const [who, whoDetail] = driver(e.who);
  if (e.type === 'ui') {
    return h('tr', { class: 'log-ui' },
      h('td', { class: 'nowrap' }, time(e.ts)),
      h('td', {}, h('span', { class: 'chip' }, 'UI')),
      h('td', { colspan: '2', class: 'mono' }, `${e.method} ${e.path}`, e.status >= 400 ? ` (${e.status})` : ''),
      h('td', {}, ''), h('td', {}, ''),
      h('td', { title: whoDetail }, who));
  }
  const arrow = e.direction === 'back' ? ' <- ' : ' -> ';
  const route = e.route ? ` (${e.route})` : '';
  // A canary match carries its surroundings (DECISIONS 40), shown under the value it matched.
  const context = e.before !== undefined || e.after !== undefined
    ? h('div', { class: 'log-context muted' }, `...${e.before || ''}`, h('mark', {}, e.real || ''), `${e.after || ''}...`,
      e.where ? h('div', { class: 'log-where' }, e.where) : null)
    : null;
  return h('tr', { 'data-action': e.action || '' },
    h('td', { class: 'nowrap' }, time(e.ts)),
    h('td', {}, h('span', { class: `chip action-${e.action}` }, `${e.action}${route}`)),
    h('td', { class: 'mono' }, e.real || '', context),
    h('td', { class: 'mono' }, e.token ? `${arrow}${e.token}` : ''),
    h('td', {}, e.client || ''),
    h('td', {}, e.session || ''),
    h('td', { title: whoDetail }, who));
}

function table() {
  const body = h('tbody', {}, view.events.map(row));
  const empty = view.events.length ? null : h('p', { class: 'muted', id: 'log-empty' }, 'Nothing logged for this day and filter.');
  return h('div', { class: 'log-wrap' },
    h('table', { class: 'log-table', id: 'log-table' },
      h('thead', {}, h('tr', {}, ['Time', 'Action', 'Real', 'Token', 'Client', 'Session', 'Who'].map((t) => h('th', {}, t)))),
      body),
    empty);
}

function query(before) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(view.filters)) if (value) params.set(key, value);
  if (before) params.set('before', String(before));
  return `/api/log?${params}`;
}

async function load(more = false) {
  try {
    const body = await getJSON(query(more ? view.next : 0));
    view.events = more ? view.events.concat(body.events) : body.events;
    view.next = body.next;
    view.days = body.days;
    view.day = body.day;
    view.total = body.total;
    view.old = body.skipped_old;
    paint();
  } catch (error) {
    sayError(error);
  }
}

function filters() {
  const day = h('select', { id: 'log-day', 'aria-label': 'Day' },
    (view.days && view.days.length ? view.days : [view.day]).filter(Boolean).map((d) =>
      h('option', { value: d, selected: d === view.day }, `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}`)));
  const action = h('select', { id: 'log-action', 'aria-label': 'Action' },
    ACTIONS.map(([value, label]) => h('option', { value, selected: value === (view.filters.action || '') }, label)));
  const client = h('input', { type: 'text', id: 'log-client', placeholder: 'Client id', value: view.filters.client || '', 'aria-label': 'Client' });
  const session = h('input', { type: 'text', id: 'log-session', placeholder: 'Session', value: view.filters.session || '', 'aria-label': 'Session' });
  const text = h('input', { type: 'search', id: 'log-q', placeholder: 'Search', value: view.filters.q || '', 'aria-label': 'Search' });
  const apply = () => {
    view.filters = { day: day.value, action: action.value, client: client.value.trim(),
      session: session.value.trim(), q: text.value.trim() };
    load();
  };
  day.addEventListener('change', apply);
  action.addEventListener('change', apply);
  for (const box of [client, session, text]) {
    box.addEventListener('keydown', (event) => { if (event.key === 'Enter') apply(); });
    box.addEventListener('change', apply);
  }
  return h('div', { class: 'form-row log-filters' }, day, action, client, session, text);
}

function paint() {
  const summary = h('p', { class: 'hint', id: 'log-summary' },
    `${view.total || 0} ${view.total === 1 ? 'entry' : 'entries'}, newest first.`,
    view.old ? ` ${view.old} older ${view.old === 1 ? 'line' : 'lines'} in the previous format are not shown here.` : '');
  const more = view.next ? h('button', { type: 'button', id: 'log-more' }, 'Show more') : null;
  if (more) more.addEventListener('click', () => load(true));
  view.main.replaceChildren(
    h('div', { class: 'page-head' }, h('h1', {}, 'Log')),
    h('section', { class: 'card' },
      h('p', { class: 'hint' }, 'Real names, kept on this machine. Read here; there is no download.'),
      filters(), summary, table(), more));
}

export async function render(main) {
  view.main = main;
  view.events = [];
  main.append(h('div', { class: 'page-head' }, h('h1', {}, 'Log')));
  await load();
}
