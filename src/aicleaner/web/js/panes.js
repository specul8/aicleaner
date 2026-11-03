// Drag bars for the Session page's app layout (feature 51).
//
// Two edges are stored and the third is whatever is left: the file column's width and the
// prompt's height, in pixels, under aicleaner.files.width and aicleaner.prompt.height - the
// same localStorage the display settings use. Three stored sizes would have to agree with each
// other and with a window that gets resized, and they cannot; two fixed edges and one elastic
// middle always add up.
//
// Clamped when applied, not only when dragged: a width saved on a wide monitor must not swallow a
// laptop screen, and the stored value is left alone so the big screen gets its layout back.
// Pointer events rather than mouse events, so a finger drags the prompt edge on a phone too.
// Sizes go in as CSS variables through the CSSOM: the page's CSP forbids inline styles.

function readPx(key) {
  try { return Number(localStorage.getItem(key)) || 0; } catch (e) { return 0; }
}

function writePx(key, value) {
  try { localStorage.setItem(key, String(Math.round(value))); } catch (e) { /* the drag still works */ }
}

// { key, variable, min, maxShare, axis: 'x' | 'y', invert }: `maxShare` of the viewport is the
// most a stored value may take; `invert` for an edge whose size grows as the pointer moves back.
export function pane(host, spec) {
  const limit = () => (spec.axis === 'x' ? window.innerWidth : window.innerHeight) * spec.maxShare;
  const clamp = (v) => Math.max(spec.min, Math.min(v, limit()));
  const apply = (v) => { if (v) host.style.setProperty(spec.variable, `${clamp(v)}px`); };
  apply(readPx(spec.key));
  window.addEventListener('resize', () => apply(readPx(spec.key)));
  return {
    // Wire one drag bar to this edge. `measure()` gives the current size when a drag starts.
    bar(bar, measure) {
      bar.addEventListener('pointerdown', (down) => {
        down.preventDefault();
        bar.setPointerCapture(down.pointerId);
        const start = spec.axis === 'x' ? down.clientX : down.clientY;
        const from = measure();
        const move = (event) => {
          const at = spec.axis === 'x' ? event.clientX : event.clientY;
          const size = clamp(from + (spec.invert ? start - at : at - start));
          host.style.setProperty(spec.variable, `${size}px`);
          writePx(spec.key, size);
        };
        const stop = () => {
          bar.removeEventListener('pointermove', move);
          bar.removeEventListener('pointerup', stop);
          bar.removeEventListener('pointercancel', stop);
        };
        bar.addEventListener('pointermove', move);
        bar.addEventListener('pointerup', stop);
        bar.addEventListener('pointercancel', stop);
      });
    },
  };
}
