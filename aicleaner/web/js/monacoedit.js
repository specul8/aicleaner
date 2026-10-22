// Feature 139 (Brad, 2026-10-22): the file editor behaves like VS Code - it is Monaco, the editor VS Code is
// built on (MIT), vendored under /web/vendor/monaco and bundled by tools/build_monaco.py (no CDN: the pages work
// offline and through the encrypted tunnel).
//
// The rest of the page drives the editor as it drove the textarea - value, a selection by offsets, scrollTop,
// focus, input/blur/scroll events - so `MonacoBox` speaks that surface over Monaco's API. Where Monaco cannot
// load (no style nonce, a failed import) the textarea editor is kept: nothing is lost, only the colouring.
//
// The page's policy allows no inline styles. Monaco makes <style> elements; each page load carries a fresh nonce
// (a meta tag the server writes) that the policy allows, and every style element made after this loads gets it.

const BASE = '/web/vendor/monaco';
let loading = null;
let monaco = null;

export function styleNonce() {
  const meta = document.querySelector('meta[name="aicleaner-style-nonce"]');
  return meta ? meta.content : '';
}

// Started early (the Session page asks as it opens), so the first tab usually finds it ready.
export function loadMonaco() {
  if (loading) return loading;
  const nonce = styleNonce();
  if (!nonce) { loading = Promise.resolve(null); return loading; }
  const make = Document.prototype.createElement;
  Document.prototype.createElement = function createElement(tag, options) {
    const element = make.call(this, tag, options);
    if (String(tag).toLowerCase() === 'style') element.nonce = nonce;
    return element;
  };
  self.MonacoEnvironment = {
    getWorkerUrl: (_id, label) => `${BASE}/${label === 'json' ? 'json' : 'editor'}.worker.js`,
  };
  if (!document.querySelector(`link[href="${BASE}/monaco.css"]`)) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = `${BASE}/monaco.css`;
    document.head.append(link);
  }
  // Monaco cancels its own work all the time (a suggestion dismissed, a request overtaken) and lets the
  // "Canceled" rejection go unhandled; it is not an error, so it is not reported as one.
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    if (reason && (reason.name === 'Canceled' || reason.message === 'Canceled')) event.preventDefault();
  });
  loading = import(`${BASE}/monaco.js`).then(async (module) => {
    monaco = module;
    const { registerCompletions } = await import('./editor/monacocomplete.js');
    registerCompletions(module);
    registerOffers(module);
    return module;
  }).catch((error) => {
    console.warn('The VS Code editor could not load; the plain editor stays.', error);
    return null;
  });
  return loading;
}

export const monacoReady = () => monaco;

// The AI's suggestion, shown as grey text at the cursor (feature 139, Copilot-style): one offer per open file,
// at the position it was asked for. Tab takes it, Escape (or typing on, or moving away) leaves it.
const offers = new Map();          // model uri -> { text, lineNumber, column }

function registerOffers(m) {
  m.languages.registerInlineCompletionsProvider('*', {
    provideInlineCompletions(model, position) {
      const offer = offers.get(model.uri.toString());
      if (!offer || offer.lineNumber !== position.lineNumber || offer.column !== position.column) return { items: [] };
      return { items: [{ insertText: offer.text,
        range: new m.Range(position.lineNumber, position.column, position.lineNumber, position.column) }] };
    },
    disposeInlineCompletions() {},
    freeInlineCompletions() {},
  });
}

// The file's language, by its name (feature 139: md, ps1, bat/cmd, js, py and the rest Monaco colours).
const BY_EXTENSION = {
  ps1: 'powershell', psm1: 'powershell', psd1: 'powershell', bat: 'bat', cmd: 'bat', md: 'markdown',
  markdown: 'markdown', js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
  ts: 'typescript', tsx: 'typescript', py: 'python', json: 'json', jsonc: 'json', yml: 'yaml', yaml: 'yaml',
  tf: 'hcl', tfvars: 'hcl', hcl: 'hcl', bicep: 'bicep', sql: 'sql', sh: 'shell', bash: 'shell', zsh: 'shell',
  xml: 'xml', csproj: 'xml', props: 'xml', targets: 'xml', config: 'xml', html: 'html', htm: 'html', css: 'css',
  cs: 'csharp', ini: 'ini', cfg: 'ini', conf: 'ini', toml: 'ini', go: 'go', java: 'java', rb: 'ruby', rs: 'rust',
  c: 'cpp', h: 'cpp', cpp: 'cpp', hpp: 'cpp', cc: 'cpp',
};

export function monacoLanguage(name) {
  const base = String(name || '').split(/[\\/]/).pop().toLowerCase();
  if (base === 'dockerfile' || base.startsWith('dockerfile.')) return 'dockerfile';
  const dot = base.lastIndexOf('.');
  return (dot >= 0 && BY_EXTENSION[base.slice(dot + 1)]) || 'plaintext';
}

const theme = () => (document.documentElement.dataset.theme === 'light' ? 'vs' : 'vs-dark');

