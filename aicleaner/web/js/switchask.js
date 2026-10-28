// Switching a busy session to another conversation (feedback #6, Brad 2026-10-28): wrap up first, switch now, or cancel.
// Resolves 'finish', 'now', or null for Cancel (also Escape and a click behind).
import { h } from './dom.js';
import { arrangeSheet } from './sheet.js';

export function askSwitch(name, why) {
  return new Promise((resolve) => {
    const cancel = h('button', { type: 'button', class: 'switch-cancel' }, 'Cancel');
    const now = h('button', { type: 'button', class: 'switch-now danger' }, 'Switch now');
    const finish = h('button', { type: 'button', class: 'primary switch-finish' }, 'Wrap up, then switch');
    const dialog = h('dialog', { id: 'switch-ask', class: 'sheet switch-ask', 'aria-label': 'Switch conversation' },
      h('h2', {}, `${name} is busy`),
      h('p', {}, why),
      h('ul', {},
        h('li', {}, h('strong', {}, 'Wrap up, then switch: '),
          'Claude is asked to finish at a safe point and say where things are; the switch happens once the session is idle, background tasks included.'),
        h('li', {}, h('strong', {}, 'Switch now: '),
          'the turn and any background tasks are ended at once, and the other conversation starts.')),
      h('div', { class: 'fb-footer' }, cancel, now, finish));
    document.body.append(dialog);
    arrangeSheet(dialog, { cancel, dirty: () => false });
    const done = (choice) => {
      if (dialog.open) dialog.close();
      dialog.remove();
      resolve(choice);
    };
    cancel.addEventListener('click', () => done(null));
    now.addEventListener('click', () => done('now'));
    finish.addEventListener('click', () => done('finish'));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); done(null); });
    dialog.showModal();
    finish.focus();
  });
}
