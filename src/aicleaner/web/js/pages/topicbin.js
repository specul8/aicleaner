// D258 (Brad, 2026-11-19): "how do I delete a Topic from the AI cleaner system? ... add in a bin icon on the topic page
// (with confirmation modal) that deletes a topic." The bin icon's modal says what will happen before anything does; the
// topic waits in Recently deleted (Topics page) for 30 days and can be restored from there.

import { getJSON, postJSON, say, sayError } from '../api.js';
import { h, iconButton } from '../dom.js';
import { arrangeSheet } from '../sheet.js';

const when = (seconds) => new Date(seconds * 1000).toLocaleString();

function sheet(id, label, ...children) {
  const dialog = h('dialog', { id, class: 'sheet topic-bin-dialog', 'aria-label': label }, ...children);
  document.body.append(dialog);
  arrangeSheet(dialog, { dirty: () => false });
  dialog.addEventListener('close', () => { if (dialog.isConnected) dialog.remove(); });
  return dialog;
}

// The modal: what deleting does and does not do, then Delete (disabled while a session using the topic runs) or Cancel.
export async function confirmDeleteTopic(cid, done) {
  let use;
  try {
    use = await getJSON(`/api/clients/${encodeURIComponent(cid)}/delete`);
  } catch (error) { sayError(error); return; }
  const label = use.label || cid;
  const sessions = use.sessions || [];
  const running = use.running || [];
  const ok = h('button', { type: 'button', class: 'danger topic-delete-ok', disabled: running.length ? true : null },
    'Delete the topic');
  const cancel = h('button', { type: 'button', class: 'topic-delete-cancel' }, 'Cancel');
  const items = [
    h('li', {}, `Its terms stop being filtered at once: names only ${label} knew are no longer hidden from the AI service.`),
    sessions.length
      ? h('li', { class: 'topic-delete-sessions' }, `${sessions.length} project session${sessions.length === 1 ? '' : 's'} `
        + `use${sessions.length === 1 ? 's' : ''} it and become${sessions.length === 1 ? 's' : ''} Topic: None (Global only): `,
      h('strong', {}, sessions.join(', ')), '.')
      : h('li', { class: 'topic-delete-sessions' }, 'No project session uses it.'),
    h('li', {}, use.assets ? `Its client assets folder (${use.assets}) and every file in it are not touched.`
      : 'Its OneDrive folder and files are not touched.'),
    h('li', {}, 'It waits in Topics > Recently deleted for 30 days, and can be restored from there.'),
  ];
  const dialog = sheet('topic-delete', `Delete topic ${label}`,
    h('h2', {}, `Delete topic ${label}?`),
    h('ul', { class: 'topic-delete-what' }, ...items),
    running.length ? h('p', { class: 'bad topic-delete-running', role: 'alert' },
      `${running.join(', ')} ${running.length === 1 ? 'is' : 'are'} running with this topic - stop `
      + `${running.length === 1 ? 'it' : 'them'} first, then delete.`) : null,
    h('div', { class: 'fb-footer' }, cancel, ok));
  cancel.addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); dialog.close(); });
  ok.addEventListener('click', async () => {
    ok.disabled = true;
    try {
      const answer = await postJSON(`/api/clients/${encodeURIComponent(cid)}/delete`, {});
      dialog.close();
      say(`Topic ${label} deleted. Restore it from Topics > Recently deleted within 30 days.`, 'ok');
      for (const note of answer.notes || []) say(note);
      if (done) await done(answer);
    } catch (error) { sayError(error); ok.disabled = false; }
  });
  dialog.showModal();
  cancel.focus();
}

export function deleteTopicButton(cid, done) {
  const button = iconButton('trash', 'Delete this topic', { id: 'topic-delete', class: 'icon danger topic-delete' });
  button.addEventListener('click', () => confirmDeleteTopic(cid, done));
  return button;
}

// Topics > Recently deleted: each deleted topic, when, which sessions it unlinked, and Restore.
export async function openDeletedTopics(restored) {
  const list = h('ul', { class: 'bin-list topic-bin-list' });
  const status = h('p', { class: 'hint' }, 'Loading...');
  const close = h('button', { type: 'button', class: 'bin-close' }, 'Close');
  const dialog = sheet('topics-deleted', 'Recently deleted topics', h('h2', {}, 'Recently deleted topics'),
    h('p', { class: 'hint' }, 'Kept for 30 days, then removed for good. Restoring puts the topic back and links the '
      + 'project sessions it was taken from again.'), status, list, h('div', { class: 'form-row' }, close));
  close.addEventListener('click', () => dialog.close());
  const load = async () => {
    try {
      const rows = (await getJSON('/api/topics/deleted')).deleted || [];
      status.textContent = rows.length ? '' : 'Nothing here.';
      list.replaceChildren(...rows.map((row) => {
        const back = h('button', { type: 'button', class: 'topic-restore', dataset: { entry: row.entry } }, 'Restore');
        back.addEventListener('click', async () => {
          back.disabled = true;
          try {
            const answer = await postJSON('/api/topics/restore', { entry: row.entry });
            say(`Topic ${answer.label || answer.id} restored${(answer.relinked || []).length
              ? `; ${answer.relinked.join(', ')} use${answer.relinked.length === 1 ? 's' : ''} it again` : ''}.`, 'ok');
            if (restored) await restored(answer);
            load();
          } catch (error) { sayError(error); back.disabled = false; }
        });
        return h('li', { class: 'bin-row topic-bin-row', dataset: { id: row.id } }, h('strong', {}, row.label || row.id),
          h('span', { class: 'hint' }, ` deleted ${when(row.deleted_at)}`,
            (row.sessions || []).length ? ` - was used by ${row.sessions.join(', ')}` : ''), ' ', back);
      }));
    } catch (error) { status.textContent = `Could not load it: ${error.message || error}`; }
  };
  dialog.showModal();
  close.focus();
  load();
}
