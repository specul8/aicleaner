// Editing a file tab (F54; DECISIONS 53).
//
// Autosave: 1.5 s after typing stops, and at once on leaving the box or pressing Ctrl+S. Every save
// carries the modified time the tab last read, and the server refuses it (409) if the file moved on
// - Claude edits these files too, and nothing locks them. A refused save stops autosave for that tab
// and offers Reload, Overwrite, or Copy mine then reload.
//
// The editor lives on the tab object and is reused across repaints: the pane is rebuilt every time
// anything about the tab changes, and a rebuilt textarea would lose the caret mid-word.

import { postJSON, say, sayError } from './api.js';
import { h } from './dom.js';
import { attachCompletion } from './editor/complete.js';
import { layerFor } from './editor/highlight.js';
import { attachKeys } from './editor/keys.js';
import { languageOf } from './editor/languages.js';
import { loadMonaco, monacoLanguage, monacoReady, MonacoBox } from './monacoedit.js';

const AUTOSAVE_MS = 1500;
const STATE_TEXT = { saved: 'Saved', saving: 'Saving...', unsaved: 'Unsaved changes', conflict: 'Not saved: changed on disk' };

async function post(body) {
  const response = await fetch('/api/session/file/write', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await response.json(); } catch (e) { data = {}; }
  return { status: response.status, data };
}

// Line numbers beside the editor (D101), as the read-only view has. The box does not wrap, so a line
// on screen is a line in the file and one number per line stays level with it; the gutter scrolls
// with the box rather than having a scroll of its own.
function numberLines(gutter, box) {
  const count = box.value.split('\n').length;
  if (gutter.dataset.count === String(count)) return;
  gutter.dataset.count = String(count);
  gutter.textContent = Array.from({ length: count }, (_, i) => String(i + 1)).join('\n');
}

// The element an editor lives in: a textarea is its own; Monaco's lives in its host (feature 139).
export const elementOf = (editor) => (editor && editor.host ? editor.host : editor);

export function focusedEditor(editor) {
  if (!editor) return false;
  return editor.hasFocus ? editor.hasFocus() : document.activeElement === editor;
}

// A tab's editor let go of (a reload, a refused save dropped): Monaco's model and view are freed.
export function dropEditor(tab) {
  if (tab.editor && tab.editor.dispose) tab.editor.dispose();
  tab.editor = null;
}

// What both editors do on a change, a blur and Ctrl+S.
function wire(tab, hooks, box, changed) {
  box.addEventListener('input', () => {
    if (changed) changed();
    tab.saveState = tab.saveState === 'conflict' ? 'conflict' : 'unsaved';
    hooks.paint();
    if (tab.saveState !== 'conflict') schedule(tab, hooks);
  });
  // A real blur saves. The one a repaint causes - it moves the box - does not: saving then would
  // repaint inside the repaint.
  box.addEventListener('blur', () => { if (!tab.repainting) saveNow(tab, hooks); });
}

// Feature 139: code for the cursor from the file's own project session - whichever AI service it runs -
// shown as grey text there (Tab takes it). Through aicleaner like every prompt, so filtered.
async function suggest(tab, hooks, box, button, instruction = '') {
  if (button.disabled) return;
  const { position, line, before, after } = box.cursorContext();
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Asking...';
  try {
    const answer = await postJSON('/api/session/suggest', { name: hooks.name(), path: tab.path || tab.name,
      language: monacoLanguage(tab.name), line, before, after, instruction });
    box.offer(position, answer.code);
  } catch (error) {
    sayError(error);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

// The Suggest control over the editor: the button asks at once; the box beside it says what to write.
function suggestBar(tab, hooks, box) {
  const button = h('button', { type: 'button', class: 'editor-suggest', title: 'Ask the session\'s AI for code at the cursor (Alt+\\)' }, 'Suggest');
  const what = h('input', { type: 'text', class: 'editor-suggest-what', placeholder: 'What it should do (optional)',
    'aria-label': 'What the suggested code should do' });
  button.addEventListener('click', () => suggest(tab, hooks, box, button, what.value));
  what.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); suggest(tab, hooks, box, button, what.value); }
  });
  box.bindSuggest(() => suggest(tab, hooks, box, button, what.value));
  return h('div', { class: 'editor-suggest-bar' }, what, button);
}

// Feature 139: Monaco, the editor VS Code is built on - its colouring, completion, keys and line numbers.
function monacoFrame(tab, hooks, text) {
  const host = h('div', { class: 'file-editor', 'aria-label': `Edit ${tab.name}` });
  const box = new MonacoBox(host, text, tab.name);
  wire(tab, hooks, box);
  box.bindSave(() => saveNow(tab, hooks));
  tab.editor = box;
  tab.editorFrame = h('div', { class: 'file-editor-frame monaco' }, suggestBar(tab, hooks, box), host);
  tab.saveState = 'saved';
  return tab.editorFrame;
}

