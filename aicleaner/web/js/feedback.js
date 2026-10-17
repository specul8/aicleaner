// Feedback from inside aicleaner (2026-10-16; spec 2026-10-16-feedback-design.md).
//
// A dialog with three tabs - New, Open, Closed. The first time, it asks before anything connects:
// the feedback system is the one place aicleaner reaches that the person did not set up. The proxy
// talks to the relay; this page only talks to the proxy.

import { getJSON, postJSON, say, sayError } from './api.js';
import { h } from './dom.js';
import { arrangeSheet } from './sheet.js';

const TEMPLATES = {
  bug: '## What happened\n\n\n## How to make it happen again\n1. \n',
  idea: '## What would help\n\n\n## Where it would be used\n\n',
};

// The field that had focus before Feedback was opened: its name, never its value.
let lastFocus = '';
function noteFocus(el) {
  if (!el || el === document.body || el === document.documentElement || !el.closest || el.closest('#feedback-dialog')) return;
  // What opens Feedback is not what the person was doing before it.
  if (el.id === 'nav-feedback' || el.classList.contains('feedback-open')) return;
  const label = el.id || el.getAttribute('name') || el.getAttribute('aria-label') || '';
  lastFocus = label ? `${el.tagName.toLowerCase()}#${label}`.slice(0, 80) : el.tagName.toLowerCase();
}
document.addEventListener('focusin', (event) => noteFocus(event.target));

// What the page can say about itself: the route, what is open, the field in focus - no values.
export function pageContext() {
  const visible = (el) => el && !el.hidden && el.offsetParent !== null;
  const panels = [...document.querySelectorAll('[id$="-pane"], [id$="-panel"], .pane, aside[id]')]
    .filter(visible).map((el) => el.id || el.className.split(' ')[0]).filter(Boolean);
  const cards = [...document.querySelectorAll('section.card[data-card]')].filter(visible).map((el) => el.dataset.card);
  const opened = [...document.querySelectorAll('details[open] > summary')].filter(visible)
    .map((s) => (s.closest('details').className || 'details').split(' ')[0]);
  return {
    route: window.location.href, focus: lastFocus,
    panels: [...new Set([...panels, ...opened])].slice(0, 30), cards: cards.slice(0, 40),
    browser: navigator.userAgent.slice(0, 200), screen: `${window.innerWidth}x${window.innerHeight}`,
    theme: document.documentElement.dataset.theme || '', layout: document.documentElement.dataset.layout || '',
  };
}

function when(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}

// Markdown around the selection (or at the caret), as typed text - what is sent stays plain text.
function wrap(box, before, after = before, placeholder = 'text') {
  const { selectionStart: s, selectionEnd: e, value } = box;
  const chosen = value.slice(s, e) || placeholder;
  box.setRangeText(`${before}${chosen}${after}`, s, e, 'select');
  box.setSelectionRange(s + before.length, s + before.length + chosen.length);
  box.focus();
}

function prefixLines(box, prefix) {
  const { selectionStart: s, selectionEnd: e, value } = box;
  const start = value.lastIndexOf('\n', s - 1) + 1;
  const lines = value.slice(start, e).split('\n');
  const done = lines.map((line, i) => `${typeof prefix === 'function' ? prefix(i) : prefix}${line}`).join('\n');
  box.setRangeText(done, start, e, 'end');
  box.focus();
}

function toolbar(box) {
  const tools = [
    ['B', 'Bold', () => wrap(box, '**')], ['I', 'Italic', () => wrap(box, '_')], ['</>', 'Code', () => wrap(box, '`')],
    ['•', 'Bullet list', () => prefixLines(box, '- ')], ['1.', 'Numbered list', () => prefixLines(box, (i) => `${i + 1}. `)],
    ['Link', 'Link', () => wrap(box, '[', '](https://)', 'words')],
  ];
  return h('div', { class: 'fb-toolbar', role: 'toolbar', 'aria-label': 'Formatting' }, ...tools.map(([text, title, act]) => {
    const button = h('button', { type: 'button', class: 'fb-tool', title, 'aria-label': title }, text);
    button.addEventListener('click', act);
    return button;
  }));
}

