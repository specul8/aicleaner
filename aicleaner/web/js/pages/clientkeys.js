// A key per client, and changes held for review (DECISIONS 51).
//
// The client's folder is shared in OneDrive with that engagement's people. A colleague's aicleaner
// signs each change with the client's key, so it is used at once; a change made any other way - by
// hand, by another tool, from the OneDrive web editor - is held here, last-good in use meanwhile,
// until someone looks at it. The key itself is shown only when it is made, to be sent to the
// others out of band; it never goes in the shared folder.

import { getJSON, postJSON, say, sayError } from '../api.js';
import { h } from '../dom.js';

const enc = encodeURIComponent;

// What changed, for the held callout: names added and names taken out (a removal is a leak).
function heldList(label, values) {
  if (!values.length) return null;
  return h('p', {}, h('b', {}, `${label}: `), values.join(', '));
}

const WHY = {
  unsigned: 'it was changed without the client key',
  altered: 'it was changed after it was signed, without the client key',
  'other-key': 'it was signed with a different key - a rotated one, or someone else\'s',
};

export function heldCallout(row, reload) {
  if (!row.state.includes('held') || !row.held) return null;
  const held = row.held;
  const accept = h('button', { type: 'button', class: 'primary held-accept' }, 'Accept the change');
  const reject = h('button', { type: 'button', class: 'held-reject' }, 'Reject it (put the last good version back)');
  const act = (yes) => async () => {
    try {
      await postJSON(`/api/clients/${enc(row.id)}/held`, { accept: yes });
      say(yes ? 'Accepted and signed.' : 'Rejected: the last good version is back.', 'ok');
      await reload();
    } catch (error) { sayError(error); }
  };
  accept.addEventListener('click', act(true));
  reject.addEventListener('click', act(false));
  return h('div', { class: 'callout held' },
    h('b', {}, `A change to this client is waiting for review: ${WHY[held.why] || held.why}. `),
    row.state.includes('broken') ? 'There is no earlier version on this machine, so AI traffic for it is stopped.'
      : 'The last good version is in use until you decide.',
    heldList('Added', held.added || []), heldList('Taken out', held.removed || []),
    h('div', { class: 'form-row' }, accept, reject));
}

function shownOnce(key) {
  const copy = h('button', { type: 'button' }, 'Copy');
  copy.addEventListener('click', () => {
    navigator.clipboard.writeText(key).then(() => say('Key copied.', 'ok'), sayError);
  });
  return h('div', { class: 'callout key-shown' },
    h('b', {}, 'The client key - shown this once. '),
    'Send it to the others on this engagement by Teams chat or a password manager, never in the shared folder. ',
    h('code', { class: 'client-key' }, key), ' ', copy);
}

async function keyAction(cid, payload, after) {
  try {
    const answer = await postJSON(`/api/clients/${enc(cid)}/key`, payload);
    await after(answer);
  } catch (error) { sayError(error); }
}

// The Client key section: generate (first person) or enter (everyone else); then rotate.
export async function keySection(cid, reload) {
  const sec = h('section', { class: 'client-key-section' }, h('h3', {}, 'Client key'));
  let state;
  try {
    state = await getJSON(`/api/clients/${enc(cid)}/key`);
  } catch (error) {
    sec.append(h('p', { class: 'error-text' }, error.message));
    return sec;
  }
  const slot = h('div', {});
  const show = (answer) => { slot.replaceChildren(shownOnce(answer.key)); };
  if (state.has_key) {
    const rotate = h('button', { type: 'button', class: 'key-rotate' }, 'Rotate (someone left)');
    rotate.addEventListener('click', () => keyAction(cid, { action: 'rotate' }, async (answer) => {
      show(answer);
      say('Rotated: the old key stops working now. Send the new one to everyone who stays.', 'ok');
    }));
    sec.append(h('p', {}, `This machine has the key (id ${state.key_id}). Changes made without it are held for review.`),
      rotate, slot);
    return sec;
  }
  const paste = h('input', { type: 'password', class: 'key-paste', 'aria-label': 'Client key', autocomplete: 'off' });
  const enter = h('button', { type: 'button', class: 'key-enter' }, 'Enter key');
  const make = h('button', { type: 'button', class: 'key-generate' }, 'Generate key (first on this engagement)');
  enter.addEventListener('click', () => keyAction(cid, { action: 'enter', key: paste.value }, async () => {
    say('Key entered.', 'ok');
    await reload();
  }));
  make.addEventListener('click', () => keyAction(cid, { action: 'generate' }, async (answer) => {
    show(answer);
    make.disabled = true;
  }));
  sec.append(h('p', { class: 'hint' }, 'No key on this machine: changes to this client\'s shared file are used as they arrive.'),
    h('div', { class: 'form-row' }, paste, enter), h('div', { class: 'form-row' }, make), slot);
  return sec;
}
