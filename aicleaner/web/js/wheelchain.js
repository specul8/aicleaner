// The mouse wheel hands over from a scrolling box to the page (D100).
//
// The browser already chains a FRESH wheel spin that starts at a box's edge on to the page, but
// Chromium latches one continuous spin to the box it began in: reach the bottom of the transcript
// mid-spin and the rest of the spin goes nowhere. Here, once the box cannot move any further in the
// spin's direction, the event is cancelled and the page is scrolled by the same amount instead.
//
// Wheel only, by construction: touch drags raise no wheel events, so they keep scrolling the box
// alone, as the user asked. Ctrl+wheel is zoom and is never taken.

const LINE_PX = 16;

function pixels(event) {
  if (event.deltaMode === 1) return event.deltaY * LINE_PX;                 // lines
  if (event.deltaMode === 2) return event.deltaY * window.innerHeight;      // pages
  return event.deltaY;
}

function atEdge(box, down) {
  if (down) return box.scrollTop + box.clientHeight >= box.scrollHeight - 1;
  return box.scrollTop <= 0;
}

// A box inside this one that can still scroll in the spin's direction - a long thinking block, a code block - between
// the pointer and the box. Its wheel is its own: the transcript sitting at its bottom used to take the wheel from it
// and hand it to the page, so the thinking box could not be scrolled down until the transcript moved up a little
// (Brad, 2026-10-27).
function innerCanScroll(target, box, down) {
  for (let el = target instanceof Element ? target : null; el && el !== box; el = el.parentElement) {
    if (el.scrollHeight <= el.clientHeight + 1) continue;
    const overflow = getComputedStyle(el).overflowY;
    if (overflow !== 'auto' && overflow !== 'scroll') continue;
    if (!atEdge(el, down)) return true;
  }
  return false;
}

export function handOverWheel(box) {
  box.addEventListener('wheel', (event) => {
    if (event.ctrlKey || !event.deltaY) return;
    const down = event.deltaY > 0;
    if (innerCanScroll(event.target, box, down)) return;  // an inner box still moves: it takes the wheel
    if (!atEdge(box, down)) return;                      // the box still moves: the browser does it
    const page = document.scrollingElement || document.documentElement;
    event.preventDefault();
    page.scrollTop += pixels(event);
  }, { passive: false });
}
