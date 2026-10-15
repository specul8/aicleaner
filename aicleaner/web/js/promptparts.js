// Feature 118: the prompt box as text boxes with snippet objects between them. A textarea cannot fold
// lines, cut one line off with "..." or treat a block as one thing, so each snippet (feature 116's
// fenced block) is an element of its own, folded to its fences, first and last code lines; the typed
// text around it stays in real textareas (phone keyboards, spell check, undo and autocorrect as
// before). There is always a text box before, between and after snippets, so there is somewhere to
// type. Any closed fenced block that appears in a text box - typed, pasted, the image reader's text,
// "Discuss this code" - is turned into an object, so code that writes snippet text keeps working.
//
// A snippet is read-only in the box. Click or tap selects it; Delete or Backspace then removes it;
// Delete just before it (Backspace just after it) selects it first, so it takes two presses; Ctrl+X
// and Ctrl+C put its text on the clipboard and pasting it into a text box makes it an object again;
// double-click, Enter, or a second tap on a selected one opens it in the code editor.
import { h } from './dom.js';
import { findSnippets, joinPieces, promptPieces } from './snippets.js';
import { onFirstRow, onLastRow } from './caretrow.js';

export const FOLD_OVER = 5;               // code lines shown in full up to this many (the user, 2026-10-14)

// The lines a snippet shows in the box: its heading and fences always, the code folded when long.
export function foldedLines(text) {
  const found = findSnippets(text)[0];
  if (!found) return String(text || '').split('\n');
  const head = text.slice(found.start, found.codeStart).replace(/\n$/, '').split('\n');
  const code = found.code === '' ? [] : found.code.split('\n');
  const shown = code.length > FOLD_OVER
    ? [code[0], `<double-click to edit - ${code.length - 2} more lines>`, code[code.length - 1]]
    : code;
  return [...head, ...shown, text.slice(found.codeEnd).replace(/^\n/, '')];
}

