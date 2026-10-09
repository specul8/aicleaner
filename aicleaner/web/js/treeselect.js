// Several files and folders at once in the Files tree (feature 97; spec
// docs/superpowers/specs/2026-10-08-f97-multi-select-files-design.md).
//
// Click selects one; Shift+click selects a range within one folder (in another folder it adds the
// one item); Ctrl/Cmd+click adds or removes one item anywhere; right-click opens the menu without
// opening or closing a folder. The menu then acts on everything selected. The selection lives here,
// keyed by full path, so it survives the tree redrawing; the rows only read it.

import { h } from './dom.js';

export const MANY_TABS = 20;          // more than this asks first
export const SEPARATE_DOWNLOADS = 5;  // up to this many files (and no folder) download one by one

const selection = { items: new Map(), anchor: null };

// What a row's name button stands for. Rows carry it in data attributes: full, root, path, parent
// (the folder listing it, `root|path`), kind (dir or file), view and name.
export function itemOf(button) {
  const d = button.dataset;
  return { full: d.full, root: d.root || '', path: d.path || '', parent: d.parent || '',
    dir: d.kind === 'dir' || button.classList.contains('dir'), view: d.view || '', name: d.name || d.full };
}

export const isSelected = (full) => selection.items.has(full);
export const selectedCount = () => selection.items.size;
export const selectedItems = () => [...selection.items.values()];

export function selectOnly(item) {
  selection.items = new Map([[item.full, item]]);
  selection.anchor = item;
}

// Returns whether anything was selected, so a caller redraws only when something changed.
export function clearSelection() {
  const had = selection.items.size > 0;
  selection.items.clear();
  selection.anchor = null;
  return had;
}

// Ctrl/Cmd: one item in or out, the rest kept. Returns whether it is now selected.
export function toggleOne(item) {
  selection.anchor = item;
  if (selection.items.has(item.full)) { selection.items.delete(item.full); return false; }
  selection.items.set(item.full, item);
  return true;
}

// Shift: from the anchor to this item within one folder, replacing that folder's earlier choice;
// in another folder (or with no anchor) it adds this item and becomes the anchor. `panel` gives the
// listing order, which is the order on screen.
export function selectRange(panel, item) {
  const anchor = selection.anchor;
  if (!anchor || anchor.parent !== item.parent || !item.parent) {
    selection.items.set(item.full, item);
    selection.anchor = item;
    return;
  }
  const rows = [...panel.querySelectorAll('button.tree-name')]
    .filter((b) => b.dataset.parent === item.parent && !b.disabled).map(itemOf);
  const from = rows.findIndex((r) => r.full === anchor.full);
  const to = rows.findIndex((r) => r.full === item.full);
  if (from < 0 || to < 0) { selection.items.set(item.full, item); return; }
  for (const [full, chosen] of selection.items) if (chosen.parent === item.parent) selection.items.delete(full);
  for (const row of rows.slice(Math.min(from, to), Math.max(from, to) + 1)) selection.items.set(row.full, row);
}

// The items a menu acts on: the whole selection when its own item is part of a larger one,
// otherwise that item alone (a search result, or a single click).
export function menuItems(item) {
  return isSelected(item.full) && selection.items.size > 1 ? selectedItems() : [item];
}

// The files a View acts on: chosen files a tab can show, and each chosen folder's OWN such files
// (not its subfolders' - the user, 2026-10-08). `ownFiles(item)` is the folder's listing entries as
// items, or null while not yet listed. Returns { files, skipped, pending }.
export function viewableFiles(items, ownFiles) {
  const files = [];
  let skipped = 0;
  let pending = false;
  const seen = new Set();
  const take = (file) => {
    if (seen.has(file.full)) return;
    seen.add(file.full);
    if (file.view && file.view !== 'download') files.push(file); else skipped += 1;
  };
  for (const item of items) {
    if (!item.dir) { take(item); continue; }
    const own = ownFiles(item);
    if (own === null) { pending = true; continue; }
    own.forEach(take);
  }
  return { files, skipped, pending };
}

const icon = (glyph) => h('span', { 'aria-hidden': 'true' }, glyph);

// The menu for several items, or for one folder (Prompt, View files, Download as a zip).
export function groupMenu(items, ownFiles, sealed) {
  const many = items.length > 1;
  const { files, pending } = viewableFiles(items, ownFiles);
  const label = many ? `${items.length} items` : items[0].name;
  const prompt = h('button', { type: 'button', class: 'tree-insert', role: 'menuitem',
    'aria-label': many ? `Put the paths of ${label} in the prompt` : `Put ${label} in the prompt`,
    title: many ? 'Put every path in the prompt, one per line' : 'Put this path in the prompt',
    dataset: { insert: items.map((i) => i.full).join('\n') } },
  ...labelled(icon('→'), ' Prompt', many ? `${items.length} paths` : ''));
  const viewCount = pending ? '...' : (many ? `${files.length} of ${countable(items, ownFiles)}` : String(files.length));
  const view = pending || files.length
    ? h('button', { type: 'button', class: 'tree-view-many', role: 'menuitem', title: 'Open each in a tab',
      'aria-label': `Open the files of ${label} in tabs` },
    ...(many ? labelled(icon('👁'), ' View', `${viewCount} files`) : [icon('👁'), ` View files (${viewCount})`]))
    : null;
  const zip = many ? needsZip(items) : true;
  // Works through the tunnel too, fetched sealed (D168); `sealed` only notes the size limit there.
  const download = h('button', { type: 'button', class: 'tree-download-many', role: 'menuitem',
    'aria-label': `Download ${label}`,
    title: (zip ? 'Download as one zip, each file under its folders' : 'Download each file')
      + (sealed ? ' (through the encrypted tunnel: up to 50 MB)' : '') },
  ...(many ? labelled(icon('⬇'), ' Download', `${items.length} ${zip ? 'as zip' : 'files'}`) : [icon('⬇'), ' Download (zip)']));
  return h('div', { class: 'tree-menu group', role: 'menu' }, prompt, view, download);
}

// A button's name, with what it covers on a second row beneath (the user, 2026-10-08: taller
// buttons rather than wider ones). No second row when there is nothing to add.
function labelled(mark, name, detail) {
  const top = h('span', { class: 'tree-menu-name' }, mark, name);
  return detail ? [top, h('span', { class: 'tree-menu-detail' }, detail)] : [top];
}

function countable(items, ownFiles) {
  let n = 0;
  for (const item of items) {
    if (!item.dir) { n += 1; continue; }
    n += (ownFiles(item) || []).length;
  }
  return n;
}

export const needsZip = (items) => items.length > SEPARATE_DOWNLOADS || items.some((i) => i.dir);
