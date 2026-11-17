// A client's OneDrive share folder, set from the Session page's profiles editor (D99; it was on the
// Clients page). The folder files cross through between this machine and the one a session is
// driven from. One per client, stored on this machine only (client-shares.json), never in the
// client file: it is a fact about you, not client data, and a teammate's path would be wrong on
// their disk.

import { getJSON, postJSON, say, sayError } from './api.js';
import { h, icon } from './dom.js';
import { folderPicker, folderRoots, noFoldersYet } from './picker.js';

// Chosen with the picker only (the user, 2026-10-04): the folder is shown once, as text, and a
// choice in the picker is saved at once. No box to type in - through the tunnel you cannot see the
// disk to know what to type - and no Create button: the picker makes a new folder anywhere allowed.
function shareForm(cid, share, label, slot) {
  const current = share.share;
  // Hidden, but still the picker's target: it opens where this points and writes the choice here.
  const input = h('input', { type: 'hidden', class: 'share-path', value: current || '', 'data-unsaved-ignore': '' });
  const save = async (body, done) => {
    try {
      const result = await postJSON(`/api/clients/${encodeURIComponent(cid)}/share`, body);
      say(done(result), 'ok');
      await fillShare(slot, cid, label);
    } catch (error) {
      sayError(error);
    }
  };
  const clear = current ? h('button', { type: 'button', class: 'share-clear' }, icon('cross'), 'Forget') : null;
  input.addEventListener('input', () => {
    if (input.value.trim()) save({ path: input.value.trim() }, (r) => `Project assets folder set to ${r.share}.`);
  });
  if (clear) clear.addEventListener('click', () => save({ path: '' }, () => 'Project assets folder forgotten. The folder itself is untouched.'));
  // The same picker as everywhere else, bounded to folders.roots by the API, so browsing shows
  // exactly what will be accepted. Not a <form>: this sits inside the profiles editor.
  const pick = folderPicker(input, { label: current ? 'Choose another folder' : 'Choose a folder' });
  const row = h('div', { class: 'form-row share-form' }, input, pick, clear);
  folderRoots().then((info) => {
    if (info.roots && info.roots.length) return;
    row.before(noFoldersYet(info.editable));
    pick.disabled = true;
  }).catch(() => {});
  return [h('p', { class: current ? 'share-current' : 'share-current muted' },
    current || 'No project assets folder set'), row];
}

// What a project assets folder is: the (i) beside "Project assets folder" in the profile editor (f184).
export const SHARE_WHY = 'The topic\'s OneDrive folder that files cross through between this machine and the one you '
  + 'drive a session from. It shows first in the Files tree, and the session can read and write it. One per topic, '
  + 'kept on this machine only; choosing a folder saves it at once.';

// Fills `slot` with the share control for one client. Nothing when the page has no proxy behind it.
export async function fillShare(slot, cid, label) {
  try {
    const share = await getJSON(`/api/clients/${encodeURIComponent(cid)}/share`);
    // f184: what the folder is lives in the (i) beside the block's label (SHARE_WHY), not a paragraph here.
    slot.replaceChildren(...shareForm(cid, share, label, slot));
  } catch (error) {
    slot.replaceChildren();
  }
}

export function shareSlot(cid, label) {
  const slot = h('div', { class: 'share-slot', dataset: { client: cid } });
  if (cid) fillShare(slot, cid, label || cid);
  return slot;
}