function consentView(state, onConnected, onCancel) {
  const connect = h('button', { type: 'button', class: 'primary fb-connect' }, 'Connect');
  const later = h('button', { type: 'button', class: 'fb-later' }, 'Not now');
  connect.addEventListener('click', async () => {
    connect.disabled = true;
    try {
      onConnected(await postJSON('/api/feedback/connect', {}));
    } catch (error) {
      sayError(error);
      connect.disabled = false;
    }
  });
  later.addEventListener('click', onCancel);
  return h('div', { class: 'fb-consent' },
    h('h2', {}, "Connect to AI Cleaner's feedback system?"),
    h('p', {}, 'This lets you raise tickets anonymously, read and update the tickets you have raised, and see any replies to them.'),
    h('p', {}, `aicleaner will connect to ${state.relay} - the only place it connects to that you did not set up yourself. `
      + 'It connects from the machine aicleaner runs on, so feedback works here and through remote access alike.'),
    h('p', {}, 'Each ticket carries technical details about aicleaner - what is switched on, the page you were on, the version - '
      + 'and never your folders, client names, files, or anything that identifies you. You can see exactly what is sent before sending.'),
    h('div', { class: 'form-row' }, connect, later));
}

function newTab(dialog, refreshCounts) {
  // Idea first and by default (the user, 2026-10-16).
  const kind = h('select', { class: 'fb-kind', 'aria-label': 'Kind' },
    h('option', { value: 'idea' }, 'Idea - something that would help'), h('option', { value: 'bug' }, 'Bug - something is not working'));
  const title = h('input', { type: 'text', class: 'fb-title', maxlength: '250', placeholder: 'A short title',
    'aria-label': 'Title' });
  const count = h('span', { class: 'hint fb-count' }, '0 / 250');
  const box = h('textarea', { class: 'fb-body', rows: '10', 'aria-label': 'Description and how to make it happen again' });
  box.value = TEMPLATES.idea;
  const includeLog = h('label', { class: 'check' }, h('input', { type: 'checkbox', class: 'fb-log' }), ' Include the last 20 lines of the proxy log (filtered)');
  const extra = h('pre', { class: 'mono fb-extra' });
  const more = h('details', { class: 'fb-more' }, h('summary', {}, 'What else is sent'), extra);
  const send = h('button', { type: 'button', class: 'primary fb-send' }, 'Send');
  const page = pageContext();
  title.addEventListener('input', () => { count.textContent = `${title.value.length} / 250`; });
  kind.addEventListener('change', () => {
    if (Object.values(TEMPLATES).includes(box.value)) box.value = TEMPLATES[kind.value];
  });
  const preview = async () => {
    try {
      const answer = await postJSON('/api/feedback/preview', { page, include_log: includeLog.querySelector('input').checked });
      extra.textContent = JSON.stringify(answer.context, null, 2);
    } catch (error) { extra.textContent = `Could not show it: ${error.message || error}`; }
  };
  more.querySelector('summary').addEventListener('click', () => { if (!more.open) preview(); });
  includeLog.querySelector('input').addEventListener('change', () => { if (more.open) preview(); });
  send.addEventListener('click', async () => {
    if (!title.value.trim()) { say('Give it a title first.', 'warn'); title.focus(); return; }
    send.disabled = true;
    try {
      const answer = await postJSON('/api/feedback/tickets', { title: title.value, body: box.value, kind: kind.value,
        page, include_log: includeLog.querySelector('input').checked });
      say(`Sent as ticket #${answer.number}. Replies will show under Open.`, 'ok');
      title.value = '';
      box.value = TEMPLATES[kind.value];
      count.textContent = '0 / 250';
      refreshCounts();
      dialog.showTab('open');
    } catch (error) {
      sayError(error);
    }
    send.disabled = false;
  });
  const view = h('div', { class: 'fb-new' },
    h('div', { class: 'form-row' }, kind),
    h('label', { class: 'field' }, 'Title', title, count),
    h('label', { class: 'field' }, 'Description, and how to make it happen again', toolbar(box), box),
    h('p', { class: 'hint' }, 'Client names in what you write are replaced by their tokens before it is sent.'),
    h('div', { class: 'form-row' }, includeLog), more);
  view.sendButton = send;            // placed far right in the dialog's footer, opposite Cancel
  return view;
}

