// Editing help in the file editor (feature 80; spec section 5): Tab and Shift+Tab indent the line or
// the selected lines, Enter keeps the indentation (one more after an opening bracket, or after ":" in
// Python and YAML), and brackets and quotes close themselves. Every change goes through insertText,
// so Ctrl+Z takes it back like typing.

import { insertText } from './complete.js';

const PAIRS = { '(': ')', '[': ']', '{': '}', '"': '"', "'": "'" };
const CLOSERS = new Set([')', ']', '}', '"', "'"]);
const COLON_BLOCKS = new Set(['python', 'yaml']);

// A file already indented with tabs keeps tabs (the user, 2026-10-07: as section 5).
function unitFor(box, unit) {
  return /^\t/m.test(box.value) && !/^ {2,}\S/m.test(box.value) ? '\t' : unit;
}

function lineBounds(text, start, end) {
  const first = text.lastIndexOf('\n', start - 1) + 1;
  let last = text.indexOf('\n', end > start && text[end - 1] === '\n' ? end - 1 : end);
  if (last < 0) last = text.length;
  return [first, last];
}

function indentLines(box, unit, outdent) {
  const text = box.value;
  const [first, last] = lineBounds(text, box.selectionStart, box.selectionEnd);
  const block = text.slice(first, last);
  const changed = block.split('\n').map((line) => {
    if (!outdent) return line ? unit + line : line;
    if (line.startsWith(unit)) return line.slice(unit.length);
    return line.replace(/^(\t| {1,4})/, '');
  }).join('\n');
  insertText(box, first, last, changed);
  box.setSelectionRange(first, first + changed.length);
}

function newline(box, unit, language) {
  const text = box.value;
  const caret = box.selectionStart;
  const lineStart = text.lastIndexOf('\n', caret - 1) + 1;
  const before = text.slice(lineStart, caret);
  const indent = (/^[ \t]*/.exec(before) || [''])[0];
  const opens = /[({[]\s*$/.test(before) || (COLON_BLOCKS.has(language) && /:\s*$/.test(before));
  const next = text[box.selectionEnd];
  if (opens && next && PAIRS[before.trimEnd().slice(-1)] === next) {
    // Between a pair: the caret's line indented, the closer on a line of its own.
    insertText(box, caret, box.selectionEnd, `\n${indent}${unit}\n${indent}`);
    box.setSelectionRange(caret + 1 + indent.length + unit.length, caret + 1 + indent.length + unit.length);
    return;
  }
  insertText(box, caret, box.selectionEnd, `\n${indent}${opens ? unit : ''}`);
}

export function attachKeys(box, language, completion) {
  box.addEventListener('keydown', (event) => {
    if (event.defaultPrevented || event.isComposing) return;
    if (completion && completion.keydown(event)) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const unit = unitFor(box, completion ? completion.indentUnit : '  ');
    const text = box.value;
    const start = box.selectionStart;
    const end = box.selectionEnd;
    if (event.key === 'Tab') {
      event.preventDefault();
      if (event.shiftKey || text.slice(start, end).includes('\n')) indentLines(box, unit, event.shiftKey);
      else insertText(box, start, end, unit);
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      newline(box, unit, language);
      return;
    }
    if (event.key === 'Backspace' && start === end && PAIRS[text[start - 1]] && PAIRS[text[start - 1]] === text[start]) {
      event.preventDefault();
      insertText(box, start - 1, start + 1, '');
      return;
    }
    // A closer that is already there is typed over rather than doubled.
    if (CLOSERS.has(event.key) && start === end && text[start] === event.key) {
      const quote = event.key === '"' || event.key === "'";
      if (!quote || text[start - 1] !== '\\') {
        event.preventDefault();
        box.setSelectionRange(start + 1, start + 1);
        return;
      }
    }
    if (PAIRS[event.key] && start === end) {
      const quote = event.key === '"' || event.key === "'";
      const after = text[start] || '';
      const prev = text[start - 1] || '';
      // Not before a word, and a quote not after one (an apostrophe in prose, a closing quote).
      if (/\w/.test(after) || (quote && /\w/.test(prev))) return;
      event.preventDefault();
      insertText(box, start, end, event.key + PAIRS[event.key]);
      box.setSelectionRange(start + 1, start + 1);
    }
  });
}
