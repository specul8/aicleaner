// f183 (Brad, 2026-11-16 07:57; spec specs/2026-11-16-f183-session-panels.md): "Conversation actions" opens a panel
// that slides in from the right edge of the window toward the middle, over the page - nothing under it moves, and it is
// as tall as the window, not the card its button sits in. It is a child of <body>, position fixed, so no container's
// height or overflow can hold it in.
//
// The actions themselves are the Session page's existing buttons, each given a one-line description: a click on one
// closes the panel first (focus back on the button that opened it), then its own handler runs as before.
import { h } from './dom.js';

const FOCUSABLE = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

// `opener`: the button that opens it. `groups`: [[{ button, why }, ...], ...] - shown in that order, a line between
// groups. Returns { open, close, isOpen, element }.
export function conversationDrawer(opener, groups) {
  document.getElementById('conv-drawer')?.remove();          // a previous visit's
  document.getElementById('conv-drawer-backdrop')?.remove();
  const title = h('h2', { id: 'conv-drawer-title' }, 'Conversation actions');
  const close = h('button', { type: 'button', class: 'conv-drawer-close', 'aria-label': 'Close conversation actions',
    title: 'Close (Escape)' }, '×');
  const sections = groups.map((items, g) => h('ul', { class: 'conv-drawer-group', 'aria-label': `Group ${g + 1}` },
    ...items.map(({ button, why }) => {
      const id = `${button.id}-why`;
      button.classList.add('conv-drawer-item');
      button.setAttribute('aria-describedby', id);
      const name = button.textContent;
      button.replaceChildren(h('span', { class: 'conv-drawer-name' }, name),
        h('small', { id, class: 'conv-drawer-why' }, why));
      return h('li', {}, button);
    })));
  const panel = h('aside', { id: 'conv-drawer', class: 'conv-drawer', role: 'dialog', 'aria-modal': 'true',
    'aria-labelledby': 'conv-drawer-title', hidden: true },
  h('div', { class: 'conv-drawer-head' }, title, close), ...sections);
  const backdrop = h('div', { id: 'conv-drawer-backdrop', class: 'conv-drawer-backdrop', hidden: true });
  document.body.append(backdrop, panel);
  opener.setAttribute('aria-haspopup', 'dialog');
  opener.setAttribute('aria-controls', 'conv-drawer');
  opener.setAttribute('aria-expanded', 'false');

  let shown = false;
  let timer = null;
  const isOpen = () => shown;
  const open = () => {
    if (shown) return;
    shown = true;
    clearTimeout(timer);
    panel.hidden = false;
    backdrop.hidden = false;
    void panel.offsetWidth;            // laid out off-screen first, so the slide plays
    panel.classList.add('open');
    backdrop.classList.add('open');
    opener.setAttribute('aria-expanded', 'true');
    const first = panel.querySelector('.conv-drawer-item:not([disabled])') || close;
    first.focus();
  };
  const shut = ({ refocus = true } = {}) => {
    if (!shown) return;
    shown = false;
    panel.classList.remove('open');
    backdrop.classList.remove('open');
    opener.setAttribute('aria-expanded', 'false');
    // Hidden once it has slid away; at once when nothing animates (reduced motion: no transitionend comes).
    const done = () => { if (!shown) { panel.hidden = true; backdrop.hidden = true; } };
    const moving = parseFloat(getComputedStyle(panel).transitionDuration || '0') > 0;
    if (moving) timer = setTimeout(done, 260); else done();
    if (refocus && opener.isConnected) opener.focus();
  };

  opener.addEventListener('click', () => (shown ? shut() : open()));
  close.addEventListener('click', () => shut());
  backdrop.addEventListener('click', () => shut());
  // An action closes the panel before its own handler runs (capture, on the way down to the button).
  panel.addEventListener('click', (event) => {
    if (event.target.closest('.conv-drawer-item')) shut();
  }, true);
  panel.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      shut();
      return;
    }
    const items = [...panel.querySelectorAll(FOCUSABLE)].filter((el) => !el.hidden);
    if (!items.length) return;
    const at = items.indexOf(document.activeElement);
    if (event.key === 'Tab') {                                 // focus stays inside while it is open
      const next = event.shiftKey ? (at <= 0 ? items.length - 1 : at - 1) : (at === items.length - 1 ? 0 : at + 1);
      event.preventDefault();
      items[next].focus();
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      items[(at + step + items.length) % items.length].focus();
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      items[event.key === 'Home' ? 0 : items.length - 1].focus();
    }
  });
  // Leaving the page closes it (it lives on <body>, outside the page that is replaced).
  const left = () => {
    if (!panel.isConnected) { window.removeEventListener('hashchange', left); return; }
    shut({ refocus: false });
  };
  window.addEventListener('hashchange', left);
  return { open, close: shut, isOpen, element: panel };
}
