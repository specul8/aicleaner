// Animations even where the device has its animation effects off (feature 163, Brad, 2026-11-05).
//
// Windows "Animation effects" off (and the same switch on other systems) reaches the page as prefers-reduced-motion,
// and every rule in app.css that follows it - the pulse on the turn landed on, the light on a View half, the running
// tool dot, the highlight on a settings card - did nothing. With Settings > Accessibility > Animations on (the
// default), <html> carries `motion-always`, the CSS rules that follow the device are scoped out by it, and smooth
// scrolling is done here rather than left to a browser that would jump.
//
// The choice is AI Cleaner's (`ui.animations_override`), so every device gets it; this browser keeps a copy so
// prefs.js can put the class on before the first paint.

import { getJSON } from './api.js';

const STORE = 'aicleaner.motion';
const REDUCED = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

export function applyMotion(on) {
  document.documentElement.classList.toggle('motion-always', Boolean(on));
  try { localStorage.setItem(STORE, on ? '1' : '0'); } catch (e) { /* storage unavailable: this page only */ }
}

export function setupMotion() {
  getJSON('/api/config').then((config) => {
    const settings = config.settings || {};
    if ('ui.animations_override' in settings) applyMotion(settings['ui.animations_override']);
  }).catch(() => {});
  window.addEventListener('aicleaner:settings', (event) => {
    if (event.detail && 'ui.animations_override' in event.detail) applyMotion(event.detail['ui.animations_override']);
  });
}

// True when the device asks for less motion and AI Cleaner animates anyway.
function overriding() {
  return Boolean(REDUCED && REDUCED.matches) && document.documentElement.classList.contains('motion-always');
}

function scroller(el) {
  for (let node = el.parentElement; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight) return node;
  }
  return document.scrollingElement || document.documentElement;
}

const SCROLL_MS = 300;
const running = new WeakMap();          // a newer scroll of the same container takes over from the one on its way

// scrollIntoView with behavior 'smooth' - which a browser may turn into a jump when the device asks for less motion.
// When overriding, the nearest scrolling container is moved over 300 ms instead; a second call while one is on its way
// aims it again (chatscroll re-aims as content arrives above the band).
export function smoothIntoView(el, block = 'start') {
  if (!el) return;
  if (!overriding()) { el.scrollIntoView({ block, behavior: 'smooth' }); return; }
  const box = scroller(el);
  const page = box === document.scrollingElement || box === document.documentElement;
  const viewTop = page ? 0 : box.getBoundingClientRect().top;
  const viewHeight = page ? window.innerHeight : box.clientHeight;
  const rect = el.getBoundingClientRect();
  const offset = block === 'center' ? (viewHeight - rect.height) / 2 : 0;
  const from = box.scrollTop;
  const to = Math.max(0, Math.min(box.scrollHeight - viewHeight, from + rect.top - viewTop - offset));
  const started = performance.now();
  const mine = {};
  running.set(box, mine);
  const step = (now) => {
    if (running.get(box) !== mine) return;
    const t = Math.min(1, (now - started) / SCROLL_MS);
    box.scrollTop = from + (to - from) * (1 - (1 - t) ** 3);       // ease out
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
