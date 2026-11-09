// Feature 120 (Brad, 2026-10-21): drag files from the Files panel - onto the conversation to open them in tabs,
// onto the prompt box to insert their paths.
//
// The prompt box takes the drag's plain text (the full paths, one per line, quoted where they hold a space)
// through the browser's own text drop: it lands where the pointer is and Ctrl+Z undoes it, as typing does.
// The conversation takes a private type that only this page sets, so a drop from Explorer is never mistaken for
// it (and the Files panel's own copy-into-a-folder drop answers only to real files).
//
// Feature 169 (Brad, 2026-11-09: "download 'from' any folder ... Drag and drop from the Files pane to the local
// machine"): one file, or one folder as a zip, dragged out of the page onto the desktop or a folder of the device
// the page is open on is saved there. Edge and Chrome only (`DownloadURL`; Firefox ignores it): the browser fetches
// the address itself, so it is AI Cleaner's own download route, same origin. Through the encrypted tunnel a plain
// address is turned away (D168) and a drag cannot wait for a sealed fetch, so there the drag carries no address and
// the Files menu's Download is the way to save a file.
import { h } from './dom.js';
import { isSealed } from './sealed.js';
import { isSelected, itemOf, selectedItems } from './treeselect.js';

export const DRAG_TYPE = 'application/x-aicleaner-files';

// `mime:filename:absolute URL`, Chromium's format, for one item - or '' when it cannot be offered (several items,
// a root, or a sealed page). A file goes as itself; a folder as the zip its Download makes.
export function downloadUrl(session, items, sealed = isSealed()) {
  if (sealed || !items || items.length !== 1) return '';
  const item = items[0];
  if (!item.root || !item.path) return '';
  const query = new URLSearchParams({ name: session || '', root: item.root, path: item.path });
  const dir = Boolean(item.dir) || item.kind === 'dir';
  const route = dir ? '/api/session/files/zip' : '/api/session/file/download';
  const filename = (dir ? `${item.name}.zip` : item.name).replace(/:/g, '_');
  return `${dir ? 'application/zip' : 'application/octet-stream'}:${filename}:${new URL(`${route}?${query}`, window.location.href).href}`;
}

const quoted = (path) => (/\s/.test(path) ? `"${path}"` : path);

// What a drag from this name button carries: the whole selection when the button is part of it, else itself.
export function draggedItems(button) {
  const item = itemOf(button);
  return isSelected(item.full) ? selectedItems() : [item];
}

export function pathsText(items) {
  return items.map((item) => quoted(item.full)).join('\n');
}

const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// Wires a drag source (the Files panel) and two targets: `conversation(target)` says whether an element is the
// conversation side, `prompt(target)` the prompt side; `open(items)` opens them as the menu's Open does;
// `session()` names the session, for the address a drag out of the page saves from (feature 169).
export function attachTreeDrag({ source, area, conversation, prompt, open, session = () => '' }) {
  const hint = h('div', { class: 'tree-drag-hint', hidden: true, role: 'status' });
  let dragging = null;
  let marked = null;
  const unmark = () => {
    if (marked) marked.classList.remove('drop-target');
    marked = null;
    hint.hidden = true;
  };
  const mark = (element, label, event) => {
    if (marked !== element) { unmark(); marked = element; element.classList.add('drop-target'); }
    if (!document.body.contains(hint)) document.body.append(hint);
    hint.textContent = label;
    hint.hidden = false;
    hint.style.left = `${event.clientX + 14}px`;
    hint.style.top = `${event.clientY + 14}px`;
  };
  source.addEventListener('dragstart', (event) => {
    const button = event.target.closest && event.target.closest('button.tree-name');
    if (!button || button.disabled) return;
    dragging = draggedItems(button);
    event.dataTransfer.effectAllowed = 'copy';
    event.dataTransfer.setData(DRAG_TYPE, JSON.stringify(dragging));
    event.dataTransfer.setData('text/plain', pathsText(dragging));
    const out = downloadUrl(session(), dragging);
    if (out) event.dataTransfer.setData('DownloadURL', out);
  });
  source.addEventListener('dragend', () => { dragging = null; unmark(); });
  area.addEventListener('dragover', (event) => {
    if (!dragging || !event.dataTransfer || ![...event.dataTransfer.types].includes(DRAG_TYPE)) return;
    const onConversation = conversation(event.target);
    const onPrompt = !onConversation && prompt(event.target);
    if (onConversation) {
      event.preventDefault();                       // ours to take
      event.dataTransfer.dropEffect = 'copy';
      const files = dragging.filter((item) => !item.dir).length;
      const folders = dragging.length - files;
      mark(onConversation, folders && !files ? `Open what is in ${count(folders, 'folder', 'folders')}`
        : `Open ${count(dragging.length, 'item', 'items')} in tabs`, event);
    } else if (onPrompt) {
      mark(onPrompt, `Insert ${count(dragging.length, 'path', 'paths')}`, event);   // the browser drops the text
    } else {
      unmark();
    }
  });
  area.addEventListener('dragleave', (event) => { if (!area.contains(event.relatedTarget)) unmark(); });
  area.addEventListener('drop', (event) => {
    if (!event.dataTransfer || ![...event.dataTransfer.types].includes(DRAG_TYPE)) return;
    const onConversation = conversation(event.target);
    unmark();
    if (!onConversation) return;                     // the prompt box: the browser inserts the paths
    event.preventDefault();
    let items = dragging;
    try { items = JSON.parse(event.dataTransfer.getData(DRAG_TYPE)); } catch (error) { /* the one we hold */ }
    dragging = null;
    if (items && items.length) open(items);
  });
}