// first: { box, element } - the box that is there from the start (it keeps the page's id); makeBox():
// another { box, element }; onEdit(part): open a snippet in the code editor; onActive(box): the text box
// typed in last, which the page's caret-based actions use.
export function promptParts({ first, makeBox, onEdit, onActive }) {
  const container = h('div', { class: 'prompt-box-parts' });
  let parts = [];
  let active = first.box;
  let editedAt = 0;

  const textPart = (made) => {
    const part = { kind: 'text', box: made.box, element: made.element };
    made.box.addEventListener('focus', () => { active = made.box; onActive(made.box); });
    made.box.addEventListener('input', () => { if (!normalise(part)) size(part); });
    return part;
  };
  const indexOf = (part) => parts.indexOf(part);

  // Text boxes between snippets grow with what is in them; with no snippets the one box fills the
  // prompt as it always has.
  const size = (part) => {
    if (!container.classList.contains('has-snippets')) { part.box.style.height = ''; return; }
    part.box.style.height = 'auto';
    part.box.style.height = `${part.box.scrollHeight + 2}px`;
  };
  const render = () => {
    container.replaceChildren(...parts.map((part) => part.element));
    container.classList.toggle('has-snippets', parts.length > 1);
    for (const part of parts) if (part.kind === 'text') size(part);
  };

  const edit = (part) => {
    if (Date.now() - editedAt < 600) return;           // a double tap is also a double-click
    editedAt = Date.now();
    onEdit(part);
  };
  const snippetPart = (text) => {
    const part = { kind: 'snippet', text, element: null };
    part.element = snippetElement(part);
    return part;
  };
  const snippetElement = (part) => {
    const found = findSnippets(part.text)[0];
    const lines = found ? found.code.split('\n').length : 0;
    const element = h('div', { class: 'prompt-snippet', tabindex: '0', role: 'group',
      'aria-label': `Code snippet${found && found.number ? ` ${found.number}` : ''}, ${lines} line${lines === 1 ? '' : 's'}. Delete removes it, Enter opens it.`,
      title: 'Double-click to edit; select it and press Delete to remove it' },
    ...foldedLines(part.text).map((line, i, all) => h('div', {
      class: `prompt-snippet-line${/^<double-click to edit/.test(line) && i > 0 && i < all.length - 1 ? ' fold' : ''}` }, line || ' ')));
    let selectedBefore = false;
    let touch = false;
    element.addEventListener('pointerdown', (event) => {
      selectedBefore = document.activeElement === element;
      touch = event.pointerType === 'touch' || event.pointerType === 'pen';
    });
    element.addEventListener('click', () => {
      if (touch && selectedBefore) edit(part);
      else element.focus();
    });
    element.addEventListener('dblclick', (event) => { event.preventDefault(); edit(part); });
    element.addEventListener('keydown', (event) => snippetKey(event, part));
    return element;
  };

  // Split a text box holding closed snippets into text, snippet, text...; true when it did.
  const normalise = (part) => {
    const pieces = promptPieces(part.box.value);
    if (pieces.length === 1) return false;
    const at = indexOf(part);
    const caret = part.box.selectionStart;
    const typing = document.activeElement === part.box;
    const added = [];
    for (let i = 1; i < pieces.length; i += 2) added.push(snippetPart(pieces[i]), textPart(makeBox()));
    part.box.value = pieces[0];
    added.forEach((one, i) => { if (one.kind === 'text') one.box.value = pieces[i + 1]; });
    parts.splice(at + 1, 0, ...added);
    render();
    // A caret after what arrived goes below the new snippet, so typing carries on there; one before
    // it stays. Focus moves only if it was in this box: an image read finishing must not take it.
    const after = added[added.length - 1];
    if (typing) {
      if (caret > pieces[0].length) focusText(after, 0);
      else focusText(part, caret);
    }
    after.box.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  };

  const focusText = (part, at) => {
    part.box.focus();
    const where = at === null ? part.box.value.length : at;
    part.box.setSelectionRange(where, where);
  };
  const selectSnippet = (part) => part.element.focus();

  // Remove a snippet: the text either side joins into the box before it, the caret where they meet.
  const remove = (part) => {
    const at = indexOf(part);
    if (at < 1) return;
    const before = parts[at - 1];
    const after = parts[at + 1];
    const meet = before.box.value.length;
    const joiner = before.box.value && after.box.value ? '\n' : '';
    before.box.value = before.box.value + joiner + after.box.value;
    parts.splice(at, 2);
    render();
    focusText(before, meet);
    before.box.dispatchEvent(new Event('input', { bubbles: true }));
  };

  const copyText = async (text) => {
    try { await navigator.clipboard.writeText(text); return; } catch (error) { /* not allowed: below */ }
    const scratch = h('textarea', { class: 'prompt-scratch' }, text);
    document.body.append(scratch);
    scratch.select();
    document.execCommand('copy');
    scratch.remove();
  };

  const snippetKey = (event, part) => {
    const at = indexOf(part);
    const key = event.key;
    const ctrl = event.ctrlKey || event.metaKey;
    if (key === 'Delete' || key === 'Backspace') { event.preventDefault(); remove(part); return; }
    if (key === 'Enter' || key === 'F2') { event.preventDefault(); edit(part); return; }
    if (ctrl && (key === 'x' || key === 'X')) { event.preventDefault(); copyText(part.text).then(() => remove(part)); return; }
    if (ctrl && (key === 'c' || key === 'C')) { event.preventDefault(); copyText(part.text); return; }
    // Paste with a snippet selected lands just after it: the paste goes to wherever focus is by then.
    if (ctrl && (key === 'v' || key === 'V')) { focusText(parts[at + 1], 0); return; }
    if (key === 'ArrowLeft' || key === 'ArrowUp') { event.preventDefault(); focusText(parts[at - 1], null); return; }
    if (key === 'ArrowRight' || key === 'ArrowDown' || key === 'Escape') { event.preventDefault(); focusText(parts[at + 1], 0); return; }
    // Typing on a selected snippet goes into the text after it, never into the snippet.
    if (key.length === 1 && !ctrl && !event.altKey) focusText(parts[at + 1], 0);
  };

  // At a text box's edge, the keys that reach a neighbouring snippet select it. Captured on the
  // container so it runs before the page's own keys (Up and Down recall earlier prompts).
  container.addEventListener('keydown', (event) => {
    const part = parts.find((one) => one.kind === 'text' && one.box === event.target);
    if (!part || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    const box = part.box;
    if (box.selectionStart !== box.selectionEnd) return;
    const at = indexOf(part);
    const previous = parts[at - 1];
    const next = parts[at + 1];
    const atStart = box.selectionStart === 0;
    const atEnd = box.selectionEnd === box.value.length;
    let target = null;
    if (previous && ((event.key === 'Backspace' && atStart) || (event.key === 'ArrowLeft' && atStart)
        || (event.key === 'ArrowUp' && onFirstRow(box)))) target = previous;
    if (next && ((event.key === 'Delete' && atEnd) || (event.key === 'ArrowRight' && atEnd)
        || (event.key === 'ArrowDown' && onLastRow(box)))) target = next;
    if (!target) return;
    event.preventDefault();
    event.stopPropagation();
    selectSnippet(target);
  }, true);

  parts = [textPart(first)];
  render();

  return {
    element: container,
    get value() {
      return joinPieces(parts.map((part) => (part.kind === 'text' ? part.box.value : part.text)));
    },
    // The whole prompt replaced (a draft, a recalled prompt, cleared after sending).
    set value(text) {
      const pieces = promptPieces(text);
      parts = [parts[0]];
      parts[0].box.value = pieces[0];
      for (let i = 1; i < pieces.length; i += 2) {
        const after = textPart(makeBox());
        after.box.value = pieces[i + 1];
        parts.push(snippetPart(pieces[i]), after);
      }
      render();
      active = parts[0].box;
    },
    // The text box the caret was last in.
    get box() { return active.isConnected || active === first.box ? active : first.box; },
    boxes() { return parts.filter((part) => part.kind === 'text').map((part) => part.box); },
    isFirst(box) { return parts[0].box === box; },
    isLast(box) { return parts[parts.length - 1].box === box; },
    // A snippet edited in the code editor goes back where it was (empty removes it).
    replace(part, text) {
      const at = indexOf(part);
      if (at < 0) return;
      if (!String(text || '').trim()) { remove(part); return; }
      part.text = text;
      const old = part.element;
      part.element = snippetElement(part);
      old.replaceWith(part.element);
      selectSnippet(part);
      parts[at + 1].box.dispatchEvent(new Event('input', { bubbles: true }));
    },
    set disabled(off) {
      for (const part of parts) {
        if (part.kind === 'text') part.box.disabled = off;
        else part.element.tabIndex = off ? -1 : 0;
      }
      container.classList.toggle('disabled', off);
    },
    focus(options) { this.box.focus(options); },
    refresh() { for (const part of parts) if (part.kind === 'text') size(part); },
  };
}
