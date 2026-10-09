// Secrets: what happens when a credential is found in a request (the only place the mode is
// changed), and saved secrets without a recognisable shape. Values are never shown back.

import { getJSON, postJSON, say, sayError } from '../api.js';
import { h, icon, iconButton, pickBox, pickCell } from '../dom.js';
import { track } from '../unsaved.js';

const MODES = [
  { value: 'redact', label: 'Redact', text: 'Credentials are replaced with [REDACTED-family] and the request continues. The model sees that a secret was there, never its value. The default.' },
  { value: 'block', label: 'Block', text: 'Any credential refuses the whole request (HTTP 403 naming the family), so you remove it and retry. Strictest; use it for sessions touching production.' },
  { value: 'off', label: 'Off', text: 'No credential scanning at all. Only for troubleshooting a false positive; client identifiers are still filtered.' },
];

const view = { main: null, ctx: null };

function modeCard(current) {
  const explain = h('p', { class: 'muted', id: 'mode-explain' });
  const show = (value) => { explain.textContent = (MODES.find((m) => m.value === value) || MODES[0]).text; };
  const group = h('div', { class: 'segmented', id: 'mode', role: 'radiogroup', 'aria-label': 'Secrets mode' },
    MODES.map((m) => h('label', {}, h('input', { type: 'radio', name: 'mode', value: m.value, checked: m.value === current }), m.label)));
  group.addEventListener('change', async (event) => {
    const value = event.target.value;
    show(value);
    try {
      await postJSON('/api/secrets/mode', { mode: value });
      say(`Secrets mode is now ${value.charAt(0).toUpperCase()}${value.slice(1)}.`, 'ok');
      await view.ctx.refresh();
    } catch (error) {
      sayError(error);
    }
  });
  show(current);
  return h('section', { class: 'card' }, h('h2', {}, 'When a credential is found in a request'),
    h('p', { class: 'hint' }, 'Credentials with a recognisable shape (Azure keys, SAS tokens, DevOps and GitHub PATs, client secrets, JWTs, connection strings) are found automatically.'),
    h('div', { class: 'form-row' }, group), explain);
}

function bulkSecrets(table) {
  const count = h('span', { class: 'muted bulk-count' }, '0 selected');
  const remove = h('button', { type: 'button', class: 'danger bulk-delete' }, 'Delete selected');
  const bar = h('div', { class: 'bulk-entries' }, count, remove);
  const picked = () => [...table.querySelectorAll('tbody tr')]
    .filter((tr) => tr.querySelector('input.pick').checked).map((tr) => tr.dataset.label);
  const recount = () => {
    const n = picked().length;
    count.textContent = `${n} selected`;
    remove.disabled = !n;
  };
  table.addEventListener('change', (event) => {
    if (event.target.classList.contains('pick-all')) {
      for (const box of table.querySelectorAll('tbody input.pick')) box.checked = event.target.checked;
    }
    recount();
  });
  recount();
  remove.addEventListener('click', () => {
    const labels = picked();
    if (!labels.length) return;
    // A secret cannot be read back once removed, so this one asks before it acts.
    const confirmBox = h('span', { class: 'confirm' }, `Delete ${labels.length} saved ${labels.length === 1 ? 'secret' : 'secrets'}?`,
      h('button', { type: 'button', class: 'danger go' }, 'Delete'),
      h('button', { type: 'button', class: 'cancel' }, 'Keep'));
    remove.replaceWith(confirmBox);
    confirmBox.querySelector('.cancel').addEventListener('click', () => confirmBox.replaceWith(remove));
    confirmBox.querySelector('.go').addEventListener('click', async () => {
      try {
        for (const label of labels) await postJSON('/api/secrets/delete', { label });
        say(`Deleted ${labels.length} saved ${labels.length === 1 ? 'secret' : 'secrets'}.`, 'ok');
        await paint();
      } catch (error) {
        sayError(error);
      }
    });
  });
  return bar;
}

