// Down to the Session page's chat band - "Start chatting", Start, a session opened from the menu.
//
// The scroll is smooth, so it runs for a moment, and content above the band can arrive during it (the Start Work /
// Start Personal panels load after the page is drawn). Such content must aim the scroll again, not nudge the page:
// a scrollBy cancelled the smooth scroll and left the page partway (2026-10-28).

const SCROLL_MS = 1500;     // how long a smooth scroll is taken to be on its way

export function scrollToChat() {
  const band = document.getElementById('session-band');
  if (!band) return;
  band.dataset.scrollUntil = String(Date.now() + SCROLL_MS);
  band.scrollIntoView({ block: 'start', behavior: 'smooth' });
}

// Around a change of height above the band: on its way there, the scroll is aimed again; otherwise the band is
// kept where it was on the screen.
export function keepChatInPlace(change) {
  const band = document.getElementById('session-band');
  const before = band ? band.getBoundingClientRect().top : null;
  change();
  if (!band || before === null) return;
  if (Number(band.dataset.scrollUntil || 0) > Date.now()) {
    band.scrollIntoView({ block: 'start', behavior: 'smooth' });
    return;
  }
  if (before < window.innerHeight) {
    const moved = band.getBoundingClientRect().top - before;
    if (moved) window.scrollBy(0, moved);
  }
}
