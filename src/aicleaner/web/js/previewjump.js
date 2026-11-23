// From a Markdown preview to the same place in its source (feature 92): double-click a word in the
// preview and the tab switches to the source with that word selected, ready to read in context or
// type over. Decided by the user 2026-10-08: the first time, ask - jump, or keep double-click for
// selecting text - with "Don't ask me again"; afterwards Settings > File editor
// (`editor.preview_dblclick`: ask, source, off), saved on the server so every device follows it.
//
// The renderer tags each block with the source lines it came from (data-line-from, data-line-to);
// the word is looked for in those lines, as the same occurrence it was in the rendered block.
// Table rows and list items also carry their own line (data-line): a double-click on an empty spot
// - a blank table cell, an empty list item, beside a heading - puts the caret at the same place in
// the source (D251).

import { getJSON, postJSON, say, sayError } from './api.js';
import { h } from './dom.js';

let choice = null;            // the setting as last read; null until the first double-click

async function setting() {
  if (choice === null) {
    try { choice = ((await getJSON('/api/config')).settings || {})['editor.preview_dblclick'] || 'ask'; }
    catch (error) { choice = 'ask'; }
  }
  return choice;
}

async function remember(value) {
  try {
    await postJSON('/api/config', { settings: { 'editor.preview_dblclick': value } });
    choice = value;
  } catch (error) { sayError(error); }
}

// Settings changed it: forget what was read, so the next double-click reads it again.
export function forgetPreviewChoice() { choice = null; }

function occurrences(text, word) {
  let count = 0;
  for (let at = text.indexOf(word); at >= 0; at = text.indexOf(word, at + word.length)) count += 1;
  return count;
}

// [start, end] in `source` of the `nth` (0-based) `word` within lines `from`..`to`; the block's
// first occurrence if that one is not there (the source spells it differently - a link's text,
// an escaped character); the block's start when the word is not there at all.
export function findInSource(source, from, to, word, nth) {
  const lines = source.split('\n');
  let start = 0;
  for (let i = 0; i < from && i < lines.length; i += 1) start += lines[i].length + 1;
  const block = lines.slice(from, to + 1).join('\n');
  if (!word) return [start, start];
  const found = [];
  for (let i = block.indexOf(word); i >= 0; i = block.indexOf(word, i + word.length)) found.push(i);
  if (!found.length) return [start, start];
  const at = found[nth] !== undefined ? found[nth] : found[0];
  return [start + at, start + at + word.length];
}

// Where a table row's source line holds cell `n`: [from, to) between its pipes. The pipes are
// counted as the renderer counts them (markdown.js cells()): an escaped \| is text, and the leading
// and trailing pipes are optional - without a leading one the first cell starts at the indent,
// without a trailing one the last cell runs to the end of the text.
export function cellSpan(line, n) {
  const lead = line.length - line.trimStart().length;
  const end = line.trimEnd().length;
  const pipes = [];
  for (let i = lead; i < end; i += 1) if (line[i] === '|' && line[i - 1] !== '\\') pipes.push(i);
  const bounds = pipes[0] === lead ? pipes.slice() : [lead - 1, ...pipes];
  if (bounds[bounds.length - 1] !== end - 1 || bounds.length < 2) bounds.push(end);
  const at = Math.max(0, Math.min(n, bounds.length - 2));
  return [bounds[at] + 1, bounds[at + 1]];
}

// The caret's offset in `source` for a spot in the preview with no word under the pointer (D251).
// `spot.kind`: 'cell' - in cell `spot.cell` of the table row on `spot.line`: between its pipes
// (after one space of padding) when empty, after its text when not; 'item' - an empty list item:
// after its marker; 'end' - the end of that line; anything else - the start of that line.
export function caretInSource(source, spot) {
  const lines = source.split('\n');
  const n = Math.max(0, Math.min(spot.line, lines.length - 1));
  let start = 0;
  for (let i = 0; i < n; i += 1) start += lines[i].length + 1;
  const line = lines[n] || '';
  if (spot.kind === 'cell') {
    const [from, to] = cellSpan(line, spot.cell || 0);
    const text = line.slice(from, to);
    if (!text.trim()) return start + from + (text.length ? 1 : 0);
    return start + from + text.trimEnd().length;
  }
  if (spot.kind === 'item') {
    const marker = /^(\s*)([-*+]|\d{1,9}[.)])[ \t]?/.exec(line);
    return start + (marker ? marker[0].length : 0);
  }
  if (spot.kind === 'end') return start + line.length;
  return start;
}