function ticketCard(ticket, open, reload) {
  const replies = (ticket.replies || []).map((r) => h('div', { class: `fb-reply fb-from-${r.from === 'you' ? 'you' : 'team'}` },
    h('div', { class: 'hint' }, `${r.from} - ${when(r.at)}`), h('div', { class: 'fb-text' }, r.body)));
  const parts = [
    h('div', { class: 'fb-ticket-head' }, h('strong', {}, ticket.title), h('span', { class: 'hint' }, ` #${ticket.number} - ${ticket.kind || 'ticket'} - ${when(ticket.created)}`)),
    h('details', { class: 'fb-ticket-body' }, h('summary', {}, 'What you sent'), h('div', { class: 'fb-text' }, ticket.body || '')),
    ...replies,
  ];
  if (open) {
    const update = h('textarea', { class: 'fb-update', rows: '3', 'aria-label': 'Add an update' });
    const add = h('button', { type: 'button', class: 'fb-add' }, 'Add update');
    const close = h('button', { type: 'button', class: 'fb-close-ticket' }, 'Close ticket');
    add.addEventListener('click', async () => {
      if (!update.value.trim()) return;
      add.disabled = true;
      try { await postJSON(`/api/feedback/tickets/${ticket.number}/comments`, { body: update.value }); say('Update added.', 'ok'); reload(); }
      catch (error) { sayError(error); add.disabled = false; }
    });
    close.addEventListener('click', async () => {
      close.disabled = true;
      try { await postJSON(`/api/feedback/tickets/${ticket.number}/close`, {}); say(`Ticket #${ticket.number} closed.`, 'ok'); reload(); }
      catch (error) { sayError(error); close.disabled = false; }
    });
    parts.push(update, h('div', { class: 'form-row' }, add, close));
  }
  return h('li', { class: 'fb-ticket' }, ...parts);
}

function listTab(state) {
  const list = h('ul', { class: 'fb-tickets' });
  const status = h('p', { class: 'hint' }, 'Loading...');
  const load = async () => {
    status.textContent = 'Loading...';
    try {
      const answer = await getJSON(`/api/feedback/tickets?state=${state}`);
      const tickets = answer.tickets || [];
      list.replaceChildren(...tickets.map((t) => ticketCard(t, state === 'open', load)));
      status.textContent = tickets.length ? '' : (state === 'open' ? 'No open tickets.' : 'No closed tickets.');
    } catch (error) {
      status.textContent = `Could not load them: ${error.message || error}`;
    }
  };
  const view = h('div', { class: `fb-list fb-${state}` }, status, list);
  view.load = load;
  return view;
}

export async function openFeedback() {
  if (document.getElementById('feedback-dialog')) return;
  noteFocus(document.activeElement);       // a field redrawn as it was focused never said so
  let state;
  try { state = await getJSON('/api/feedback/state'); } catch (error) { sayError(error); return; }
  const body = h('div', { class: 'fb-body-area' });
  // One row at the foot (the user, 2026-10-16): Cancel far left, Send far right (on New only).
  const cancelButton = h('button', { type: 'button', class: 'fb-dismiss' }, 'Cancel');
  const sendSlot = h('span', { class: 'fb-send-slot' });
  // Always shown (2026-10-17): every dialog has its Cancel in view, the consent step included.
  const footer = h('div', { class: 'fb-footer' }, cancelButton, sendSlot);
  const dialog = h('dialog', { id: 'feedback-dialog', class: 'sheet feedback-dialog', 'aria-label': 'Feedback' }, body, footer);
  const finish = () => { dialog.close(); dialog.remove(); };
  cancelButton.addEventListener('click', finish);
  dialog.addEventListener('close', () => { if (dialog.isConnected) dialog.remove(); });
  arrangeSheet(dialog);

  const showTabs = () => {
    const tabs = { new: null, open: listTab('open'), closed: listTab('closed') };
    const buttons = {};
    const panel = h('div', { class: 'fb-panel' });
    footer.hidden = false;
    dialog.showTab = (name) => {
      for (const [key, button] of Object.entries(buttons)) button.setAttribute('aria-selected', String(key === name));
      if (name === 'new') {
        tabs.new = tabs.new || newTab(dialog, () => tabs.open.load());
        panel.replaceChildren(tabs.new);
        sendSlot.replaceChildren(tabs.new.sendButton);
      } else {
        panel.replaceChildren(tabs[name]);
        sendSlot.replaceChildren();
        tabs[name].load();
      }
    };
    const strip = h('div', { class: 'fb-tabs', role: 'tablist' }, ...[['new', 'New'], ['open', 'Open'], ['closed', 'Closed']].map(([key, text]) => {
      buttons[key] = h('button', { type: 'button', role: 'tab', class: `fb-tab fb-tab-${key}` }, text);
      buttons[key].addEventListener('click', () => dialog.showTab(key));
      return buttons[key];
    }));
    body.replaceChildren(h('h2', {}, 'Feedback'), strip, panel);
    dialog.showTab('new');
  };

  if (state.connected) showTabs();
  else body.replaceChildren(consentView(state, () => showTabs(), finish));
  document.body.append(dialog);
  dialog.showModal();
}