// A textarea's surface over a Monaco editor.
export class MonacoBox {
  constructor(host, text, name, { compact = false } = {}) {
    this.host = host;
    this.dataset = host.dataset;
    // Newlines only, as the textarea had them: offsets count one character per line end, and the server puts the
    // file's own line endings (CRLF or LF) back on save, as it always has.
    this.editor = monaco.editor.create(host, {
      value: String(text || '').replace(/\r\n?/g, '\n'), language: monacoLanguage(name), theme: theme(), automaticLayout: true,
      minimap: { enabled: !compact }, scrollBeyondLastLine: false, fontSize: 13, tabSize: 4,
      renderWhitespace: 'selection', wordWrap: 'off', 'semanticHighlighting.enabled': false,
      ariaLabel: `Edit ${name}`, fixedOverflowWidgets: true,
    });
    this.model = this.editor.getModel();
    const self = this;
    // What older code and the tests use on the element: a textarea's value, selection and focus.
    Object.defineProperty(host, 'value', { configurable: true, get: () => self.value, set: (v) => { self.value = v; } });
    Object.defineProperty(host, 'selectionStart', { configurable: true, get: () => self.selectionStart });
    Object.defineProperty(host, 'selectionEnd', { configurable: true, get: () => self.selectionEnd });
    Object.defineProperty(host, 'scrollTop', { configurable: true, get: () => self.scrollTop, set: (v) => { self.scrollTop = v; } });
    host.setSelectionRange = (start, end) => self.setSelectionRange(start, end);
    host.focus = () => self.focus();
    host.classList.add('monaco-host');
    // A scroll position set before Monaco has a size (a tab drawn again) is applied once it has one.
    this.pendingTop = null;
    this.editor.onDidLayoutChange((info) => {
      if (info.height > 0 && this.pendingTop !== null) {
        this.editor.setScrollTop(this.pendingTop);
        this.pendingTop = null;
      }
    });
  }

  get alive() { return !this.model.isDisposed(); }

  get value() { return this.alive ? this.model.getValue() : ''; }

  set value(text) { if (this.alive) this.model.setValue(text); }

  get selectionStart() {
    return this.alive ? this.model.getOffsetAt(this.editor.getSelection().getStartPosition()) : 0;
  }

  get selectionEnd() {
    return this.alive ? this.model.getOffsetAt(this.editor.getSelection().getEndPosition()) : 0;
  }

  setSelectionRange(start, end) {
    if (!this.alive) return;
    const a = this.model.getPositionAt(start);
    const b = this.model.getPositionAt(end);
    this.editor.setSelection(new monaco.Selection(a.lineNumber, a.column, b.lineNumber, b.column));
  }

  // The selection scrolled to a third of the way down (feature 92's jump from the preview).
  revealOffsets(start, end) {
    this.setSelectionRange(start, end);
    this.editor.revealRangeNearTopIfOutsideViewport(this.editor.getSelection());
  }

  get scrollTop() { return this.editor.getScrollTop(); }

  set scrollTop(value) {
    if (this.editor.getLayoutInfo().height > 0) this.editor.setScrollTop(value);
    else this.pendingTop = value;
  }

  get scrollLeft() { return this.editor.getScrollLeft(); }

  set scrollLeft(value) { this.editor.setScrollLeft(value); }

  get clientHeight() { return this.editor.getLayoutInfo().height; }

  focus() { this.editor.focus(); }

  hasFocus() { return this.editor.hasTextFocus(); }

  addEventListener(type, handler) {
    const e = this.editor;
    const calls = {
      input: () => e.onDidChangeModelContent(() => handler(new Event('input'))),
      blur: () => e.onDidBlurEditorText(() => handler(new Event('blur'))),
      focus: () => e.onDidFocusEditorText(() => handler(new Event('focus'))),
      scroll: () => e.onDidScrollChange(() => handler(new Event('scroll'))),
      select: () => e.onDidChangeCursorSelection(() => handler(new Event('select'))),
      keyup: () => e.onKeyUp((k) => handler(k.browserEvent)),
      mouseup: () => e.onMouseUp((m) => handler(m.event.browserEvent)),
      keydown: () => e.onKeyDown((k) => handler(k.browserEvent)),
    };
    if (calls[type]) calls[type]();
  }

  dispatchEvent() { return true; }

  // Ctrl+S, and any other command, as Monaco binds keys.
  bindSave(run) {
    this.editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, run);
  }

  // Alt+\ asks for a suggestion, as VS Code's "trigger inline suggestion" does.
  bindSuggest(run) {
    this.editor.addCommand(monaco.KeyMod.Alt | monaco.KeyCode.Backslash, run);
  }

  // What the AI is shown: the text before and after the cursor, and where it is.
  cursorContext() {
    const position = this.editor.getPosition();
    const at = this.model.getOffsetAt(position);
    const text = this.model.getValue();
    return { position, line: position.lineNumber, before: text.slice(0, at), after: text.slice(at) };
  }

  // The suggestion offered at `position`: shown as grey text, gone once the text or the cursor moves on.
  offer(position, code) {
    const key = this.model.uri.toString();
    offers.set(key, { text: code, lineNumber: position.lineNumber, column: position.column });
    const clear = () => { offers.delete(key); listeners.forEach((l) => l.dispose()); };
    const listeners = [
      this.editor.onDidChangeModelContent(clear),
      this.editor.onDidChangeCursorPosition((e) => {
        if (e.position.lineNumber !== position.lineNumber || e.position.column !== position.column) clear();
      }),
    ];
    this.editor.setPosition(position);
    this.editor.focus();
    this.editor.trigger('aicleaner', 'editor.action.inlineSuggest.trigger', {});
  }

  dispose() {
    if (!this.alive) return;
    this.editor.dispose();
    this.model.dispose();
    // The element stays until the pane is drawn again, as the textarea did: its value reads empty meanwhile.
  }
}

// The light/dark switch (app.js sets data-theme on the page) reaches every open editor.
new MutationObserver(() => { if (monaco) monaco.editor.setTheme(theme()); })
  .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
