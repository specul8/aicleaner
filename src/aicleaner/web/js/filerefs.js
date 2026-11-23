// f188 (Brad, 2026-11-20): file names and paths in the Session transcript, clickable.
//
// A reply or prompt is drawn first, as it always was; when it comes near the visible log, the names and paths in it are
// found here and asked of the server in batches (POST /api/session/file/refs). Only what the server says is an existing
// file inside the session's folders - the Files panel's own allow-list - becomes a link, and a click opens it as a
// double-click in the Files panel does. Display only: the transcript and what Claude sees are never changed.
//
// What counts as a reference:
// - prose (paragraphs, list items, table cells, headings, quotes, a prompt's text): a word that is an absolute Windows
//   path, a relative path or a bare file name with an extension, or a quoted one (spaces allowed inside the quotes);
// - an inline code span that is, as a whole, one of those (spaces allowed);
// - a code block's line that is, as a whole, one of those - never a name inside a command or a script line.

import { postJSON } from './api.js';
import { h } from './dom.js';

const BATCH = 200;              // refs per request (the server's limit)
const BATCH_WAIT_MS = 120;      // turns coming into view together are asked about together
const NEAR_PX = 600;            // scanned this far before it is on screen
const RESCAN_WAIT_MS = 400;
const UNFINISHED_TRIES = 5;
const UNFINISHED_WAIT_MS = 1500;

