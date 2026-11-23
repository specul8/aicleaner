// A topic's client assets folder (its OneDrive "share"): the folder files cross through between this machine and the
// client. One per topic, stored on this machine only (client-shares.json), never in the topic file: it is a fact about
// you, not client data, and a teammate's path would be wrong on their disk.
//
// f187 (Brad, 2026-11-19): "each project session reads the client assets folder from the topic. It can be changed either
// in the topic or the project session configuration, but it changes it in both places (as well as any other project
// session)." This is the topic page's side (the project session editor's is the Assets role button): the folder, Choose /
// Change / Clear, and the project sessions that read it, each with an Edit link.

import { getJSON, postJSON, say, sayError } from './api.js';
import { h, icon } from './dom.js';
import { folderPicker, folderRoots, noFoldersYet } from './picker.js';

// Chosen with the picker only (the user, 2026-10-04): the folder is shown once, as text, and a choice in the picker is
// saved at once. No box to type in - through the tunnel you cannot see the disk to know what to type.
function shareForm(cid, share, slot) {
  const current = share.share;
  // Hidden, but still the picker's target: it opens where this points and writes the choice here.
  const input = h('input', { type: 'hidden', class: 'share-path', value: current || '', 'data-unsaved-ignore': '' });
  const save = async (body, done) => {
    try {
      const result = await postJSON(`/api/clients/${encodeURIComponent(cid)}/share`, body);
      say(done(result), 'ok');
      for (const note of result.notes || []) say(note);
      await fillShare(slot, cid);
    } catch (error) {
      sayError(error);
    }
  };
  const clear = current ? h('button', { type: 'button', class: 'share-clear', title: 'The topic has no client assets '
    + 'folder after this; the folder itself, and every file in it, is left exactly where it is' }, icon('cross'), 'Clear') : null;
  input.addEventListener('input', () => {
    if (input.value.trim()) save({ path: input.value.trim() }, (r) => `Client assets folder set to ${r.share}.`);
  });
  if (clear) clear.addEventListener('click', () => save({ path: '' }, () => 'Client assets folder cleared. The folder itself is untouched.'));
  // The same picker as everywhere else, bounded to folders.roots by the API, so browsing shows exactly what will be
  // accepted.
  const pick = folderPicker(input, { label: current ? 'Change' : 'Choose a folder',
    title: current ? 'Choose another client assets folder for this topic' : 'Choose the client assets folder for this topic' });
  const row = h('div', { class: 'form-row share-form' }, input, pick, clear);
  folderRoots().then((info) => {
    if (info.roots && info.roots.length) return;
    row.before(noFoldersYet(info.editable));
    pick.disabled = true;
  }).catch(() => {});
  return [h('p', { class: current ? 'share-current mono' : 'share-current muted' },
    current || 'No client assets folder set'), row];
}

// The project sessions that read the topic's folder, each with Edit (the Session page's editor, on its tab).
function sessionsList(cid, share) {
  const sessions = share.sessions || [];
  if (!sessions.length) {
    return h('p', { class: 'hint topic-sessions-none' }, 'No project session uses this topic yet. ',
      h('a', { href: '#/session', class: 'topic-add-session' }, 'Add a project session'), ' on the Session page.');
  }
  return h('ul', { class: 'topic-sessions' }, ...sessions.map((s) => h('li', { class: 'topic-session', dataset: { session: s.name } },
    h('strong', {}, s.name), ' - ',
    s.assets ? h('span', { class: 'mono' }, s.assets) : h('span', { class: 'muted' }, 'no client assets folder set'), ' ',
    h('a', { class: 'topic-session-edit', href: `#/session/${encodeURIComponent(s.name)}/edit`,
      title: `Open ${s.name}'s project session settings` }, 'Edit'))));
}

// What a client assets folder is: said beside the label.
export const SHARE_WHY = 'The topic\'s client assets folder (usually in OneDrive): the files that cross between you and '
  + 'the client. Every project session of the topic reads it - it shows among its folders with the Assets role, '
  + 'wherever you put it in the order. Changing it here, or with the Assets button in a project session\'s settings, '
  + 'changes it for all of them. Kept on this machine only; choosing a folder saves it at once.';

// Fills `slot` with the control and the sessions list for one topic. Nothing when the page has no proxy behind it.
export async function fillShare(slot, cid) {
  try {
    const share = await getJSON(`/api/clients/${encodeURIComponent(cid)}/share`);
    slot.replaceChildren(...shareForm(cid, share, slot),
      h('p', { class: 'p-list-label' }, 'Project sessions using it'), sessionsList(cid, share));
  } catch (error) {
    slot.replaceChildren();
  }
}

export function shareSlot(cid) {
  const slot = h('div', { class: 'share-slot', dataset: { client: cid } });
  if (cid) fillShare(slot, cid);
  return slot;
}