// The word double-clicked, if the browser selected one and it is under the pointer: in an empty
// table cell or beside a short line, the browser may select a neighbour's word or nothing at all.
function wordAt(event) {
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount) return null;
  const word = selection.toString().trim();
  if (!word || /\s/.test(word)) return null;
  const range = selection.getRangeAt(0);
  const near = (r) => event.clientX >= r.left - 2 && event.clientX <= r.right + 2
    && event.clientY >= r.top - 2 && event.clientY <= r.bottom + 2;
  return [...range.getClientRects()].some(near) ? { word, range } : null;
}

// What was double-clicked: the word, its block's source lines, and which occurrence it was.
function clicked(event) {
  const block = event.target.closest && event.target.closest('[data-line-from]');
  if (!block) return null;
  const found = wordAt(event);
  if (!found || !block.contains(found.range.startContainer)) return null;
  const before = document.createRange();
  before.setStart(block, 0);
  before.setEnd(found.range.startContainer, found.range.startOffset);
  return { from: Number(block.dataset.lineFrom), to: Number(block.dataset.lineTo),
    word: found.word, nth: occurrences(before.toString(), found.word) };
}

// The element of `items` nearest the point: one the point is in, else the least distance away.
function nearest(items, x, y) {
  let best = null;
  let bestDistance = Infinity;
  for (const item of items) {
    const r = item.getBoundingClientRect();
    const dx = x < r.left ? r.left - x : x > r.right ? x - r.right : 0;
    const dy = y < r.top ? r.top - y : y > r.bottom ? y - r.bottom : 0;
    const distance = dy * 10000 + dx;          // the row (or line) first, then across it
    if (distance < bestDistance) { best = item; bestDistance = distance; }
  }
  return best;
}

// How many line breaks of `el`'s text come before the point - which of a block's source lines the
// pointer is beside (a paragraph keeps its source line breaks in its text).
function linesBefore(el, x, y) {
  let node = null;
  let offset = 0;
  if (document.caretPositionFromPoint) {
    const at = document.caretPositionFromPoint(x, y);
    if (at) { node = at.offsetNode; offset = at.offset; }
  } else if (document.caretRangeFromPoint) {
    const at = document.caretRangeFromPoint(x, y);
    if (at) { node = at.startContainer; offset = at.startOffset; }
  }
  if (!node || !el.contains(node)) return 0;
  const before = document.createRange();
  before.setStart(el, 0);
  before.setEnd(node, offset);
  return (before.toString().match(/\n/g) || []).length;
}

// An item's own text, without the lists nested under it.
function ownText(item) {
  return [...item.childNodes].filter((child) => !(child.nodeType === 1 && /^(UL|OL)$/.test(child.tagName)))
    .map((child) => child.textContent).join('').trim();
}

// A table cell as a spot: the row's source line and the cell's index - the one the pointer is in,
// or the nearest across the row.
function cellSpot(row, target, x, y) {
  const cell = (target.closest && target.closest('td, th')) || nearest(row.cells, x, y);
  return { line: Number(row.dataset.line), kind: 'cell', cell: cell ? cell.cellIndex : 0 };
}

