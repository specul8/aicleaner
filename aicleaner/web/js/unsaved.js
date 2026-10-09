// Stop before unsaved changes are lost (D98).
//
// A form registers itself with track(); what it holds is compared with a snapshot taken when it was
// drawn (or last saved - rebase()). Changed and changed back reads the same, so it is not unsaved.
// confirmLeave() asks in an in-page dialog when anything still on the page differs: Leave forgets
// the forms and goes on, Stay keeps everything as it is.

import { h } from './dom.js';

let tracked = [];
let asking = null;

// Every input under the root, as the page would save it. Password fields included: a secret typed
// and not added is unsaved too. Anything marked data-unsaved-ignore is left out.
function fieldsOf(root) {
  return () => Array.from(root.querySelectorAll('input, select, textarea'))
    .filter((el) => !el.closest('[data-unsaved-ignore]') && !['button', 'submit', 'file'].includes(el.type))
    .map((el) => (el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value));
}

const snap = (read) => {
  try { return JSON.stringify(read()); } catch (error) { return ''; }
};

export function track(root, read) {
  const reader = read || fieldsOf(root);
  tracked = tracked.filter((t) => t.root !== root);
  tracked.push({ root, read: reader, base: snap(reader) });
}

export function rebase(root) {
  for (const t of tracked) if (t.root === root) t.base = snap(t.read);
}

export function forget(root) {
  tracked = root ? tracked.filter((t) => t.root !== root) : [];
}

export function dirty() {
  tracked = tracked.filter((t) => t.root.isConnected);
  return tracked.some((t) => snap(t.read) !== t.base);
}

function dialog() {
  let found = document.getElementById('unsaved-dialog');
  if (found) return found;
  found = h('dialog', { id: 'unsaved-dialog', class: 'sheet', 'aria-labelledby': 'unsaved-title' },
    h('h2', { id: 'unsaved-title' }, 'Unsaved changes'),
    h('p', {}, 'You have unsaved changes on this page. Leave anyway? Your changes will be lost.'),
    h('div', { class: 'form-row' },
      h('button', { type: 'button', class: 'unsaved-stay primary' }, 'Stay on this page'),
      h('button', { type: 'button', class: 'unsaved-leave' }, 'Leave and lose the changes')));
  document.body.append(found);
  return found;
}

// true to go on (nothing unsaved, or the person chose to leave), false to stay.
export function confirmLeave() {
  if (!dirty()) return Promise.resolve(true);
  if (asking) return asking;
  const box = dialog();
  asking = new Promise((resolve) => {
    const done = (leave) => {
      box.close();
      box.querySelector('.unsaved-stay').onclick = null;
      box.querySelector('.unsaved-leave').onclick = null;
      box.oncancel = null;
      asking = null;
      if (leave) forget();
      resolve(leave);
    };
    box.querySelector('.unsaved-stay').onclick = () => done(false);
    box.querySelector('.unsaved-leave').onclick = () => done(true);
    box.oncancel = (event) => { event.preventDefault(); done(false); };   // Escape means stay
    box.showModal();
    box.querySelector('.unsaved-stay').focus();
  });
  return asking;
}
