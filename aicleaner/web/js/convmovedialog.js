// Move a conversation to another project session (J3; Brad, 2026-10-20). The dialog names the conversation,
// lists the other project sessions, and offers - unticked - to MOVE the files it made there too, listing
// them first. Files a command made cannot be told apart from the rest, so they stay (said).

import { getJSON, postJSON, say, sayError } from './api.js';
import { h } from './dom.js';
import { arrangeSheet } from './sheet.js';

// name: the project session it is in; row: the conversation (id, label/title); onMoved: after a move.
export async function openMoveConversation(name, row, onMoved) {
  let info;
  try {
    info = await getJSON(`/api/session/conversations/move?name=${encodeURIComponent(name)}&id=${encodeURIComponent(row.id)}`);
  } catch (error) { sayError(error); return; }
  if (!info.targets.length) { say('There is no other project session to move it to.', 'warn'); return; }
  const choices = info.targets.map((t, i) => h('label', { class: 'check move-target' },
    h('input', { type: 'radio', name: 'move-to', value: t.name, checked: i === 0 ? true : null }),
    ` ${t.name}`, t.client ? h('span', { class: 'muted' }, ` - ${t.client}`) : null,
    t.home ? h('span', { class: 'muted mono move-home' }, `  ${t.home}`) : null));
  const files = h('input', { type: 'checkbox', name: 'move-files' });
  const fileList = info.files.length
    ? h('ul', { class: 'move-files-list mono' }, ...info.files.map((f) => h('li', {}, f)))
    : h('p', { class: 'hint' }, 'It has not written any files that are still here.');
  const go = h('button', { type: 'button', class: 'move-go' }, 'Move');
  const cancel = h('button', { type: 'button' }, 'Cancel');
  const dialog = h('dialog', { id: 'move-conversation', class: 'sheet move-dialog', 'aria-label': 'Move a conversation' },
    h('h2', {}, 'Move this conversation'),
    h('p', {}, h('b', {}, row.label || row.title || row.id), ` - from ${name} to:`),
    h('div', { class: 'move-targets' }, ...choices),
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
      const done = await postJSON('/api/session/conversations/move', { name, id: row.id, to, files: files.checked });
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
}
