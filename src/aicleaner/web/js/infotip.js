// f183: an (i) beside a control, holding what it means. Shown on hover and on keyboard focus; a click or a tap toggles
// it (a touch screen has no hover); Escape, or a click elsewhere, hides it. The text is the tooltip element itself, so
// a caller that fills it (the Session page's Asks explanation) keeps doing so.
import { h } from './dom.js';

let count = 0;

export function infoTip(content, { label = 'More information' } = {}) {
  count += 1;
  const id = `info-tip-${count}`;
  const tip = h('span', { id, role: 'tooltip', class: 'info-tip-text' }, content);
  const button = h('button', { type: 'button', class: 'info-tip-button', 'aria-label': label,
    'aria-describedby': id, 'aria-expanded': 'false' }, 'i');
  const wrap = h('span', { class: 'info-tip' }, button, tip);
  const set = (on) => {
    wrap.classList.toggle('open', on);
    button.setAttribute('aria-expanded', String(on));
  };
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    set(!wrap.classList.contains('open'));
  });
  button.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && wrap.classList.contains('open')) { event.stopPropagation(); set(false); }
  });
  button.addEventListener('blur', () => set(false));
  let mounted = false;
  const outside = (event) => {
    mounted = mounted || wrap.isConnected;
    if (mounted && !wrap.isConnected) { document.removeEventListener('click', outside); return; }   // its page has gone
    if (!wrap.contains(event.target)) set(false);
  };
  document.addEventListener('click', outside);
  return wrap;
}
