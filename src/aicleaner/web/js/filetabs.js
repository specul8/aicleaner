// File tabs on the Session page (feature 54, 74; DECISIONS 50).
//
// The conversation is the first tab and cannot be closed; each file opened from the tree's View
// button gets a tab of its own. Read-only for now. A file is shown as it is on disk - real names and
// all - because it goes to the person, not the AI service; the server holds it to the file tree's
// own allow-list.
//
// One state object per tab, so editing (a later release) adds fields rather than a rewrite:
//   { key, root, path, name, view, modified, readAt, stale, gone, data, mode }
// `stale` comes from the 15-second check today; a watcher will push the same answer later.

import { getJSON, postJSON, say, sayError } from './api.js';
import { h } from './dom.js';
import { renderMarkdown } from './markdown.js';
import { conflictBar, dropEditor, editorFor, elementOf, focusedEditor, saveChip, saveNow, unsaved } from './fileedit.js';
import { colourable, colourInto } from './editor/highlight.js';
import { languageOf } from './editor/languages.js';
import { attachPreviewJump } from './previewjump.js';
import { isSealed, objectUrlThrough } from './sealed.js';

const CHECK_EVERY_MS = 15000;

function fileQuery(name, tab) {
  return new URLSearchParams({ name, root: tab.root, path: tab.path }).toString();
}

// Through the encrypted tunnel a file opened by its own address cannot be sealed (feature 21), so
// it is not offered rather than offered and refused.

export function downloadLink(name, root, path, label = 'Download', attrs = {}) {
  // Through the tunnel the click is caught and the file fetched sealed (sealed.js, D168).
  const href = `/api/session/file/download?${new URLSearchParams({ name, root, path })}`;
  return h('a', { class: 'btn file-download', href, download: '', ...attrs }, label);
}

function bytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function when(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString() : '';
}

// The text with its line numbers, one scroll for both, so they never drift apart.
function textView(text, name) {
  const lines = text.split(/\r?\n/);
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  const gutter = h('pre', { class: 'file-gutter', 'aria-hidden': 'true' },
    lines.map((_, i) => String(i + 1)).join('\n'));
  const body = h('pre', { class: 'file-text' }, lines.join('\n'));
  // Coloured like the editor (feature 80, decided 2026-10-07), for the languages it knows.
  const language = languageOf(name);
  if (colourable(language, text)) colourInto(body, language, lines.join('\n'));
  return h('div', { class: 'file-scroll', tabindex: '0' }, gutter, body);
}

const INLINE_IMAGE = /\.(png|jpe?g|gif|webp|bmp|ico)$/i;

