// From a Markdown preview to the same place in its source (feature 92): double-click a word in the
// preview and the tab switches to the source with that word selected, ready to read in context or
// type over. Decided by the user 2026-10-08: the first time, ask - jump, or keep double-click for
// selecting text - with "Don't ask me again"; afterwards Settings > File editor
// (`editor.preview_dblclick`: ask, source, off), saved on the server so every device follows it.
//
// The renderer tags each block with the source lines it came from (data-line-from, data-line-to);
// the word is looked for in those lines, as the same occurrence it was in the rendered block.

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

// What was double-clicked: the word, its block's source lines, and which occurrence it was.
function clicked(event) {
  const block = event.target.closest && event.target.closest('[data-line-from]');
  if (!block) return null;
  const selection = window.getSelection();
  const word = selection && selection.rangeCount ? selection.toString().trim() : '';
  let nth = 0;
  if (word && !/\s/.test(word)) {
    const before = document.createRange();
    before.setStart(block, 0);
    const range = selection.getRangeAt(0);
    before.setEnd(range.startContainer, range.startOffset);
    nth = occurrences(before.toString(), word);
  }
  return { from: Number(block.dataset.lineFrom), to: Number(block.dataset.lineTo),
    word: word && !/\s/.test(word) ? word : '', nth };
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
    h('p', {}, 'Jump to the source on double-click? It opens the source view with that word selected, '
      + 'ready to read in context or edit.'),
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

// `toSource(start, end)`: switch the tab to its source and select that range.
export function attachPreviewJump(body, source, toSource) {
  body.addEventListener('dblclick', async (event) => {
    if (event.target.closest('a, button, input')) return;
    const what = clicked(event);
    if (!what) return;
    const go = () => { const [start, end] = findInSource(source(), what.from, what.to, what.word, what.nth); toSource(start, end); };
    const mode = await setting();
    if (mode === 'off') return;
    if (mode === 'source') { go(); return; }
    ask({ x: event.clientX, y: event.clientY }, go);
  });
}
