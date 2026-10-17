// Every dialog laid out one way (the user, 2026-10-17): what it is for fixed at the top, its buttons
// fixed at the bottom, and the content scrolling between them - so the title and the buttons are
// always in view. Each has a Cancel on the left, and a click on the page behind it cancels, as Escape
// does - unless there are changes in it that would be lost, when it says so instead.
//
// Called on a dialog once it is built and before showModal(). Cancelling - the button, a click
// behind, Escape - goes through the dialog's own 'cancel' event, so each dialog's own handling of
// Escape is what runs; a dialog with none is closed.
import { say } from './api.js';
import { h } from './dom.js';

const CANCEL_TEXT = /^(cancel|close)$/i;

function cancelButtonIn(foot) {
  return [...foot.querySelectorAll('button')].find((b) => CANCEL_TEXT.test(b.textContent.trim())) || null;
}

const SAVE_TEXT = /\bsave\b/i;
const DELETE_TEXT = /^(delete|remove)\b|lose the changes/i;

// Save green, Delete (and anything that throws work away) red; the rest as they were.
function colour(actions) {
  for (const button of actions.querySelectorAll('button')) {
    const text = button.textContent.trim();
    const kind = DELETE_TEXT.test(text) ? 'delete' : SAVE_TEXT.test(text) ? 'save' : '';
    if (button.classList.contains('sheet-save') !== (kind === 'save')) button.classList.toggle('sheet-save', kind === 'save');
    if (button.classList.contains('sheet-delete') !== (kind === 'delete')) button.classList.toggle('sheet-delete', kind === 'delete');
  }
}

export function requestCancel(dialog) {
  const event = new Event('cancel', { cancelable: true });
  dialog.dispatchEvent(event);
  if (!event.defaultPrevented && dialog.open) dialog.close();
}

// opts: head - the elements to fix at the top (default: a leading heading);
//       cancel - the button that cancels, when it is not called Cancel or Close;
//       dirty() - true while there are changes a click behind must not throw away (default: anything
//                 typed or changed in the dialog since it opened);
//       layout - false for a dialog with its own layout (the folder picker): only the click behind.
export function arrangeSheet(dialog, opts = {}) {
  if (dialog.dataset.sheet) return dialog;
  dialog.dataset.sheet = '1';
  if (opts.layout !== false) layOut(dialog, opts);
  let edited = false;
  dialog.addEventListener('input', () => { edited = true; });
  dialog.addEventListener('change', (event) => { if (!event.target.matches('input[type=file]')) edited = true; });
  const dirty = opts.dirty || (() => edited);
  // A click on the backdrop reaches the dialog itself, outside its box.
  dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    const inside = event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
    if (inside) return;
    if (dirty()) { say('There are changes here: use Cancel to discard them, or finish first.', 'warn'); return; }
    requestCancel(dialog);
  });
  return dialog;
}

function layOut(dialog, opts) {
  // A dialog that is one form lays out inside the form, so submitting still works.
  const only = dialog.children.length === 1 && dialog.firstElementChild.tagName === 'FORM' ? dialog.firstElementChild : null;
  const box = only || dialog;
  if (only) only.classList.add('sheet-form');
  const kids = [...box.children];
  const head = opts.head ? [].concat(opts.head).filter(Boolean)
    : kids.filter((el, i) => i === 0 && /^H[1-3]$/.test(el.tagName));
  const last = kids[kids.length - 1];
  let foot = last && !head.includes(last) && (last.matches('.fb-footer, .form-row') && last.querySelector('button')) ? last : null;
  if (!foot) {
    foot = h('div', { class: 'fb-footer' });
    box.append(foot);
  }
  foot.classList.add('sheet-foot');
  // Cancel on the left: the dialog's own, moved there, or one made for it.
  let cancel = opts.cancel || cancelButtonIn(foot);
  if (!cancel) {
    cancel = h('button', { type: 'button', class: 'sheet-cancel' }, 'Cancel');
    cancel.addEventListener('click', () => requestCancel(dialog));
  }
  if (foot.firstElementChild !== cancel) foot.prepend(cancel);
  // Everything else on the right (the user, 2026-10-17); Cancel uncoloured, Save green, Delete red -
  // looked at again whenever a label changes (Next becomes Import, Delete counts its ticks).
  cancel.classList.remove('primary', 'danger');
  cancel.classList.add('sheet-cancel-button');
  const actions = h('span', { class: 'sheet-actions' }, ...[...foot.childNodes].filter((n) => n !== cancel));
  foot.replaceChildren(cancel, actions);
  colour(actions);
  new MutationObserver(() => colour(actions)).observe(actions, { subtree: true, childList: true, characterData: true });
  const middle = [...box.children].filter((el) => !head.includes(el) && el !== foot);
  const body = h('div', { class: 'sheet-body' }, ...middle);
  const top = head.length ? h('div', { class: 'sheet-head' }, ...head) : null;
  box.replaceChildren(...[top, body, foot].filter(Boolean));
}
