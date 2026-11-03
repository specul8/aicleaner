// Move a conversation to another project session (J3; Brad, 2026-10-20). The dialog lists every conversation
// of this session (not just the newest 25) and every other session as a destination, one per line, and
// offers - unticked - to MOVE the files the chosen conversation made there too, listing them first. Files a
// command made cannot be told apart from the rest, so they stay (said).

import { getJSON, postJSON, say, sayError } from './api.js';
import { h, setChildren } from './dom.js';
import { arrangeSheet } from './sheet.js';

function label(row) {
  return row.label || row.title || row.id;
}

// name: the project session it is in; chosenId: the conversation to start on; onMoved: after a move.
export async function openMoveConversation(name, chosenId, onMoved) {
  let rows;
  let targets;
  try {
    const past = await getJSON(`/api/session/past?name=${encodeURIComponent(name)}&limit=5000`);
    rows = (past.projects || []).filter((group) => !group.outside).flatMap((group) => group.sessions || []);
    rows.sort((a, b) => (b.updated || 0) - (a.updated || 0));
    if (!rows.length) { say('No conversations in this project session to move.', 'warn'); return; }
    const first = rows.find((r) => r.id === chosenId) || rows[0];
    targets = (await getJSON(`/api/session/conversations/move?name=${encodeURIComponent(name)}&id=${encodeURIComponent(first.id)}`)).targets;
    chosenId = first.id;
  } catch (error) { sayError(error); return; }
  if (!targets.length) { say('There is no other project session to move it to.', 'warn'); return; }

  const conversation = h('select', { name: 'move-conversation', 'aria-label': 'Conversation to move' },
    ...rows.map((row) => h('option', { value: row.id, selected: row.id === chosenId ? true : null }, label(row))));
  const destinations = h('div', { class: 'move-targets', role: 'radiogroup', 'aria-label': 'Move it to' },
    ...targets.map((t, i) => h('label', { class: 'check move-target' },
      h('input', { type: 'radio', name: 'move-to', value: t.name, checked: i === 0 ? true : null }),
      ` ${t.name}`, t.client ? h('span', { class: 'muted' }, ` - ${t.client}`) : null)));
  const files = h('input', { type: 'checkbox', name: 'move-files' });
  const fileList = h('div', { class: 'move-files-list' });
  const showFiles = async () => {
    setChildren(fileList, h('p', { class: 'hint' }, 'Looking for the files it made...'));
    try {
      const info = await getJSON(`/api/session/conversations/move?name=${encodeURIComponent(name)}&id=${encodeURIComponent(conversation.value)}`);
      setChildren(fileList, info.files.length
        ? h('ul', { class: 'mono' }, ...info.files.map((f) => h('li', {}, f)))
        : h('p', { class: 'hint' }, 'It has not written any files that are still here.'));
    } catch (error) { setChildren(fileList, h('p', { class: 'hint bad' }, error.message || String(error))); }
  };
  conversation.addEventListener('change', showFiles);
  const go = h('button', { type: 'button', class: 'move-go' }, 'Move');
  const cancel = h('button', { type: 'button' }, 'Cancel');
  const dialog = h('dialog', { id: 'move-conversation', class: 'sheet move-dialog', 'aria-label': 'Move conversation' },
    h('h2', {}, 'Move conversation'),
    h('label', { class: 'field' }, `Conversation in ${name}`, conversation),
    h('div', { class: 'field' }, h('span', {}, 'Move it to'), destinations),
    h('p', { class: 'hint' }, 'It is filed under the chosen session\'s home folder and continues there, with that session\'s '
      + 'folders, client filter and brief. Its title goes with it.'),
    h('label', { class: 'check' }, files, ' Also move the files it made (moved, not copied)'),
    fileList,
    h('p', { class: 'hint' }, 'Each goes to the same place under the new home folder; one whose name is already taken there '
      + 'stays where it is. Files a command made cannot be told apart from the rest, so they are not moved.'),
    h('div', { class: 'fb-footer' }, cancel, go));
  const close = () => { dialog.close(); dialog.remove(); };
  cancel.addEventListener('click', close);
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
  go.addEventListener('click', async () => {
    const to = (dialog.querySelector('input[name="move-to"]:checked') || {}).value;
    if (!to) return;
    go.disabled = true;
    try {
      const done = await postJSON('/api/session/conversations/move', { name, id: conversation.value, to, files: files.checked });
      close();
      const moved = done.files_moved.length ? ` ${done.files_moved.length} file(s) moved with it.` : '';
      const left = done.files_left.length ? ` ${done.files_left.length} left where they were (the name was taken): ${done.files_left.join(', ')}` : '';
      say(`Moved to ${done.to}.${moved}${left}`, left ? 'warn' : 'ok');
      if (onMoved) await onMoved(done);
    } catch (error) { go.disabled = false; sayError(error); }
  });
  document.body.append(dialog);
  arrangeSheet(dialog, { dirty: () => false });
  dialog.showModal();
  showFiles();
}
