// Down to the Session page's chat band - Start, a session opened from the menu or a notice (`/chat`).
//
// The scroll is smooth, so it runs for a moment, and content above the band can arrive during it (the Start Work /
// Start Personal panels load after the page is drawn). Such content must aim the scroll again, not nudge the page:
// a scrollBy cancelled the smooth scroll and left the page partway (2026-10-28).

import { smoothIntoView } from './motion.js';

const SCROLL_MS = 1500;     // how long a smooth scroll is taken to be on its way

export function scrollToChat() {
  const band = document.getElementById('session-band');
  if (!band) return;
  band.dataset.scrollUntil = String(Date.now() + SCROLL_MS);
  smoothIntoView(band, 'start');
}

// Around a change of height above the band: on its way there, the scroll is aimed again; otherwise the band is
// kept where it was on the screen.
export function keepChatInPlace(change) {
  const band = document.getElementById('session-band');
  const before = band ? band.getBoundingClientRect().top : null;
  change();
  if (!band || before === null) return;
  if (Number(band.dataset.scrollUntil || 0) > Date.now()) {
    smoothIntoView(band, 'start');
    return;
  }
  // At the very top the reader is looking at the top of the page, not the chat: content arriving above the band
  // pushes it down rather than scrolling the page (f183 - with the shorter session card the band is on screen at once,
  // and the Start Work panels arriving scrolled the page 116 px on arrival).
  if (before < window.innerHeight && window.scrollY > 0) {
    const moved = band.getBoundingClientRect().top - before;
    if (moved) window.scrollBy(0, moved);
  }
}