const LINE_SUFFIX = /(?::\d+(?:[-:]\d+)?|#L\d+(?:-L?\d+)?)$/;
const LAST_PART = /^[^\\/:*?"<>|]*[^\\/:*?"<>|.\s]\.[A-Za-z][A-Za-z0-9_-]{0,9}$/;
const NOT_NAMES = new Set(['e.g', 'i.e', 'etc', 'a.m', 'p.m', 'vs']);
const LEADING = /^[([{<'"\u2018\u201c*_]+/;
const TRAILING = /[)\]}>'"\u2019\u201d*_.,;:!?]+$/;
const QUOTED = /(^|[\s([{])(['"\u2018\u201c])([^'"\u2018\u2019\u201c\u201d\n]{3,300}?)(['"\u2019\u201d])(?=$|[\s)\]}.,;:!?])/g;
const BLOCKS = 'p, li, td, th, h1, h2, h3, h4, h5, h6, blockquote, .prompt-text, .md-reply';

const ctx = { name: () => '', open: () => {} };
const state = { session: '', answers: new Map(), waiting: new Map(), queue: new Set(), timer: null, fresh: false,
  generation: 0, unfinished: 0, observer: null, observerRoot: null, visible: new Set(), rescan: null };

// Whether `text` (already trimmed) names a file: an absolute Windows path, a relative path or a bare name with an
// extension. `spaces`: inside quotes or a code span a path may hold spaces.
export function isFileRef(text, spaces = false) {
  if (!text || text.length < 3 || text.length > 400) return false;
  if (!spaces && /\s/.test(text)) return false;
  if (/[\n\r\t]/.test(text) || text.includes('://') || /^[^\s@]+@[^\s@]+$/.test(text)) return false;
  if (text.startsWith('\\\\') || text.startsWith('//')) return false;              // a network path: never
  const bare = text.replace(LINE_SUFFIX, '');
  if (/[<>"|?*]/.test(bare)) return false;
  if (/^[A-Za-z]:/.test(bare) && !/^[A-Za-z]:[\\/]/.test(bare)) return false;       // drive-relative
  if (bare.slice(2).includes(':')) return false;
  if (/^[\\/]/.test(bare)) return false;                                             // rooted without a drive
  const last = bare.split(/[\\/]/).pop();
  if (!LAST_PART.test(last) || NOT_NAMES.has(last.toLowerCase())) return false;
  return !/^\d+(\.\d+)+$/.test(last);
}

// [{ start, end, ref }] in a run of prose: quoted names first (they may hold spaces), then each word.
export function proseRefs(text) {
  const found = [];
  const taken = (start, end) => found.some((f) => start < f.end && end > f.start);
  for (const quoted of text.matchAll(QUOTED)) {
    const inner = quoted[3].trim();
    const start = quoted.index + quoted[1].length + 1 + quoted[3].indexOf(inner);
    if (isFileRef(inner, true)) found.push({ start, end: start + inner.length, ref: inner });
  }
  for (const word of text.matchAll(/\S+/g)) {
    const lead = (LEADING.exec(word[0]) || [''])[0].length;
    const trimmed = word[0].slice(lead).replace(TRAILING, '');
    if (!trimmed) continue;
    const start = word.index + lead;
    const end = start + trimmed.length;
    if (!taken(start, end) && isFileRef(trimmed)) found.push({ start, end, ref: trimmed });
  }
  return found.sort((a, b) => a.start - b.start);
}

// ---- the DOM: where the references are --------------------------------------------------------------------------------

function textGroup(nodes) {
  let at = 0;
  const parts = nodes.map((node) => { const part = { node, start: at }; at += node.nodeValue.length; return part; });
  return { parts, text: nodes.map((n) => n.nodeValue).join('') };
}

function insideSkipped(node, root) {
  for (let el = node.parentElement; el && el !== root; el = el.parentElement) {
    if (el.matches('code, pre, a, .snippet, button, textarea, input')) return true;
  }
  return false;
}

// Every candidate in `root`: [{ ref, group, start, end }] for text, or [{ ref, element }] for a whole code span.
export function candidates(root) {
  const out = [];
  for (const code of root.querySelectorAll('code')) {
    if (code.closest('pre, a, .snippet')) continue;
    const text = code.textContent.trim();
    if (isFileRef(text, true)) out.push({ ref: text, element: code });
  }
  for (const pre of root.querySelectorAll('pre')) {
    if (pre.closest('.snippet, a')) continue;
    const group = textGroup(textNodes(pre, () => true));
    if (!group.parts.length) continue;
    let at = 0;
    for (const line of group.text.split('\n')) {
      const trimmed = line.trim();
      // A line already a link (drawn before the tree changed) is left as it is.
      if (trimmed && isFileRef(trimmed, true)
          && !pointAt(group, at + line.indexOf(trimmed), false)[0].parentElement.closest('a')) {
        const start = at + line.indexOf(trimmed);
        out.push({ ref: trimmed, group, start, end: start + trimmed.length });
      }
      at += line.length + 1;
    }
  }
  // Prose: consecutive text nodes under the same block read as one run, so a path crossing a restored name is whole.
  let run = [];
  let block = null;
  const flush = () => {
    if (run.length) {
      const group = textGroup(run);
      for (const found of proseRefs(group.text)) out.push({ ...found, group });
    }
    run = [];
  };
  for (const node of textNodes(root, () => true)) {
    // Code, a link, a snippet: not prose, and the prose either side of it is two runs, never one.
    if (insideSkipped(node, root)) { flush(); block = null; continue; }
    const owner = node.parentElement.closest(BLOCKS) || root;
    if (owner !== block) { flush(); block = owner; }
    run.push(node);
  }
  flush();
  return out;
}

function textNodes(root, keep) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) if (walker.currentNode.nodeValue && keep(walker.currentNode)) nodes.push(walker.currentNode);
  return nodes;
}

function pointAt(group, offset, end) {
  for (let i = 0; i < group.parts.length; i++) {
    const part = group.parts[i];
    const length = part.node.nodeValue.length;
    // An end on a node's boundary stays in that node; a start there moves to the next one.
    if (end ? offset <= part.start + length : offset < part.start + length) return [part.node, offset - part.start];
  }
  const last = group.parts[group.parts.length - 1];
  return [last.node, last.node.nodeValue.length];
}

// ---- links and the chooser --------------------------------------------------------------------------------------------

function titleOf(matches) {
  if (matches.length === 1) return matches[0].full;
  return `${matches.length} files named ${matches[0].name} - choose one:\n${matches.map((m) => m.full).join('\n')}`;
}

function makeLink(matches) {
  const link = h('a', { href: '#', class: 'file-ref', title: titleOf(matches),
    dataset: { fileRef: matches.length === 1 ? 'one' : 'many' } });
  link.addEventListener('click', (event) => {
    event.preventDefault();
    if (matches.length === 1) ctx.open(matches[0]);
    else openChooser(link, matches);
  });
  return link;
}

function rootLabel(root) {
  return String(root).replace(/[\\/]+$/, '').split(/[\\/]/).pop() || String(root);
}

function closeChooser(focusBack = false) {
  const open = document.querySelector('.file-ref-chooser');
  if (!open) return;
  const link = open.fileRefLink;
  open.remove();
  document.removeEventListener('pointerdown', outside, true);
  if (focusBack && link && link.isConnected) link.focus();
}

function outside(event) {
  const open = document.querySelector('.file-ref-chooser');
  if (open && !open.contains(event.target) && !(open.fileRefLink && open.fileRefLink.contains(event.target))) closeChooser();
}

function openChooser(link, matches) {
  closeChooser();
  const items = matches.map((m) => h('button', { type: 'button', class: 'file-ref-choice', role: 'menuitem', title: m.full },
    h('span', { class: 'file-ref-folder' }, rootLabel(m.root)), h('span', { class: 'file-ref-path' }, m.path)));
  const menu = h('div', { class: 'file-ref-chooser', role: 'menu', 'aria-label': `Files named ${matches[0].name}` },
    h('div', { class: 'file-ref-chooser-head hint' }, `${matches.length} files named ${matches[0].name}`), items);
  menu.fileRefLink = link;
  items.forEach((item, i) => item.addEventListener('click', () => { closeChooser(); ctx.open(matches[i]); }));
  menu.addEventListener('keydown', (event) => {
    const at = items.indexOf(document.activeElement);
    if (event.key === 'Escape') { event.preventDefault(); closeChooser(true); }
    else if (event.key === 'ArrowDown') { event.preventDefault(); items[(at + 1) % items.length].focus(); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); items[(at - 1 + items.length) % items.length].focus(); }
    else if (event.key === 'Home') { event.preventDefault(); items[0].focus(); }
    else if (event.key === 'End') { event.preventDefault(); items[items.length - 1].focus(); }
  });
  document.body.append(menu);
  const rect = link.getBoundingClientRect();
  const width = menu.offsetWidth;
  menu.style.left = `${Math.max(4, Math.min(rect.left, window.innerWidth - width - 4))}px`;
  const below = rect.bottom + 4;
  menu.style.top = `${below + menu.offsetHeight > window.innerHeight ? Math.max(4, rect.top - menu.offsetHeight - 4) : below}px`;
  document.addEventListener('pointerdown', outside, true);
  items[0].focus();
}

// The links in `root` for every candidate the server found; right to left within a run, so earlier offsets hold.
function apply(root) {
  const found = candidates(root).filter((c) => (state.answers.get(c.ref) || []).length);
  for (const c of found.filter((f) => f.element)) {
    const link = makeLink(state.answers.get(c.ref));
    c.element.replaceWith(link);
    link.append(c.element);
  }
  const text = found.filter((f) => f.group).sort((a, b) => b.start - a.start);
  for (const c of text) {
    const range = document.createRange();
    range.setStart(...pointAt(c.group, c.start, false));
    range.setEnd(...pointAt(c.group, c.end, true));
    const link = makeLink(state.answers.get(c.ref));
    link.append(range.extractContents());
    range.insertNode(link);
  }
}

// ---- asking the server: batched, deduplicated, cached per session ------------------------------------------------------

function sameSession() {
  const name = ctx.name() || '';
  if (name !== state.session) {
    state.session = name;
    state.answers.clear();
    state.waiting.clear();
    state.queue.clear();
  }
  return name;
}

function ask(ref) {
  if (state.answers.has(ref)) return Promise.resolve();
  return new Promise((resolve) => {
    if (!state.waiting.has(ref)) state.waiting.set(ref, []);
    state.waiting.get(ref).push(resolve);
    state.queue.add(ref);
    if (!state.timer) state.timer = setTimeout(send, BATCH_WAIT_MS);
  });
}

async function send() {
  state.timer = null;
  const name = state.session;
  const refs = [...state.queue].slice(0, BATCH);
  refs.forEach((r) => state.queue.delete(r));
  if (state.queue.size) state.timer = setTimeout(send, BATCH_WAIT_MS);
  if (!refs.length) return;
  const fresh = state.fresh;
  state.fresh = false;
  let matches = {};
  let complete = true;
  try {
    const answer = await postJSON('/api/session/file/refs', { name, refs, fresh });
    matches = answer.matches || {};
    complete = answer.complete !== false;
  } catch (error) {
    matches = {};                     // not a link is the safe answer; the next tree change asks again
  }
  if (name !== state.session) return;
  // The server's name index ran out of time (a large tree): the names not found are asked again shortly, a few times,
  // as it carries on walking.
  if (!complete && state.unfinished < UNFINISHED_TRIES) {
    state.unfinished += 1;
    setTimeout(() => again(false), UNFINISHED_WAIT_MS);
  } else if (complete) state.unfinished = 0;
  for (const ref of refs) {
    state.answers.set(ref, Array.isArray(matches[ref]) ? matches[ref] : []);
    for (const done of state.waiting.get(ref) || []) done();
    state.waiting.delete(ref);
  }
}

async function scan(el) {
  if (!el.isConnected || el.dataset.fileRefs === String(state.generation)) return;
  const generation = state.generation;
  el.dataset.fileRefs = String(generation);
  sameSession();
  const refs = [...new Set(candidates(el).map((c) => c.ref))];
  if (!refs.length) return;
  await Promise.all(refs.map(ask));
  if (generation !== state.generation || !el.isConnected) return;
  apply(el);
}

function observer() {
  const log = document.getElementById('session-log');
  if (state.observer && state.observerRoot === log) return state.observer;
  if (state.observer) state.observer.disconnect();
  state.visible.clear();
  state.observerRoot = log;
  state.observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) { state.visible.add(entry.target); scan(entry.target); continue; }
      state.visible.delete(entry.target);
      // Drawn and since taken away (another session, the page left): no longer watched.
      if (!entry.target.isConnected && entry.target.dataset.fileRefs !== undefined) state.observer.unobserve(entry.target);
    }
  }, { root: log && log.isConnected ? log : null, rootMargin: `${NEAR_PX}px 0px` });
  // A new log (the page drawn again): what it already holds is watched against it.
  if (log) for (const el of log.querySelectorAll('.md-reply, .prompt-text')) state.observer.observe(el);
  return state.observer;
}

// A reply's or a prompt's element, to be scanned when it comes near the visible log. Never while it is drawn.
export function watchFileRefs(el) {
  if (!el || typeof IntersectionObserver === 'undefined') return el;
  observer().observe(el);
  return el;
}

// The Files tree changed (a turn ended, a file was made, renamed, deleted, dropped; Refresh): what was asked is asked
// again, the server walks its index again, and the turns in view are scanned again - a missing name can become a link.
export function forgetFileRefs() {
  state.unfinished = 0;
  again(true);
}

function again(fresh) {
  if (fresh) state.answers.clear();
  else for (const [ref, found] of [...state.answers]) if (!found.length) state.answers.delete(ref);
  if (fresh) state.fresh = true;
  state.generation += 1;
  clearTimeout(state.rescan);
  state.rescan = setTimeout(() => { for (const el of [...state.visible]) scan(el); }, RESCAN_WAIT_MS);
}

// session.js says which session the transcript is, and how a file is opened (the Files panel's double-click).
export function fileRefContext({ name, open }) {
  if (name) ctx.name = name;
  if (open) ctx.open = open;
}

window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && document.querySelector('.file-ref-chooser')) closeChooser(true);
});