function savedTable(entries) {
  if (!entries.length) {
    return h('div', {}, h('p', { class: 'empty' }, 'No saved secrets.'), h('table', { id: 'saved-secrets', hidden: true }, h('tbody')));
  }
  const body = h('tbody', {}, entries.map((entry) => {
    const actions = h('td', { class: 'actions' });
    const normal = () => actions.replaceChildren(iconButton('trash', `Delete ${entry.label}`, { class: 'icon danger delete' }));
    normal();
    actions.addEventListener('click', async (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      if (button.classList.contains('delete')) {
        actions.replaceChildren(h('span', { class: 'confirm' }, 'Delete?',
          h('button', { type: 'button', class: 'danger confirm-delete' }, 'Delete'),
          h('button', { type: 'button', class: 'cancel-delete' }, 'Keep')));
      } else if (button.classList.contains('cancel-delete')) {
        normal();
      } else if (button.classList.contains('confirm-delete')) {
        try {
          await postJSON('/api/secrets/delete', { label: entry.label });
          say(`Deleted ${entry.label}.`, 'ok');
          await paint();
        } catch (error) {
          sayError(error);
        }
      }
    });
    return h('tr', { dataset: { label: entry.label } }, pickCell(`Select ${entry.label}`),
      h('td', {}, entry.label), h('td', {}, String(entry.length)),
      h('td', {}, entry.added ? entry.added.slice(0, 10) : ''), actions);
  }));
  const table = h('table', { id: 'saved-secrets', dataset: { resizable: 'secrets' } },
    h('thead', {}, h('tr', {},
      h('th', { class: 'pick', dataset: { col: 'pick' } }, pickBox('Select all', { class: 'pick-all' })),
      h('th', { dataset: { col: 'label' } }, 'Label'),
      h('th', { dataset: { col: 'length' } }, 'Length'),
      h('th', { dataset: { col: 'added' } }, 'Added'),
      h('th', { class: 'actions' }, 'Actions'))),
    body);
  // The bar goes above the list, where a selection is being built rather than below where it ends.
  return h('div', {}, h('div', { class: 'tools' }, bulkSecrets(table)), table);
}

function addForm(enabled) {
  const form = h('form', { id: 'add-secret', class: 'form-row' },
    h('label', { class: 'field' }, 'Label', h('input', { type: 'text', name: 'label', placeholder: 'e.g. cloudflare-pat', required: true })),
    h('label', { class: 'field' }, 'Value', h('input', { type: 'password', name: 'value', placeholder: 'paste the credential', autocomplete: 'off', required: true })),
    h('button', { type: 'submit', class: 'primary', disabled: !enabled }, icon('plus'), 'Save'));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await postJSON('/api/secrets', { label: form.elements.label.value, value: form.elements.value.value });
      say(`Saved ${form.elements.label.value.trim()}.`, 'ok');
      form.reset();
      await paint();
    } catch (error) {
      sayError(error);
    }
  });
  track(form);                          // a secret typed and not saved is unsaved (D98)
  return form;
}

async function paint() {
  let data = { entries: [], enabled: false };
  try {
    data = await getJSON('/api/secrets');
  } catch (error) {
    sayError(error);
  }
  const saved = h('section', { class: 'card' }, h('h2', {}, 'Saved secrets'),
    h('p', { class: 'hint' }, 'Anything you know is a credential but has no recognisable shape (a Cloudflare or Shopify token, a webhook secret, a licence key). Each exact value is replaced with [REDACTED-user:label] before a request leaves this machine.'),
    addForm(data.enabled), savedTable(data.entries || []),
    h('p', { class: 'hint' }, 'Stored encrypted for this Windows user on this machine only (DPAPI). Values are never shown again, synced or logged; the evidence log records the label and a count.'));
  view.main.replaceChildren(h('div', { class: 'page-head' }, h('h1', {}, 'Secrets')), modeCard(view.ctx.status.secrets_mode), saved);
}

export async function render(main, ctx) {
  view.main = main;
  view.ctx = ctx;
  main.append(h('div', { class: 'page-head' }, h('h1', {}, 'Secrets')));
  await paint();
}