// Where a double-click with no word under it lands (D251): an empty table cell, an empty list
// item, the blank beside a heading or a paragraph's line, or - anywhere else - the nearest block.
function spotAt(event, body) {
  const x = event.clientX;
  const y = event.clientY;
  const target = event.target;
  const el = target.closest && target.closest('[data-line], [data-line-from]');
  if (!el || !body.contains(el)) {
    const block = nearest(body.querySelectorAll('[data-line-from]'), x, y);
    return block ? { line: Number(block.dataset.lineFrom), kind: 'start' } : null;
  }
  if (el.tagName === 'TR') return cellSpot(el, target, x, y);
  if (el.tagName === 'LI') {
    if (!ownText(el)) return { line: Number(el.dataset.line), kind: 'item' };
    return { line: Number(el.dataset.line) + linesBefore(el, x, y), kind: 'end' };
  }
  const from = Number(el.dataset.lineFrom);
  const to = Number(el.dataset.lineTo);
  const rows = el.querySelectorAll('tr[data-line]');
  if (rows.length) { const row = nearest(rows, x, y); return cellSpot(row, row, x, y); }
  if (/^(P|H[1-6])$/.test(el.tagName)) return { line: Math.min(to, from + linesBefore(el, x, y)), kind: 'end' };
  if (el.tagName === 'PRE') return { line: Math.min(to, from + 1 + linesBefore(el, x, y)), kind: 'end' };
  return { line: from, kind: 'start' };
}

// A bubble beside the pointer (the user, 2026-10-08: a panel above the file "took me a few seconds
// to find"). Placed from script, which the page's policy allows, and kept on screen.
function place(card, x, y) {
  const gap = 12;
  const box = card.getBoundingClientRect();
  const left = Math.min(Math.max(8, x + gap), window.innerWidth - box.width - 8);
  const below = y + gap + box.height <= window.innerHeight - 8;
  card.style.left = `${Math.round(left)}px`;
  card.style.top = `${Math.round(below ? y + gap : Math.max(8, y - gap - box.height))}px`;
}

function ask(at, jump) {
  document.querySelector('.preview-jump-ask')?.remove();
  const again = h('input', { type: 'checkbox', class: 'preview-jump-remember' });
  const yes = h('button', { type: 'button', class: 'primary preview-jump-yes' }, 'Yes, jump to the source');
  const no = h('button', { type: 'button', class: 'preview-jump-no' }, 'No, keep double-click for selecting text');
  const card = h('div', { class: 'callout preview-jump-ask floating', role: 'dialog', 'aria-label': 'Double-click in the preview' },
    h('p', {}, 'Jump to the source on double-click? It opens the source view with that word selected '
      + '(or, on an empty spot such as a blank table cell, the cursor there), ready to read in context or edit.'),
    h('div', { class: 'form-row' }, yes, no,
      h('label', { class: 'check' }, again, ' Don\'t ask me again')),
    h('p', { class: 'hint' }, 'You can change this later in Settings > File editor.'));
  yes.addEventListener('click', async () => {
    card.remove();
    if (again.checked) await remember('source');
    jump();
  });
  no.addEventListener('click', async () => {
    card.remove();
    if (again.checked) {
      await remember('off');
      say('Double-click selects text in previews. Settings > File editor changes it.', 'ok');
    }
  });
  // Esc or a click elsewhere closes it, as "not this time".
  const away = (event) => {
    if (event.type === 'keydown' ? event.key === 'Escape' : !card.contains(event.target)) close();
  };
  const close = () => {
    card.remove();
    document.removeEventListener('keydown', away, true);
    document.removeEventListener('mousedown', away, true);
  };
  for (const button of [yes, no]) button.addEventListener('click', close);
  document.body.append(card);
  place(card, at.x, at.y);
  document.addEventListener('keydown', away, true);
  document.addEventListener('mousedown', away, true);
  yes.focus({ preventScroll: true });
}

// `toSource(start, end)`: switch the tab to its source and select that range - or, with start ==
// end, put the caret there (a double-click with no word under it, D251).
export function attachPreviewJump(body, source, toSource) {
  body.addEventListener('dblclick', async (event) => {
    if (event.target.closest('a, button, input')) return;
    const what = clicked(event);
    const where = what ? null : spotAt(event, body);
    if (!what && !where) return;
    const go = what
      ? () => { const [start, end] = findInSource(source(), what.from, what.to, what.word, what.nth); toSource(start, end); }
      : () => { const at = caretInSource(source(), where); toSource(at, at); };
    const mode = await setting();
    if (mode === 'off') return;
    if (mode === 'source') { go(); return; }
    ask({ x: event.clientX, y: event.clientY }, go);
  });
}
