// Creating a client from the menu, rather than from the bottom of the Review page.
//
// The form asks for three things and only one of them is obvious, so it says which is which. The
// id is the one that matters outside this window: it names the file, keys the mapping, and is
// what every command line wants.

import { postJSON, say, sayError } from '../api.js';
import { refreshStatus } from '../app.js';
import { h } from '../dom.js';

const FIELDS = [
  ['id', 'Client id', 'e.g. acme',
   'The key. Names the file (acme.json), keys the mapping, and is what the command line wants.'],
  ['label', 'Display name', 'e.g. ACME Pty Ltd',
   'What you read, everywhere in here. Optional - the id is used when it is empty.'],
  ['index', 'Index', 'automatic',
   'The number inside the tokens the AI sees: index 7 makes Client07 and client07.invalid.'],
];

function field([name, label, placeholder, why]) {
  const input = name === 'index'
    ? h('input', { type: 'number', name, min: '1', placeholder })
    : h('input', {
      type: 'text', name, placeholder, maxlength: name === 'label' ? '80' : '64',
      required: name === 'id' ? true : null,
      pattern: name === 'id' ? '[A-Za-z0-9](?:[A-Za-z0-9_]|-){0,63}' : null,
    });
  return h('label', { class: 'field wide' }, label, input, h('span', { class: 'hint' }, why));
}

// A dialog rather than a panel: it is a decision with three answers, and everything behind it
// keeps its place while it is made. Esc and the backdrop close it, as a dialog should.
export function askForNewClient(onCreated) {
  document.getElementById('new-client-dialog')?.remove();
  const form = h('form', { method: 'dialog' },
    h('h2', {}, 'Add a new client'),
    h('p', { class: 'hint' },
      'A client is one engagement. Its names and identifiers are replaced in every session, not '
      + 'only the ones you label as theirs.'),
    ...FIELDS.map(field),
    h('div', { class: 'form-row' },
      h('button', { type: 'submit', class: 'primary' }, 'Create'),
      h('button', { type: 'button', class: 'cancel' }, 'Cancel')));
  const dialog = h('dialog', { id: 'new-client-dialog', class: 'sheet' }, form);
  document.body.append(dialog);

  form.querySelector('.cancel').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => dialog.remove());
  dialog.addEventListener('click', (event) => {           // the backdrop, not the form
    if (event.target === dialog) dialog.close();
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = form.elements.id.value.trim();
    const index = form.elements.index.value.trim();
    const label = form.elements.label.value.trim();
    try {
      await postJSON('/api/mapping/client', { id, index: index ? Number(index) : null, label: label || null });
      // Before navigating: the client page reads the polled status, and a client it has not
      // heard of yet renders as "there is no client called ...".
      await refreshStatus();
      say(`Created ${label || id}.`, 'ok');
      dialog.close();
      if (onCreated) onCreated(id);
      else window.location.hash = `#/clients/${encodeURIComponent(id)}`;
    } catch (error) {
      sayError(error);
    }
  });
  dialog.showModal();
  form.elements.id.focus();
  return dialog;
}
