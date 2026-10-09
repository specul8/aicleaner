// The editor's suggestion list (feature 80; spec section 4): opens as you type (two characters that
// match something) and on Ctrl+Space; Up/Down choose, Tab or Enter accept, Esc closes. Sources, in
// rank order: the language's snippets, its keywords and built-ins (PowerShell's from this machine,
// compiled by the server), and every word already in the file. Insertions go through
// execCommand('insertText') so Ctrl+Z undoes them like typing.

import { h } from '../dom.js';
import { INDENT, SNIPPETS, WORDS } from './languages.js';

const MAX_SHOWN = 50;
// What counts as part of the word being completed, per language: PowerShell's $variables,
// Verb-Noun and -Parameters; Terraform's var.name; the rest plain identifiers (and keys).
const WORD_CHAR = { powershell: /[\w$:-]/, terraform: /[\w.-]/, python: /\w/, json: /[\w.$-]/, yaml: /[\w.$-]/, markdown: /\w/ };
const FILE_WORDS = { powershell: /\$?[A-Za-z_][\w-]*(?::\w+)?/g, terraform: /[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*/g };
const COMMENT_MARK = { powershell: '#', python: '#', yaml: '#', terraform: '#' };

// ---- PowerShell, from this machine ----------------------------------------------------------------

const machine = { tag: '', names: null, lower: null, source: '', count: 0, loading: null, parameters: new Map() };

async function loadMachineCommands() {
  if (machine.loading) return machine.loading;
  machine.loading = (async () => {
    try {
      const response = await fetch('/api/editor/powershell/commands', {
        headers: { Accept: 'application/json', ...(machine.tag ? { 'If-None-Match': machine.tag } : {}) } });
      if (response.status === 304 || !response.ok) return;
      const data = await response.json();
      machine.tag = response.headers.get('ETag') || '';
      machine.names = (data.commands || []).map((row) => row[0]);
      machine.lower = machine.names.map((name) => name.toLowerCase());
      machine.source = data.source;
      machine.count = machine.names.length;
    } catch (error) { /* the built-in list stands in; completion is never empty */ } finally {
      machine.loading = null;
    }
  })();
  return machine.loading;
}

async function parametersOf(command) {
  const key = command.toLowerCase();
  if (machine.parameters.has(key)) return machine.parameters.get(key);
  machine.parameters.set(key, []);                          // asked once, whatever the answer
  try {
    const response = await fetch(`/api/editor/powershell/parameters?command=${encodeURIComponent(command)}`,
      { headers: { Accept: 'application/json' } });
    if (response.ok) machine.parameters.set(key, ((await response.json()).parameters || []).map((p) => `-${p}`));
  } catch (error) { /* no parameters offered */ }
  return machine.parameters.get(key);
}

export function powershellSource() {
  return machine.count ? `PowerShell commands: ${machine.count.toLocaleString()} from this machine`
    : 'PowerShell commands: built-in list';
}

// ---- the word at the caret ------------------------------------------------------------------------

function wordAt(box, language) {
  const text = box.value;
  const caret = box.selectionStart;
  const test = WORD_CHAR[language] || /\w/;
  let start = caret;
  while (start > 0 && test.test(text[start - 1])) start -= 1;
  let end = caret;
  while (end < text.length && test.test(text[end])) end += 1;
  return { start, end, word: text.slice(start, caret) };
}

function inComment(box, language) {
  const mark = COMMENT_MARK[language];
  if (!mark) return false;
  const text = box.value;
  const lineStart = text.lastIndexOf('\n', box.selectionStart - 1) + 1;
  const before = text.slice(lineStart, box.selectionStart);
  let quote = '';
  for (let i = 0; i < before.length; i += 1) {
    const c = before[i];
    if (quote) { if (c === quote) quote = ''; } else if (c === '"' || c === "'") quote = c;
    else if (c === mark || (language === 'terraform' && before.startsWith('//', i))) return true;
  }
  return false;
}

// The cmdlet a "-Param" being typed belongs to: the last Verb-Noun before it on the line.
function commandBefore(box, start) {
  const text = box.value;
  const lineStart = text.lastIndexOf('\n', start - 1) + 1;
  const found = text.slice(lineStart, start).match(/[A-Za-z]+-[A-Za-z][A-Za-z0-9]*(?=[^|;]*$)/g);
  return found ? found[found.length - 1] : '';
}

// ---- candidates -----------------------------------------------------------------------------------

function candidates(box, language, where, extra) {
  const lower = where.word.toLowerCase();
  const seen = new Set();
  const out = [];
  const add = (label, kind, rank, insert = label, snippet = false) => {
    const key = `${kind}|${label.toLowerCase()}`;
    if (seen.has(key) || label === where.word) return;
    const low = label.toLowerCase();
    const at = low.indexOf(lower);
    if (at < 0) return;
    seen.add(key);
    out.push({ label, kind, insert, snippet, score: [at === 0 ? 0 : 1, rank, label.length, low] });
  };
  for (const s of SNIPPETS[language] || []) add(s.label, 'snippet', 0, s.body, true);
  for (const p of extra || []) add(p, 'parameter', 1);
  const words = WORDS[language] || {};
  for (const w of [...(words.keywords || []), ...(words.operators || []), ...(words.variables || [])]) add(w, 'keyword', 2);
  // This machine's commands once they have arrived; until then, or with none (no PowerShell, the
  // compile off or failed), the built-in list - completion is never empty.
  if (language === 'powershell' && machine.names && machine.names.length) {
    for (let i = 0; i < machine.lower.length && out.length < 4000; i += 1) {
      if (machine.lower[i].includes(lower)) add(machine.names[i], 'command', 2);
    }
  } else {
    for (const w of words.builtins || []) add(w, 'builtin', 2);
  }
  // Words already in the file, the one being typed left out.
  const text = box.value.slice(0, where.start) + box.value.slice(where.end);
  const pattern = new RegExp(FILE_WORDS[language] || /[A-Za-z_]\w*/g);
  for (const m of text.matchAll(pattern)) if (m[0].length > 2) add(m[0], 'word', 3);
  out.sort((a, b) => {
    for (let i = 0; i < a.score.length; i += 1) {
      if (a.score[i] !== b.score[i]) return a.score[i] < b.score[i] ? -1 : 1;
    }
    return 0;
  });
  return out.slice(0, MAX_SHOWN);
}

// ---- snippets -------------------------------------------------------------------------------------

// "${1:Name}" / "$1" / "$0" -> plain text and the offsets of each stop, indented like the caret's line.
function expand(body, indent) {
  let text = '';
  const stops = [];
  const pattern = /\$\{(\d+)(?::([^}]*))?\}|\$(\d+)/g;
  let at = 0;
  for (const m of body.matchAll(pattern)) {
    text += body.slice(at, m.index);
    const number = Number(m[1] || m[3]);
    const placeholder = m[2] || '';
    stops.push({ number, start: text.length, end: text.length + placeholder.length });
    text += placeholder;
    at = m.index + m[0].length;
  }
  text += body.slice(at);
  const lines = text.split('\n');
  let shift = 0;
  const indented = lines.map((line, i) => (i === 0 ? line : indent + line)).join('\n');
  // Each stop moves by the indent added to the lines before it.
  for (const stop of stops) {
    const before = text.slice(0, stop.start).split('\n').length - 1;
    shift = before * indent.length;
    stop.start += shift;
    stop.end += shift;
  }
  stops.sort((a, b) => (a.number === 0 ? 1 : b.number === 0 ? -1 : a.number - b.number));
  return { text: indented, stops };
}

