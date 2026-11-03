// Which visual row of a textarea the caret is on - first, last, or in between (the user, 2026-10-01).
//
// Prompt history walked on Up/Down whenever no line break stood between the caret and the edge of
// the text. A long paragraph wraps over several rows with no line break in it, so Up anywhere in it
// recalled the previous prompt instead of moving up a row. VS Code (and this now) only walks the
// history from the first row (Up) or the last row (Down) as the text is actually laid out.
//
// The layout is measured, not guessed: a hidden copy of the box, same text width, font and wrapping,
// holds the whole text with a marker at the caret, and the marker says how far down the caret sits.

const COPIED = ['boxSizing', 'width', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
  'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth', 'fontFamily', 'fontSize',
  'fontWeight', 'fontStyle', 'letterSpacing', 'lineHeight', 'textTransform', 'wordSpacing', 'tabSize',
  'textIndent', 'whiteSpace', 'overflowWrap', 'wordBreak'];

let mirror = null;

function caretTop(box, position) {
  return measured(box, position).offsetTop;
}

// Where in the window the given position of the box's text sits, as laid out: the slash list opens
// there (the user, 2026-10-14) rather than under the box, which is short when the box is.
export function caretRect(box, position) {
  const marker = measured(box, position);
  const style = getComputedStyle(box);
  const outer = box.getBoundingClientRect();
  const left = outer.left + parseFloat(style.borderLeftWidth) + marker.offsetLeft - box.scrollLeft;
  const top = outer.top + parseFloat(style.borderTopWidth) + marker.offsetTop - box.scrollTop;
  return { left, top, bottom: top + marker.offsetHeight };
}

// The hidden copy holding the text up to `position`, and the marker at its end.
function measured(box, position) {
  if (!mirror) {
    mirror = document.createElement('div');
    mirror.setAttribute('aria-hidden', 'true');
    mirror.style.position = 'absolute';
    mirror.style.visibility = 'hidden';
    mirror.style.top = '0';
    mirror.style.left = '-9999px';
    mirror.style.overflow = 'hidden';
    document.body.append(mirror);
  }
  const style = getComputedStyle(box);
  for (const name of COPIED) mirror.style[name] = style[name];
  // The room the text really has: clientWidth leaves out the borders and the scroll bar (Brad, 2026-10-29: once the
  // box scrolled, the copy was a scroll bar wider, kept the wrapped word on the first row, and Up on it recalled the
  // previous prompt instead of moving up). Padding stays, so the copy takes the width as border-box with no border.
  // Not clientWidth itself: it is rounded to a whole pixel, and a box 660.6 px wide made a copy 661 px wide, which
  // kept a word on the first row that the box had wrapped to the second (Brad, 2026-10-29, no scroll bar showing).
  // The exact width less the borders, less the scroll bar (whole pixels, so offsetWidth - clientWidth is exact).
  const borders = parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth);
  const bar = Math.max(0, box.offsetWidth - box.clientWidth - Math.round(borders));
  mirror.style.boxSizing = 'border-box';
  mirror.style.width = `${box.getBoundingClientRect().width - borders - bar}px`;
  for (const side of ['Top', 'Right', 'Bottom', 'Left']) mirror.style[`border${side}Width`] = '0';
  // A textarea always wraps; the copy has to wrap the same way, whatever the stylesheet says.
  mirror.style.whiteSpace = 'pre-wrap';
  mirror.style.overflowWrap = 'break-word';
  // The whole text, the marker at the caret (Brad, 2026-10-29): with only the text before it, the caret inside a word
  // the box wraps to the next row - the start of the word fitting where the whole word does not - measured on the row
  // above, and Up there recalled the previous prompt. A word joiner: no width, and no break where the caret is.
  mirror.textContent = box.value.slice(0, position);
  const marker = document.createElement('span');
  marker.textContent = '⁠';
  mirror.append(marker, document.createTextNode(box.value.slice(position)));
  return marker;
}

// True when the caret (the start of a selection) is on the first row as laid out.
export function onFirstRow(box) {
  return caretTop(box, box.selectionStart) === caretTop(box, 0);
}

// True when the caret (the end of a selection) is on the last row as laid out.
export function onLastRow(box) {
  return caretTop(box, box.selectionEnd) === caretTop(box, box.value.length);
}