// A Markdown image path resolved as its reader would: beside the file, or from the folder root
// when it starts with '/'. The server holds the answer to the tabs' allow-list; a path that climbs
// above the root is refused here first so it is never asked for. SVG is not an inline type.
function localImage(name, tab) {
  return (url) => {
    let path;
    try { path = decodeURI(url.split(/[?#]/)[0]); } catch (error) { return null; }
    if (!INLINE_IMAGE.test(path)) return null;
    const parts = path.startsWith('/') ? [] : tab.path.split(/[\\/]/).slice(0, -1);
    for (const part of path.split(/[\\/]/)) {
      if (!part || part === '.') continue;
      if (part !== '..') parts.push(part);
      else if (!parts.length) return null;
      else parts.pop();
    }
    return viaTunnel(`/api/session/file/raw?${new URLSearchParams({ name, root: tab.root, path: parts.join('/') })}`);
  };
}

// Through the encrypted tunnel an image's address must never be requested as it is: the path in it
// would cross Cloudflare readable, before the tunnel turned the request away (D169). The element gets
// a blank picture marked with a key instead, and loadSealed fetches the real one sealed.
const BLANK_IMAGE = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
const sealedSources = new Map();
let sealedSeq = 0;

function viaTunnel(href) {
  if (!isSealed()) return href;
  sealedSeq += 1;
  const key = `aicleaner-sealed-${sealedSeq}`;
  sealedSources.set(key, href);
  return `${BLANK_IMAGE}#${key}`;
}

function loadSealed(root) {
  if (!isSealed() || !root) return;
  for (const img of root.querySelectorAll('img[src*="#aicleaner-sealed-"]')) {
    const key = img.getAttribute('src').split('#')[1];
    const href = sealedSources.get(key);
    sealedSources.delete(key);
    if (!href) continue;
    objectUrlThrough(href).then((url) => { img.src = url; })
      .catch(() => { img.alt = `${img.alt || 'Image'} (could not be loaded through the tunnel)`; });
  }
}

// Source and Preview, in the header row and only on a Markdown tab that has its text; per tab.
// The source view with [start, end] selected and scrolled to a third of the way down (feature 92).
function selectInSource(tab, start, end) {
  const box = tab.editor;
  if (!box) return;
  if (box.revealOffsets) { box.focus(); box.revealOffsets(start, end); return; }      // Monaco (feature 139)
  box.focus({ preventScroll: true });
  box.setSelectionRange(start, end);
  const line = box.value.slice(0, start).split('\n').length - 1;
  const lineHeight = parseFloat(getComputedStyle(box).lineHeight) || 18;
  box.scrollTop = Math.max(0, line * lineHeight - box.clientHeight / 3);
  box.dispatchEvent(new Event('scroll'));     // the gutter and the colouring follow
}

function markdownSwitch(tab, repaint) {
  const pick = (mode, label) => h('button', {
    type: 'button', class: `btn md-${mode}`, 'aria-pressed': String(tab.mode === mode),
    onclick: () => { tab.mode = mode; repaint(); },
  }, label);
  return h('span', { class: 'md-switch', role: 'group', 'aria-label': 'Markdown view' },
    pick('source', 'Source'), pick('preview', 'Preview'));
}

// `ctx`: { strip, pane, stream, name() } - the tab strip, the file pane, the transcript it stands in
// for, and the session being shown. Returns the handful of things session.js calls.
export function fileTabs(ctx) {
  const bySession = new Map();          // session name -> { tabs: [], active: key | null }
  let timer = null;

  const state = () => {
    const name = ctx.name();
    if (!bySession.has(name)) bySession.set(name, { tabs: [], active: null });
    return bySession.get(name);
  };
  const activeTab = () => { const s = state(); return s.tabs.find((t) => t.key === s.active) || null; };

  // Open tabs survive a refresh (D136): kept on the server per session, as the tree's open folders
  // are, and put back the first time a session is shown. Only where each file is and how it was
  // shown; its contents are read afresh, the active tab at once and the others when chosen.
  const restored = new Set();
  function persist() {
    const name = ctx.name();
    if (!name) return;
    const s = state();
    postJSON('/api/session/tabs-state', { name, active: s.active || '',
      tabs: s.tabs.map((t) => ({ root: t.root, path: t.path, name: t.name, view: t.view, mode: t.mode })) })
      .catch(() => { /* forgotten on refresh; the tabs themselves are fine */ });
  }
  async function restore(name) {
    let saved;
    try { saved = await getJSON(`/api/session/tabs-state?name=${encodeURIComponent(name)}`); } catch (error) { return; }
    const s = bySession.get(name) || { tabs: [], active: null };
    bySession.set(name, s);
    for (const t of saved.tabs || []) {
      const key = `${t.root}|${t.path}`;
      if (!s.tabs.some((x) => x.key === key)) {
        s.tabs.push({ key, root: t.root, path: t.path, name: t.name, view: t.view, modified: 0, stale: false,
          gone: false, data: null, mode: t.mode || 'source' });
      }
    }
    if (!s.active && saved.active) s.active = saved.active;
    if (ctx.name() !== name) return;
    const active = s.tabs.find((t) => t.key === s.active);
    if (active && !active.data) load(active);
    paint();
    if (s.tabs.length) startChecking();
  }

  function paintStrip() {
    const s = state();
    const conversation = h('button', { type: 'button', role: 'tab', class: 'file-tab conversation',
      'aria-selected': String(!s.active), dataset: { key: '' } }, 'Conversation');
    const tabs = s.tabs.map((tab) => h('span', {
      role: 'tab', class: `file-tab${tab.stale ? ' stale' : ''}${tab.gone ? ' gone' : ''}`,
      'aria-selected': String(s.active === tab.key), dataset: { key: tab.key },
      title: `${tab.root}${tab.root.includes('\\') ? '\\' : '/'}${tab.path}${tab.stale ? ' - changed on disk' : ''}${tab.gone ? ' - no longer there' : ''}`,
    },
    h('button', { type: 'button', class: 'file-tab-open', dataset: { key: tab.key } }, tab.name),
    h('button', { type: 'button', class: 'file-tab-close', dataset: { close: tab.key },
      'aria-label': `Close ${tab.name}` }, '×')));
    ctx.strip.replaceChildren(conversation, ...tabs);
    ctx.strip.hidden = !s.tabs.length;
  }

  function paintPane() {
    const tab = activeTab();
    ctx.stream.hidden = Boolean(tab);
    ctx.pane.hidden = !tab;
    if (!tab) { ctx.pane.replaceChildren(); return; }
    const name = ctx.name();
    const d = tab.data || {};
    const facts = [bytes(d.size || 0), `modified ${when(d.modified)}`, `read ${when(d.read_at)}`].join(' - ');
    const reload = h('button', { type: 'button', class: 'file-reload' }, tab.stale ? 'Reload (changed on disk)' : 'Reload');
    reload.addEventListener('click', () => load(tab));
    const markdown = !tab.error && d.kind === 'markdown' && typeof d.text === 'string';
    const head = h('div', { class: 'file-head' },
      markdown ? markdownSwitch(tab, () => { paintPane(); persist(); }) : null,
      h('span', { class: 'mono file-path', title: `${tab.root}` }, tab.path),
      h('span', { class: 'hint file-facts' }, facts),
      tab.gone ? h('span', { class: 'chip bad' }, 'No longer on disk') : null,
      tab.stale ? h('span', { class: 'chip warn' }, 'Changed on disk') : null,
      h('span', { class: 'file-actions' }, discussButton(), reload, downloadLink(name, tab.root, tab.path)));
    let body;
    if (tab.error) body = h('p', { class: 'error-text' }, tab.error);
    else if (!tab.data) body = h('p', { class: 'muted' }, 'Reading...');
    else if (d.download_only) body = h('p', { class: 'muted' }, `${tab.name} cannot be shown here. Download it to open it.`);
    else if (d.too_big) body = h('p', { class: 'muted' }, `${tab.name} is ${bytes(d.size)}, too big to show here. Download it to open it.`);
    else if (d.binary) body = h('p', { class: 'muted' }, `${tab.name} is not text. Download it to open it.`);
    else if (d.view === 'image') {
      body = h('div', { class: 'file-media' }, h('img', { src: viaTunnel(`/api/session/file/raw?${fileQuery(name, tab)}&t=${d.read_at}`), alt: tab.name }));
      loadSealed(body);
    }
    else if (d.view === 'pdf') {
      const src = `/api/session/file/raw?${fileQuery(name, tab)}&t=${d.read_at}`;
      // Through the tunnel, fetched sealed and shown from memory (D168); never the address itself.
      const frame = h('iframe', { title: tab.name, class: 'file-pdf' });
      const elsewhere = h('a', { href: isSealed() ? '#' : src, target: '_blank', rel: 'noopener noreferrer' }, 'Open it in a new browser tab');
      if (isSealed()) {
        objectUrlThrough(src).then((url) => { frame.src = url; elsewhere.href = url; })
          .catch((error) => frame.replaceWith(h('p', { class: 'error-text' }, error.message)));
      } else {
        frame.src = src;
      }
      body = h('div', { class: 'file-media pdf' }, frame, h('p', { class: 'hint' }, 'Not showing? ', elsewhere, '.'));
    } else if (markdown && tab.mode === 'preview') {
      // Preview renders what is in the editor, saved or not.
      const text = tab.editor ? tab.editor.value : d.text;
      body = h('div', { class: 'file-scroll md-preview-body', tabindex: '0' }, h('div', { class: 'md-doc' },
        renderMarkdown(text, { localImage: localImage(name, tab) })));
      loadSealed(body);
      // Double-click a word: the source, with that word selected (feature 92).
      attachPreviewJump(body, () => String(text || '').replace(/\r\n?/g, '\n'), (start, end) => {
        tab.mode = 'source';
        paintPane();
        persist();
        selectInSource(tab, start, end);
      });
    } else if (d.editable) body = editorFor(tab, hooks);
    else {
      body = textView(d.text || '', tab.name);
      // Say why it cannot be edited (the user, 2026-10-12, read an unexplained read-only view as a bug).
      if (d.editable === false) {
        head.append(h('span', { class: 'chip warn', title: 'Saving it here could change characters it holds. Convert it to UTF-8 to edit it here.' },
          'Read-only: not UTF-8 text'));
      }
    }
    head.append(...[saveChip(tab)].filter(Boolean));
    // The editor is reused, so moving it into the new pane must not lose the caret or fire a save.
    const editing = tab.editor && focusedEditor(tab.editor);
    const caret = editing ? [tab.editor.selectionStart, tab.editor.selectionEnd, tab.editor.scrollTop] : null;
    tab.repainting = true;
    try {
      // The editor already showing here stays where it is and only what is around it is redrawn
      // (D135): moving a textarea clears the browser's undo history, and every keystroke repaints
      // for the save chip, so Ctrl+Z had nothing to undo.
      const holder = body === tab.editorFrame && ctx.pane.contains(body) ? body.parentElement : null;
      if (holder && holder.parentElement === ctx.pane) {
        for (const child of [...ctx.pane.children]) if (child !== holder) child.remove();
        holder.before(...[conflictBar(tab, hooks), head].filter(Boolean));
        return;
      }
      ctx.pane.replaceChildren(...[conflictBar(tab, hooks), head, h('div', { class: 'file-body' }, body)].filter(Boolean));
      if (caret && body === tab.editorFrame) {
        tab.editor.focus();
        tab.editor.setSelectionRange(caret[0], caret[1]);
        tab.editor.scrollTop = caret[2];
      } else {
        putBack(tab);
      }
      follow(tab);
    } finally {
      tab.repainting = false;
    }
  }

  // Where the reader was in each tab (bug, Brad 2026-10-20: switching to a file tab always showed it
  // from line 1). Recorded as it changes - a scroll, a selection, the editor losing focus when another
  // tab is clicked - and put back whenever the tab is drawn again: from another file tab, from the
  // conversation, or from another session. Per view (source or preview), since their heights differ.
  function placeKey(tab) { return tab.mode === 'preview' ? 'preview' : 'source'; }

  function follow(tab) {
    const scroller = scrollerOf(tab);
    if (!scroller || scroller.dataset.followed) return;
    scroller.dataset.followed = '1';
    const record = () => {
      if (activeTab() !== tab || tab.repainting || !ctx.pane.contains(elementOf(scroller))) return;
      tab.place = tab.place || {};
      const box = tab.editor && scroller === tab.editor ? tab.editor : null;
      tab.place[placeKey(tab)] = { top: scroller.scrollTop, left: scroller.scrollLeft,
        caret: box ? [box.selectionStart, box.selectionEnd] : null };
    };
    scroller.addEventListener('scroll', record, { passive: true });
    if (scroller === tab.editor) {
      for (const type of ['select', 'keyup', 'mouseup', 'blur']) scroller.addEventListener(type, record);
    }
  }

  function putBack(tab) {
    const place = tab.place && tab.place[placeKey(tab)];
    const scroller = place ? scrollerOf(tab) : null;
    if (!scroller) return;
    if (place.caret && scroller === tab.editor) {
      const end = tab.editor.value.length;
      tab.editor.setSelectionRange(Math.min(place.caret[0], end), Math.min(place.caret[1], end));
    }
    scroller.scrollTop = place.top;
    scroller.scrollLeft = place.left;
    // A preview's images can grow it after it is drawn: once more when they have.
    if (scroller.querySelector && scroller.querySelector('img')) {
      requestAnimationFrame(() => { if (activeTab() === tab) scroller.scrollTop = place.top; });
    }
  }

  function paint() {
    const name = ctx.name();
    if (name && !restored.has(name)) { restored.add(name); restore(name); }
    paintStrip();
    paintPane();
  }

  // What the editor needs from here: whose files these are, a repaint, and a fresh read.
  const hooks = { name: () => ctx.name(), paint: () => paint(), reload: (tab) => load(tab),
    // 139 part 3: the file as it is on disk now, for the conflict's comparison (nothing in the tab changes).
    diskText: async (tab) => (await getJSON(`/api/session/file?${fileQuery(ctx.name(), tab)}`)).text || '' };

  async function load(tab) {
    tab.error = '';
    dropEditor(tab);                    // a fresh read starts a fresh editor
    tab.saveState = 'saved';
    try {
      tab.data = await getJSON(`/api/session/file?${fileQuery(ctx.name(), tab)}`);
      tab.modified = tab.data.modified;
      tab.stale = false;
      tab.gone = false;
    } catch (error) {
      tab.error = error && error.message ? error.message : String(error);
    }
    paint();
  }

  // Opened from the tree's View button. A file already open is focused, not opened twice.
  function open(root, path, name, view) {
    const s = state();
    const key = `${root}|${path}`;
    let tab = s.tabs.find((t) => t.key === key);
    if (!tab) {
      tab = { key, root, path, name, view, modified: 0, stale: false, gone: false, data: null, mode: 'source' };
      s.tabs.push(tab);
      load(tab);
    }
    s.active = key;
    paint();
    persist();
    startChecking();
  }

  async function close(key) {
    const s = state();
    const tab = s.tabs.find((t) => t.key === key);
    if (tab && tab.editor && tab.saveState !== 'saved') {
      await saveNow(tab, hooks);        // autosave: closing saves what is there first
      if (tab.saveState !== 'saved') {
        // Never lose edits by closing: the tab stays, and its bar says how to settle it.
        s.active = key;
        paint();
        say(`${tab.name} is not saved, so it stays open: ${tab.saveState === 'conflict'
          ? 'it changed on disk - choose Reload, Overwrite or Copy' : 'the save failed - try again'}.`, 'warn');
        return;
      }
    }
    s.tabs = s.tabs.filter((t) => t.key !== key);
    if (s.active === key) s.active = null;
    paint();
    persist();
  }

  // The 15-second changed-on-disk check: one request for every tab this session has open.
  async function check() {
    const s = state();
    if (!s.tabs.length || document.hidden) return;
    try {
      const answer = await postJSON('/api/session/file/stat', {
        name: ctx.name(), files: s.tabs.map((t) => ({ root: t.root, path: t.path })) });
      let changed = false;
      const fresh = [];
      for (const found of answer.files || []) {
        const tab = s.tabs.find((t) => t.root === found.root && t.path === found.path);
        if (!tab || !tab.data) continue;
        const gone = !found.exists;
        const stale = !gone && found.modified > tab.modified;
        // Nothing of the reader's to lose (the user, 2026-10-16): reload at once rather than offer
        // Reload. Edits in progress (unsaved, saving, conflict) keep the chip and the conflict bar.
        if (stale && (!tab.editor || (tab.saveState === 'saved' && !tab.saving))) { fresh.push(tab); continue; }
        if (gone !== tab.gone || stale !== tab.stale) { tab.gone = gone; tab.stale = stale; changed = true; }
      }
      if (changed) paint();
      for (const tab of fresh) await reloadInPlace(tab);
    } catch (error) { /* a missed check is caught by the next one */ }
  }

  // A fresh read that leaves the reader where they were: the scroll position, and in the editor
  // the caret and the focus, as near as the new text allows.
  function scrollerOf(tab) {
    if (tab.editor && ctx.pane.contains(elementOf(tab.editor))) return tab.editor;
    return ctx.pane.querySelector('.file-scroll') || ctx.pane.querySelector('.file-body');
  }

  async function reloadInPlace(tab) {
    const shown = activeTab() === tab ? scrollerOf(tab) : null;
    const box = tab.editor;
    const keep = shown ? {
      top: shown.scrollTop,
      caret: box ? [box.selectionStart, box.selectionEnd] : null,
      focused: Boolean(box) && focusedEditor(box),
    } : null;
    await load(tab);
    if (!keep || activeTab() !== tab) return;
    const again = scrollerOf(tab);
    if (again) again.scrollTop = keep.top;
    if (keep.caret && tab.editor) {
      const end = tab.editor.value.length;
      tab.editor.setSelectionRange(Math.min(keep.caret[0], end), Math.min(keep.caret[1], end));
      if (keep.focused) tab.editor.focus({ preventScroll: true });
    }
  }

  function startChecking() {
    if (!timer) timer = setInterval(check, CHECK_EVERY_MS);
  }

  ctx.strip.addEventListener('click', (event) => {
    const closer = event.target.closest('[data-close]');
    if (closer) { close(closer.dataset.close); return; }
    const pick = event.target.closest('[data-key]');
    if (!pick) return;
    const s = state();
    s.active = pick.dataset.key || null;
    // A tab put back after a refresh is read when it is first chosen.
    const chosen = s.tabs.find((t) => t.key === s.active);
    if (chosen && !chosen.data && !chosen.error) load(chosen);
    paint();
    persist();
  });

  // Leaving the page with an edit not yet saved (a refused save, or one still in flight): the
  // browser's own "leave this page?" - the one moment a page cannot show its own message.
  window.addEventListener('beforeunload', (event) => {
    if ([...bySession.values()].some((s) => unsaved(s.tabs))) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  // "Discuss this code" (feature 116): the lines selected in a file tab - in the editor or the
  // read-only view - become a snippet in the prompt, naming the file and its line numbers. A button
  // in the tab's header while there is a selection (seen on a computer and a touch screen alike, so
  // nobody has to know to right-click), and the same on right-click.
  function selected() {
    const tab = activeTab();
    if (!tab || !ctx.discuss || !tab.data || typeof tab.data.text !== 'string') return null;
    let text = '';
    let before = '';
    const box = tab.editor && ctx.pane.contains(elementOf(tab.editor)) ? tab.editor : null;
    if (box && box.selectionStart !== box.selectionEnd) {
      text = box.value.slice(box.selectionStart, box.selectionEnd);
      before = box.value.slice(0, box.selectionStart);
    } else {
      const body = ctx.pane.querySelector('.file-text');
      const sel = window.getSelection();
      if (!body || !sel || sel.isCollapsed || !sel.rangeCount || !body.contains(sel.anchorNode) || !body.contains(sel.focusNode)) return null;
      const range = sel.getRangeAt(0);
      text = range.toString();
      const lead = document.createRange();
      lead.selectNodeContents(body);
      lead.setEnd(range.startContainer, range.startOffset);
      before = lead.toString();
    }
    const code = text.replace(/\r\n?/g, '\n').replace(/\n+$/, '');
    if (!code.trim()) return null;
    const first = before.replace(/\r\n?/g, '\n').split('\n').length;
    return { code, lines: [first, first + code.split('\n').length - 1], file: tab.path.replace(/\\/g, '/'), name: tab.name };
  }

  function discuss() {
    const found = selected();
    closeDiscussMenu();
    if (found) ctx.discuss(found);
  }

  function discussButton() {
    if (!ctx.discuss) return null;
    const button = h('button', { type: 'button', class: 'btn file-discuss', hidden: true,
      title: 'Put the selected lines in the prompt as a code snippet, with the file and line numbers' }, 'Discuss this code');
    // Pressed before the click lands, so the selection it acts on is still there.
    button.addEventListener('mousedown', (event) => event.preventDefault());
    button.addEventListener('click', discuss);
    return button;
  }

  function showDiscuss() {
    const button = ctx.pane.querySelector('.file-discuss');
    if (button) button.hidden = !selected();
  }

  function closeDiscussMenu() {
    for (const menu of document.querySelectorAll('.file-discuss-menu')) menu.remove();
  }

  document.addEventListener('selectionchange', showDiscuss);
  ctx.pane.addEventListener('mouseup', showDiscuss);
  ctx.pane.addEventListener('keyup', showDiscuss);
  ctx.pane.addEventListener('select', showDiscuss, true);
  ctx.pane.addEventListener('contextmenu', (event) => {
    if (!selected()) return;                     // nothing selected: the browser's own menu
    event.preventDefault();
    closeDiscussMenu();
    const item = h('button', { type: 'button', class: 'file-discuss-item', role: 'menuitem' }, 'Discuss this code');
    item.addEventListener('mousedown', (e) => e.preventDefault());
    item.addEventListener('click', discuss);
    const menu = h('div', { class: 'file-discuss-menu', role: 'menu' }, item);
    menu.style.left = `${Math.round(event.clientX)}px`;
    menu.style.top = `${Math.round(event.clientY)}px`;
    document.body.append(menu);
  });
  document.addEventListener('mousedown', (event) => {
    if (!event.target.closest('.file-discuss-menu')) closeDiscussMenu();
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDiscussMenu(); });

  return {
    open,
    paint,
    check,
    active: activeTab,
    stop() { if (timer) clearInterval(timer); timer = null; },
    onError: sayError,
  };
}
