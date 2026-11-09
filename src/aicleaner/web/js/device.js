// Which device this browser is on (feature 168, Brad 2026-11-08). A web page cannot read a computer's name, so it asks
// once, on the first page opened through remote access: one of the Remote desktops entries, or another device typed in.
// The answer stays in this browser only. It names the device in the note sent with each prompt ("The user is accessing
// the AI Cleaner app remotely from <device>") and greys out this device's own entry in the menu.

import { h } from './dom.js';
import { arrangeSheet } from './sheet.js';

const KEY = 'aicleaner.device';                     // {"name": ..., "url": ...}
const NO_ASK = 'aicleaner.deviceNoAsk';             // '1': "Don't ask me again" was ticked
const NAME = /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$/;  // as the server checks it (config.device_name)

function kept() {
  try { return JSON.parse(localStorage.getItem(KEY) || 'null') || null; } catch (e) { return null; }
}

export function deviceName() {
  const answer = kept();
  return answer && typeof answer.name === 'string' ? answer.name : '';
}

export function deviceUrl() {
  const answer = kept();
  return answer && typeof answer.url === 'string' ? answer.url : '';
}

function keep(answer, noAsk) {
  try {
    if (answer) localStorage.setItem(KEY, JSON.stringify(answer));
    if (noAsk) localStorage.setItem(NO_ASK, '1');
  } catch (e) { /* a browser that keeps nothing asks again next time */ }
  window.dispatchEvent(new CustomEvent('aicleaner:device'));
}

export function forgetDevice() {
  try { localStorage.removeItem(KEY); localStorage.removeItem(NO_ASK); } catch (e) { /* nothing kept */ }
  window.dispatchEvent(new CustomEvent('aicleaner:device'));
}

function noAskSet() {
  try { return localStorage.getItem(NO_ASK) === '1'; } catch (e) { return false; }
}

export function shouldAsk() {
  return !deviceName() && !noAskSet() && !document.getElementById('device-ask');
}

// The sheet. Resolves with the answer kept, or null for Not now.
export function askDevice(hosts) {
  return new Promise((resolve) => {
    const group = 'device-choice';
    const choices = (hosts || []).map((host, i) => h('label', { class: 'device-choice' },
      h('input', { type: 'radio', name: group, value: String(i), checked: i === 0 ? true : null }), ' ', host.name,
      host.device && host.device.toLowerCase() !== host.name.toLowerCase()
        ? h('span', { class: 'hint' }, ` (${host.device})`) : null));
    const other = h('input', { type: 'radio', name: group, value: 'other', checked: hosts && hosts.length ? null : true });
    const typed = h('input', { type: 'text', class: 'device-typed', maxlength: '63', 'aria-label': 'Device name',
      placeholder: 'e.g. TABLET-1234ABCD' });
    typed.addEventListener('input', () => { other.checked = true; });
    const noAsk = h('input', { type: 'checkbox', class: 'device-no-ask' });
    const save = h('button', { type: 'button', class: 'primary device-save' }, 'Save');
    const later = h('button', { type: 'button', class: 'device-later' }, 'Not now');
    const problem = h('p', { class: 'hint device-problem', role: 'alert' });
    const dialog = h('dialog', { id: 'device-ask', class: 'sheet device-ask', 'aria-label': 'Which device is this?' },
      h('h2', {}, 'Which device is this?'),
      h('p', {}, 'Tells Claude which device you are on, and greys out this device\'s RDP entry. Kept in this browser only.'),
      h('div', { class: 'device-choices' }, ...choices,
        h('label', { class: 'device-choice' }, other, ' Another device: ', typed)),
      h('p', { class: 'hint' }, 'Where to find its name - Windows: Settings > System > About > Device name. iPad or iPhone: '
        + 'Settings > General > About > Name. Android: Settings > About phone.'),
      h('label', { class: 'device-no-ask-row' }, noAsk, ' Don\'t ask me again'),
      problem,
      h('div', { class: 'fb-footer' }, later, save));
    document.body.append(dialog);
    arrangeSheet(dialog, { cancel: later, dirty: () => false });
    const done = (answer) => {
      if (dialog.open) dialog.close();
      dialog.remove();
      resolve(answer);
    };
    save.addEventListener('click', () => {
      const picked = dialog.querySelector(`input[name=${group}]:checked`);
      let answer;
      if (picked && picked.value !== 'other') {
        const host = hosts[Number(picked.value)];
        answer = { name: host.device || host.name, url: host.url };
      } else {
        const name = typed.value.trim();
        if (!NAME.test(name)) {
          problem.textContent = 'A device name is 1 to 63 letters, digits, spaces, dots, underscores or hyphens.';
          typed.focus();
          return;
        }
        answer = { name, url: '' };
      }
      keep(answer, noAsk.checked);
      done(answer);
    });
    later.addEventListener('click', () => { keep(null, noAsk.checked); done(null); });
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); keep(null, noAsk.checked); done(null); });
    dialog.showModal();
    save.focus();
  });
}
