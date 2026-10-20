// Casual chats and Chat now (item J, feedback #5; spec 2026-10-20-casual-chats-and-chat-now.md).
//
// Chat now (the menu): a new conversation in the Casual chats project session - made first if needed,
// asking for its folder when none is set - opened on the Session page. The folder chooser here is also
// the setup wizard's "Casual chats" step and the Settings card's field.

import { getJSON, postJSON, say, sayError } from './api.js';
import { h } from './dom.js';
import { arrangeSheet } from './sheet.js';

// A folder field with the drives this machine has offered as one-click choices. onSaved(answer) runs
// once the folder is set up (created, allowed, Casual chats made).
export function casualFolderField(onSaved, saveLabel = 'Save the folder') {
  const input = h('input', { type: 'text', name: 'casual-folder', class: 'wide', autocomplete: 'off',
    placeholder: 'e.g. C:\\Users\\you\\OneDrive\\AI Chats', 'aria-label': 'Folder for casual chats' });
  const offers = h('div', { class: 'form-row casual-offers' });
  const note = h('p', { class: 'hint casual-note' });
  const save = h('button', { type: 'button', class: 'casual-save' }, saveLabel);
  getJSON('/api/session/casual').then((info) => {
    if (info.folder) {
      input.value = info.folder;
      note.textContent = `Casual chats keeps its files in ${info.folder}.`;
    }
    offers.replaceChildren(...(info.suggestions || []).map((s) => {
      const pick = h('button', { type: 'button', class: 'casual-offer', title: s.path }, s.label);
      pick.addEventListener('click', () => { input.value = s.path; input.focus(); });
      return pick;
    }));
  }).catch(() => {});
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const answer = await postJSON('/api/session/casual', { folder: input.value });
      note.textContent = `Casual chats keeps its files in ${answer.folder}.`;
      say(`Casual chats is set up in ${answer.folder}.`, 'ok');
      if (onSaved) await onSaved(answer);
    } catch (error) {
      sayError(error);
    } finally {
      save.disabled = false;
    }
  });
  return { element: h('div', { class: 'casual-field' }, offers, h('div', { class: 'form-row' }, input, save), note), input };
}

function askForFolder() {
  return new Promise((resolve) => {
    let done = false;
    const field = casualFolderField(async () => { done = true; close(); resolve(true); }, 'Save and start');
    const cancel = h('button', { type: 'button' }, 'Cancel');
    const dialog = h('dialog', { id: 'casual-folder', class: 'sheet casual-dialog', 'aria-label': 'A folder for casual chats' },
      h('h2', {}, 'A folder for casual chats'),
      h('p', { class: 'hint' }, 'Chats that belong to no client or project keep their files here. A OneDrive or Google Drive '
        + 'folder is synced, so what a chat makes is on your other devices too.'),
      field.element,
      h('div', { class: 'fb-footer' }, cancel));
    const close = () => { dialog.close(); dialog.remove(); if (!done) resolve(false); };
    cancel.addEventListener('click', close);
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
    document.body.append(dialog);
    arrangeSheet(dialog, { dirty: () => false });
    dialog.showModal();
    field.input.focus();
  });
}

function openSession(name) {
  const target = `#/session/${encodeURIComponent(name)}/chat`;
  if (window.location.hash === target) window.dispatchEvent(new HashChangeEvent('hashchange'));
  else window.location.hash = target;
}

// The menu's Chat now.
export async function chatNow() {
  try {
    let answer = await postJSON('/api/session/chat-now', {});
    if (answer.needs_folder) {
      if (!(await askForFolder())) return;
      answer = await postJSON('/api/session/chat-now', {});
    }
    say('A new casual chat - ask away.', 'ok');
    openSession(answer.name);
  } catch (error) {
    sayError(error);
  }
}
