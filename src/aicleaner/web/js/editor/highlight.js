// Syntax colouring (feature 80) without loosening the page's policy against inline styles: spans with
// tok-* classes (editor.css). In the editor they sit in a <pre> exactly behind the textarea, whose
// own text is transparent, so typing, the caret, selection and undo stay the textarea's own.

import { h } from '../dom.js';
import { tokenise } from './tokenise.js';

// Above this the editor and the view stay plain: a re-colour per keystroke would be felt.
export const COLOUR_LIMIT = 2 * 1024 * 1024;

export function colourable(language, text) {
  return Boolean(language) && String(text || '').length <= COLOUR_LIMIT;
}

// The text as coloured spans, into `el` (replacing what it held).
export function colourInto(el, language, text) {
  el.replaceChildren(...tokenise(language, text).map(([kind, piece]) =>
    (kind ? h('span', { class: `tok-${kind}` }, piece) : document.createTextNode(piece))));
}

// The layer behind an editor's textarea. Returns the element to put where the textarea was, and a
// refresh to call after anything changes the text programmatically.
export function layerFor(box, language) {
  const layer = h('pre', { class: 'code-layer', 'aria-hidden': 'true' });
  const stack = h('div', { class: 'code-stack' }, layer, box);
  box.classList.add('coloured');
  let queued = false;
  const paint = () => {
    queued = false;
    if (!colourable(language, box.value)) {
      box.classList.remove('coloured');
      layer.replaceChildren();
      return;
    }
    box.classList.add('coloured');
    // A trailing newline in a <pre> collapses; the space keeps the last line's height.
    colourInto(layer, language, `${box.value}\n `);
    sync();
  };
  const sync = () => {
    layer.scrollTop = box.scrollTop;
    layer.scrollLeft = box.scrollLeft;
  };
  const refresh = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(paint);
  };
  box.addEventListener('input', refresh);
  box.addEventListener('scroll', sync);
  paint();
  return { stack, refresh, sync };
}
