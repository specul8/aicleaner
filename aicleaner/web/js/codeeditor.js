// Feature 116: the code editor that takes the prompt box's place - a line-number gutter that scrolls
// with the text, no wrapping, sideways scrolling, a monospace font (built like the file editor, D101).
// Enter is a new line and never sends; Tab indents; Ctrl+Enter adds; Escape cancels (twice when
// something was typed, so a stray key does not throw it away).
import { h } from './dom.js';
import { LANGUAGES, MAX_LINES } from './snippets.js';

const INDENT = { powershell: '    ', python: '    ', csharp: '    ', sql: '    ' };

function numberLines(gutter, box, first) {
  const count = box.value.split('\n').length;
  const sig = `${first}:${count}`;
  if (gutter.dataset.sig === sig) return;
  gutter.dataset.sig = sig;
  gutter.textContent = Array.from({ length: count }, (_, i) => String(first + i)).join('\n');
}

// slot: where the editor is drawn; pane: the prompt pane, marked while the editor is open.
export function codeEditor({ slot, pane, say }) {
  let current = null;

  const close = () => {
    current = null;
    slot.replaceChildren();
    slot.hidden = true;
    pane.classList.remove('coding');
  };

  // { code, lang, firstLine, title, update, onDone(code, lang), onCancel(), onChange(state) }
  const open = (options) => {
    const first = Number(options.firstLine) > 0 ? Number(options.firstLine) : 1;
    const box = h('textarea', { class: 'code-editor-box', wrap: 'off', spellcheck: 'false',
      'aria-label': options.title || 'Code snippet', placeholder: 'Paste or type the code here' });
    box.value = options.code || '';
    const gutter = h('pre', { class: 'file-gutter code-editor-gutter', 'aria-hidden': 'true' });
    const select = h('select', { class: 'code-editor-lang', 'aria-label': 'Language' },
      ...LANGUAGES.map(([value, label]) => h('option', { value, selected: value === (options.lang || 'text') }, label)));
    if (options.lang && !LANGUAGES.some(([value]) => value === options.lang)) {
      select.append(h('option', { value: options.lang, selected: true }, options.lang));
    }
    const hint = h('span', { class: 'hint code-editor-hint' }, 'Ctrl+Enter adds it; Escape cancels');
    const done = h('button', { type: 'button', class: 'primary code-editor-done' },
      options.update ? 'Update snippet' : 'Add snippet to prompt');
    const cancel = h('button', { type: 'button', class: 'btn code-editor-cancel' }, 'Cancel');
    const original = box.value;
    let armed = false;
    const state = () => ({ code: box.value, lang: select.value });
    const finish = () => {
      if (box.value.split('\n').length > MAX_LINES) {
        say(`That is over ${MAX_LINES} lines - ask Claude to read the file instead.`, 'warn');
        return;
      }
      const result = state();
      close();
      options.onDone(result.code, result.lang);
    };
    const abandon = () => { close(); if (options.onCancel) options.onCancel(); };
    box.addEventListener('input', () => {
      numberLines(gutter, box, first);
      gutter.scrollTop = box.scrollTop;
      armed = false;
      hint.textContent = 'Ctrl+Enter adds it; Escape cancels';
      if (options.onChange) options.onChange(state());
    });
    select.addEventListener('change', () => { if (options.onChange) options.onChange(state()); });
    box.addEventListener('scroll', () => { gutter.scrollTop = box.scrollTop; });
    box.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); finish(); return; }
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        if (box.value !== original && !armed) {
          armed = true;
          hint.textContent = 'Press Escape again to throw this away';
          return;
        }
        abandon();
        return;
      }
      if (event.key === 'Tab' && !event.shiftKey && !event.ctrlKey && !event.altKey) {
        event.preventDefault();
        box.setRangeText(INDENT[select.value] || '  ', box.selectionStart, box.selectionEnd, 'end');
        box.dispatchEvent(new Event('input', { bubbles: true }));
      }
    });
    done.addEventListener('click', finish);
    cancel.addEventListener('click', abandon);
    numberLines(gutter, box, first);
    slot.replaceChildren(
      h('div', { class: 'code-editor-head' }, h('strong', {}, options.title || 'Code snippet'), select),
      h('div', { class: 'file-editor-frame code-editor-frame' }, gutter, box),
      h('div', { class: 'form-row code-editor-actions' }, done, cancel, hint));
    slot.hidden = false;
    pane.classList.add('coding');
    current = { box };
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  };

  return { open, close, isOpen: () => Boolean(current) };
}