// ---- the list -------------------------------------------------------------------------------------

export function insertText(box, start, end, text) {
  box.focus();
  box.setSelectionRange(start, end);
  // execCommand keeps the edit on the textarea's own undo stack; setRangeText is the fallback.
  if (!document.execCommand('insertText', false, text)) {
    box.setRangeText(text, start, end, 'end');
    box.dispatchEvent(new Event('input', { bubbles: true }));
  }
}

export function attachCompletion(box, language, stack) {
  const list = h('ul', { class: 'complete-list', role: 'listbox', hidden: true });
  const foot = h('li', { class: 'complete-foot', role: 'presentation' });
  stack.append(list);
  const state = { items: [], index: 0, where: null, snippet: null };
  const charWidth = (() => {
    const ctx = document.createElement('canvas').getContext('2d');
    return () => {
      const cs = getComputedStyle(box);
      ctx.font = `${cs.fontSize} ${cs.fontFamily}`;
      return ctx.measureText('M').width;
    };
  })();
  if (language === 'powershell') loadMachineCommands();

  const close = () => { list.hidden = true; state.items = []; };
  const place = () => {
    const cs = getComputedStyle(box);
    const text = box.value.slice(0, state.where.start);
    const line = text.split('\n').length;
    const lineStart = text.lastIndexOf('\n') + 1;
    const column = text.slice(lineStart).replace(/\t/g, ' '.repeat(Number(cs.tabSize) || 4)).length;
    const lineHeight = parseFloat(cs.lineHeight) || 18;
    // CSSOM properties set from script, which the page's style-src 'self' allows.
    list.style.left = `${Math.max(0, parseFloat(cs.paddingLeft) + column * charWidth() - box.scrollLeft)}px`;
    list.style.top = `${parseFloat(cs.paddingTop) + line * lineHeight - box.scrollTop}px`;
  };
  const draw = () => {
    list.replaceChildren(...state.items.map((item, i) => h('li', {
      class: `complete-item${i === state.index ? ' active' : ''}`, role: 'option',
      'aria-selected': String(i === state.index), dataset: { index: String(i) } },
    h('span', { class: `complete-kind kind-${item.kind}` }, item.kind === 'snippet' ? '{ }' : item.kind[0]),
    h('span', { class: 'complete-label' }, item.label))));
    if (language === 'powershell') { foot.textContent = powershellSource(); list.append(foot); }
    list.hidden = !state.items.length;
    if (state.items.length) {
      place();
      const active = list.querySelector('.complete-item.active');
      if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
    }
  };
  const open = async (forced) => {
    if (inComment(box, language)) { close(); return; }
    const where = wordAt(box, language);
    if (!forced && where.word.length < 2) { close(); return; }
    let extra = [];
    if (language === 'powershell' && where.word.startsWith('-')) {
      const command = commandBefore(box, where.start);
      if (command) extra = await parametersOf(command);
    }
    if (box.selectionStart !== where.start + where.word.length) return;   // typed on meanwhile
    state.where = where;
    state.items = candidates(box, language, where, extra);
    state.index = 0;
    draw();
  };
  const accept = () => {
    const item = state.items[state.index];
    const where = wordAt(box, language);
    close();
    if (!item) return;
    if (!item.snippet) { insertText(box, where.start, where.end, item.insert); return; }
    const text = box.value;
    const lineStart = text.lastIndexOf('\n', where.start - 1) + 1;
    const indent = (/^[ \t]*/.exec(text.slice(lineStart)) || [''])[0];
    const { text: body, stops } = expand(item.insert, indent);
    insertText(box, where.start, where.end, body);
    state.snippet = { base: where.start, stops, at: 0, length: box.value.length };
    const first = stops[0];
    if (first) box.setSelectionRange(where.start + first.start, where.start + first.end);
  };
  // Tab inside a snippet: on to the next stop, the stops after the one just edited moved by however
  // much its text changed.
  const nextStop = () => {
    const s = state.snippet;
    if (!s) return false;
    const delta = box.value.length - s.length;
    s.length = box.value.length;
    for (let i = s.at + 1; i < s.stops.length; i += 1) { s.stops[i].start += delta; s.stops[i].end += delta; }
    s.at += 1;
    const stop = s.stops[s.at];
    if (!stop) { state.snippet = null; return false; }
    box.setSelectionRange(s.base + stop.start, s.base + stop.end);
    if (s.at === s.stops.length - 1) state.snippet = null;
    return true;
  };

  box.addEventListener('input', (event) => {
    if (event.inputType === 'insertText' || event.inputType === 'deleteContentBackward' || !list.hidden) open(false);
  });
  box.addEventListener('blur', () => setTimeout(close, 150));
  box.addEventListener('scroll', () => { if (!list.hidden) place(); });
  list.addEventListener('mousedown', (event) => {
    const li = event.target.closest('.complete-item');
    if (!li) return;
    event.preventDefault();
    state.index = Number(li.dataset.index);
    accept();
  });
  // The keys the list owns while it is open; returns true when the key was used here.
  return {
    keydown(event) {
      if (event.key === ' ' && event.ctrlKey) { event.preventDefault(); open(true); return true; }
      if (!list.hidden) {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          const step = event.key === 'ArrowDown' ? 1 : -1;
          state.index = (state.index + step + state.items.length) % state.items.length;
          draw();
          return true;
        }
        if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); accept(); return true; }
        if (event.key === 'Escape') { event.preventDefault(); close(); return true; }
      }
      if (event.key === 'Tab' && !event.shiftKey && state.snippet) {
        event.preventDefault();
        if (nextStop()) return true;
        return true;
      }
      if (event.key === 'Escape' && state.snippet) { state.snippet = null; return true; }
      return false;
    },
    indentUnit: INDENT[language] || '  ',
  };
}
