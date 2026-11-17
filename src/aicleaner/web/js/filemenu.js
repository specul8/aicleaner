// f178 (Brad, 2026-11-14; spec specs/2026-11-14-f178-files-menu.md): New file, Rename and Delete in the files panel's
// menu, and the name typed in place in the tree. f180 (2026-11-15): the menu opens on right-click only, one action per
// row, always with its words (the text-labels setting is gone); a slow second click on the chosen item renames it.
//
// The menu items are made here; the page (pages/session.js) draws the box where a name is typed and sends what it
// does to the server, which checks every place against the file tree's own allow-list again.

import { h } from './dom.js';

const icon = (glyph) => h('span', { 'aria-hidden': 'true' }, glyph);
// New file (a folder's menu, one item chosen), Rename (one item, not a root), Delete (one or all chosen, no root).
// `item` is the row the menu belongs to; `items` what the menu acts on (the selection, or that row alone).
export function changeItems(item, items) {
  const many = items.length > 1;
  const isRoot = (i) => !i.path;
  const made = [];
  if (!many && item.dir) {
    made.push(h('button', { type: 'button', class: 'tree-new-file', role: 'menuitem',
      'aria-label': `New file in ${item.name}`, title: 'Make an empty file in this folder; type its name in the tree',
      dataset: { root: item.root, path: item.path || '' } }, icon('📄'), ' New file'));
  }
  if (!many && !isRoot(item)) {
    made.push(h('button', { type: 'button', class: 'tree-rename', role: 'menuitem', 'aria-label': `Rename ${item.name}`,
      title: 'Rename this in place', dataset: { root: item.root, path: item.path, name: item.name } },
    icon('✏'), ' Rename'));
  }
  if (items.length && !items.some(isRoot)) {
    made.push(h('button', { type: 'button', class: 'tree-delete', role: 'menuitem',
      'aria-label': many ? `Delete ${items.length} items` : `Delete ${item.name}`,
      title: 'Move to the Recycle Bin (asks first)' }, icon('🗑'), many ? ` Delete (${items.length})` : ' Delete'));
  }
  return made;
}

// The question Delete asks, naming how many (and the one, when it is one).
export function deleteQuestion(items) {
  return items.length === 1 ? `Move ${items[0].name} to the Recycle Bin?`
    : `Move ${items.length} items to the Recycle Bin?`;
}

// The box a name is typed in: a new file's (at the top of its folder) or a rename's (in the row's place). `editing`
// is the page's record of it - what has been typed, and why the last try was refused.
export function nameBox(editing) {
  const input = h('input', { type: 'text', class: 'tree-edit-name', value: editing.value || '', spellcheck: 'false',
    autocomplete: 'off', 'aria-label': editing.mode === 'new' ? 'Name of the new file' : `New name for ${editing.name}`,
    title: 'Enter to save, Escape to cancel' });
  const error = editing.error ? h('span', { class: 'tree-edit-error', role: 'alert' }, editing.error) : null;
  return h('span', { class: 'tree-edit' }, editing.mode === 'new' ? icon('📄') : null, input, error);
}

// Rename: the name without its extension is selected the first time, as Explorer does.
export function selectStem(input) {
  const dot = input.value.lastIndexOf('.');
  input.setSelectionRange(0, dot > 0 ? dot : input.value.length);
}
