// Creating a client from the menu, rather than from the bottom of the Review page.
//
// The form asks for three things and only one of them is obvious, so it says which is which. The
// id is the one that matters outside this window: it names the file, keys the mapping, and is
// what every command line wants.

import { postJSON, say, sayError } from '../api.js';
import { refreshStatus } from '../app.js';
import { h } from '../dom.js';
import { openClientWizard, setWizardSkipped, wizardSkipped } from './clientwizard.js';
import { arrangeSheet } from '../sheet.js';

// The short form (feature 111): the id and display name, with the wizard's help. The index is not
// asked for: it is worked out from the id, every time.
const FIELDS = [
  ['id', 'Client ID', 'e.g. acme',
   'Names the client\'s file (acme.json) and keys the mapping. Letters, digits, hyphens and underscores.'],
  ['label', 'Display name', 'e.g. ACME Pty Ltd',
   'What you read, everywhere in here.'],
];

function field([name, label, placeholder, why]) {
  const input = h('input', {
    type: 'text', name, placeholder, maxlength: name === 'label' ? '80' : '64',
    required: name === 'id' ? true : null,
    pattern: name === 'id' ? '[A-Za-z0-9](?:[A-Za-z0-9_]|-){0,63}' : null,
  });
  return h('label', { class: 'field wide' }, label, input, h('span', { class: 'hint' }, why));
}

// A dialog rather than a panel: it is a decision with three answers, and everything behind it
// keeps its place while it is made. Esc and the backdrop close it, as a dialog should.
export function askForNewClient(onCreated) {
  // The Add Client wizard, unless "Skip the wizard next time" was ticked on its last page.
  if (!wizardSkipped()) return openClientWizard(onCreated);
  document.getElementById('new-client-dialog')?.remove();
  const form = h('form', { method: 'dialog' },
    h('h2', {}, 'Add a new client'),
    h('p', { class: 'hint' },
      'A client is one engagement. Its names and identifiers are replaced in every session, not '
      + 'only the ones you label as theirs. Add its other names, domains and identifiers on its page next.'),
    ...FIELDS.map(field),
    h('p', { class: 'hint' }, h('button', { type: 'button', class: 'link use-wizard' }, 'Use the step-by-step wizard instead')),
    h('div', { class: 'form-row' },
      h('button', { type: 'submit', class: 'primary' }, 'Create'),
      h('button', { type: 'button', class: 'cancel' }, 'Cancel')));
  const dialog = h('dialog', { id: 'new-client-dialog', class: 'sheet' }, form);
  document.body.append(dialog);
  arrangeSheet(dialog);

  form.querySelector('.cancel').addEventListener('click', () => dialog.close());
  form.querySelector('.use-wizard').addEventListener('click', () => {
    setWizardSkipped(false);
    dialog.close();
    openClientWizard(onCreated);
  });
  // The display name's hint follows the id, as the wizard's does.
  form.elements.id.addEventListener('input', () => {
    const id = form.elements.id.value.trim();
    form.elements.label.placeholder = id ? `leave blank to use ${id} as the Display name` : 'e.g. ACME Pty Ltd';
  });
  dialog.addEventListener('close', () => dialog.remove());
  dialog.addEventListener('click', (event) => {           // the backdrop, not the form
    if (event.target === dialog) dialog.close();
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = form.elements.id.value.trim();
    const label = form.elements.label.value.trim();
    try {
      await postJSON('/api/mapping/client', { id, index: null, label: label || null });
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
