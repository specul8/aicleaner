// Alerts (feature 161): what a script or a session on the AI Cleaner machine raised for you - a title, a summary and
// the session to discuss it in. Each stays, on every device, until it is dismissed here (on any device). The summary
// is drawn as text, as written, never as HTML.

import { getJSON, postJSON, sayError } from '../api.js';
import { h } from '../dom.js';

const view = { main: null };

function when(at) {
  const d = new Date(at * 1000);
  const two = (n) => String(n).padStart(2, '0');
  return `${two(d.getDate())}/${two(d.getMonth() + 1)} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

function alertCard(alert) {
  const dismiss = h('button', { type: 'button', class: 'alert-dismiss' }, 'Dismiss');
  dismiss.addEventListener('click', async () => {
    dismiss.disabled = true;
    try {
      await postJSON('/api/alerts/dismiss', { id: alert.id });
      window.dispatchEvent(new CustomEvent('aicleaner:alerts-changed'));
      await load();
    } catch (error) {
      dismiss.disabled = false;
      sayError(error);
    }
  });
  const open = alert.session
    ? h('a', { class: 'button primary alert-open', href: `#/session/${encodeURIComponent(alert.session)}/chat` },
      `Open ${alert.session}`)
    : null;
  return h('section', { class: 'card alert-card', dataset: { level: alert.level, id: alert.id } },
    h('h3', {}, alert.title),
    h('div', { class: 'hint' }, `${when(alert.at)}${alert.session ? ` - ${alert.session}` : ''}`),
    alert.summary ? h('pre', { class: 'alert-summary' }, alert.summary) : null,
    h('div', { class: 'form-row' }, open, dismiss));
}

async function load() {
  let answer;
  try {
    answer = await getJSON('/api/alerts');
  } catch (error) {
    sayError(error);
    return;
  }
  const alerts = [...(answer.alerts || [])].reverse();          // newest first
  const body = alerts.length ? alerts.map(alertCard)
    : [h('p', { class: 'muted' }, 'No alerts. A script or a session on the AI Cleaner machine can raise one; it stays here until it is dismissed.')];
  view.main.replaceChildren(h('div', { class: 'page-head' }, h('h1', {}, 'Alerts')), ...body);
}

export async function render(main) {
  view.main = main;
  main.append(h('div', { class: 'page-head' }, h('h1', {}, 'Alerts')));
  document.title = 'Alerts - AI Cleaner';
  await load();
}

// A poll that brings a new alert, or loses one dismissed elsewhere, redraws the list.
let shownIds = '';
export function onStatus(status) {
  const ids = (((status || {}).activity || {}).alerts || []).map((a) => a.id).join(',');
  if (ids === shownIds) return;
  shownIds = ids;
  if (view.main && view.main.isConnected) load();
}