// hooks: { name(), paint(), reload(tab) }
// Returns the frame (gutter and box); the box itself stays on tab.editor.
export function editorFor(tab, hooks) {
  if (tab.editor) return tab.editorFrame;
  if (monacoReady()) return monacoFrame(tab, hooks, tab.data.text || '');
  const box = h('textarea', { class: 'file-editor', spellcheck: 'false', wrap: 'off', 'aria-label': `Edit ${tab.name}` });
  box.value = tab.data.text || '';
  const gutter = h('pre', { class: 'file-gutter', 'aria-hidden': 'true' });
  numberLines(gutter, box);
  box.addEventListener('scroll', () => { gutter.scrollTop = box.scrollTop; });
  // A language the editor knows (feature 80): colouring behind the box, suggestions, editing keys.
  // Any other file stays the plain textarea it always was.
  const language = languageOf(tab.name);
  let body = box;
  if (language) {
    const { stack } = layerFor(box, language);
    attachKeys(box, language, attachCompletion(box, language, stack));
    body = stack;
  }
  tab.editorFrame = h('div', { class: 'file-editor-frame' }, gutter, body);
  tab.saveState = 'saved';
  wire(tab, hooks, box, () => { numberLines(gutter, box); gutter.scrollTop = box.scrollTop; });
  box.addEventListener('keydown', (event) => {
    if (event.key === 's' && (event.ctrlKey || event.metaKey) && !event.altKey) {
      event.preventDefault();
      saveNow(tab, hooks);
    }
  });
  tab.editor = box;
  // Monaco still loading (the first tab of a page load): it takes over once it arrives - but only while
  // nobody has started on the plain editor, so nothing typed is ever moved under the caret.
  loadMonaco().then((ready) => {
    if (!ready || tab.editor !== box || tab.saveState !== 'saved' || document.activeElement === box) return;
    const old = tab.editorFrame;
    const fresh = monacoFrame(tab, hooks, box.value);
    if (old.isConnected) old.replaceWith(fresh);
  });
  return tab.editorFrame;
}

function schedule(tab, hooks) {
  clearTimeout(tab.saveTimer);
  tab.saveTimer = setTimeout(() => saveNow(tab, hooks), AUTOSAVE_MS);
}

// `overwrite`: send the time the refusal named, which says "I have seen it, write mine anyway".
export async function saveNow(tab, hooks, overwrite = false) {
  clearTimeout(tab.saveTimer);
  if (!tab.editor || tab.saving) return;
  if (tab.saveState === 'saved' || (tab.saveState === 'conflict' && !overwrite)) return;
  const text = tab.editor.value;
  tab.saving = true;
  tab.saveState = 'saving';
  hooks.paint();
  try {
    const { status, data } = await post({ name: hooks.name(), root: tab.root, path: tab.path, text,
      modified: overwrite ? tab.conflictModified : tab.modified });
    if (status === 409) {
      tab.saveState = 'conflict';
      tab.conflictModified = data.modified;
    } else if (status >= 400 || data.error) {
      tab.saveState = 'unsaved';
      sayError(new Error(data.error || `Could not save ${tab.name}`));
    } else {
      tab.modified = data.modified;
      tab.stale = false;
      // Typed while the save was in flight: still unsaved, and it goes on the next timer.
      tab.saveState = tab.editor && tab.editor.value !== text ? 'unsaved' : 'saved';
      if (tab.saveState === 'unsaved') schedule(tab, hooks);
    }
  } catch (error) {
    tab.saveState = 'unsaved';
    sayError(error);
  } finally {
    tab.saving = false;
    hooks.paint();
  }
}

export function saveChip(tab) {
  if (!tab.editor) return null;
  const level = { saved: '', saving: '', unsaved: ' warn', conflict: ' bad' }[tab.saveState] || '';
  return h('span', { class: `chip file-save-state${level}`, dataset: { state: tab.saveState } }, STATE_TEXT[tab.saveState]);
}

// Shown only while a save is refused: what happened and the three ways out.
export function conflictBar(tab, hooks) {
  if (tab.saveState !== 'conflict') return null;
  const drop = () => { dropEditor(tab); hooks.reload(tab); };
  const reload = h('button', { type: 'button', class: 'conflict-reload' }, 'Reload (drop my edits)');
  const overwrite = h('button', { type: 'button', class: 'conflict-overwrite' }, 'Overwrite with mine');
  const copy = h('button', { type: 'button', class: 'conflict-copy' }, 'Copy mine, then reload');
  reload.addEventListener('click', drop);
  overwrite.addEventListener('click', () => saveNow(tab, hooks, true));
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(tab.editor.value);
      say('Your text is on the clipboard.', 'ok');
      drop();
    } catch (error) { sayError(error); }
  });
  return h('div', { class: 'callout bad file-conflict' },
    h('b', {}, `${tab.name} changed on disk since you opened it - Claude may have edited it. `),
    'Your edits are not saved. ', reload, ' ', overwrite, ' ', copy);
}

export function unsaved(tabs) {
  return tabs.some((t) => t.editor && t.saveState !== 'saved');
}
