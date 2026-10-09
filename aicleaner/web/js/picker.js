// Choosing a folder on the machine the proxy runs on, from wherever you are reading the page.
//
// It shows only what folders.roots allows: a root and everything under it, plus the folders on the
// way down to one. Walking down from the drive is therefore not a disclosure - each step names
// only the child that leads somewhere allowed, never what else is beside it.
//
// The picker is a convenience, not a control. Every endpoint that takes a folder checks it again,
// so a typed path, a page left open since the list changed and a direct POST all meet the same
// answer. Nothing here is load-bearing for safety.

import { getJSON, postJSON, sayError } from './api.js';
import { h, icon } from './dom.js';

// A modal dialog with a tree (D114, the user, 2026-10-06). It opened inline under its button and
// grew and shrank with each folder, moving the page and pushing its own buttons off-screen; and it
// showed one level at a time, so opening a folder lost the place. Now: a fixed header saying what is
// being chosen and where you are, a fixed footer with the buttons, and between them a tree that
// expands in place like the Files panel and scrolls on its own.
//
// Options: `label` (the button), `title` (the dialog's heading; the label when absent), `append` (a
// list box gains a line), `value` (turns the chosen folder into what the box holds), `start` (where
// an empty box opens), `workspaceFiles` (choose one .code-workspace file, never a folder or another
// kind of file, and no New folder), `chooseLabel` (the footer's choose button).
export function folderPicker(input, opts = {}) {
  const button = h('button', { type: 'button', class: 'pick-folder', title: 'Browse folders on the proxy machine' },
    icon('archive'), opts.label || 'Browse');
  button.addEventListener('click', () => openPicker(input, opts));
  return button;
}

const listingOf = (path) => getJSON(`/api/folders?path=${encodeURIComponent(path || '')}`);
const samePath = (a, b) => String(a || '').toLowerCase() === String(b || '').toLowerCase();
// The depth as a CSS variable, set through the style object: the page's policy refuses style
// attributes in markup, not properties set from script.
const indent = (el, depth) => { el.style.setProperty('--depth', String(depth)); return el; };
const lastName = (path) => String(path).split(/[\\/]/).filter(Boolean).pop() || String(path);

async function openPicker(input, opts) {
  const files = Boolean(opts.workspaceFiles);
  const crumbs = h('nav', { class: 'picker-crumbs', 'aria-label': 'Where you are' });
  const tree = h('div', { class: 'folder-rows picker-tree', role: 'tree' });
  const here = h('span', { class: 'mono folder-here' });
  const choose = h('button', { type: 'button', class: 'primary folder-choose', disabled: true },
    opts.chooseLabel || (files ? 'Use this file' : 'Use this folder'));
  const newName = h('input', { type: 'text', class: 'folder-new-name', placeholder: 'New folder name',
    'aria-label': 'New folder name' });
  const make = h('button', { type: 'button', class: 'primary folder-make' }, 'Create');
  const newRow = h('div', { class: 'folder-new-row', hidden: true }, newName, make);
  const newFolder = files ? null : h('button', { type: 'button', class: 'folder-new', disabled: true }, 'New folder');
  const cancel = h('button', { type: 'button', class: 'folder-close' }, 'Cancel');
  const refused = h('p', { class: 'hint warn picker-refused', hidden: true, role: 'alert' });
  const dialog = h('dialog', { class: 'folder-picker picker-dialog', 'aria-label': opts.title || opts.label || 'Choose a folder' },
    h('header', { class: 'picker-head' }, h('h2', {}, opts.title || opts.label || 'Choose a folder'), crumbs),
    tree,
    h('footer', { class: 'picker-foot' },
      h('div', { class: 'picker-selected' }, h('span', { class: 'muted' }, files ? 'Selected file: ' : 'Selected: '), here),
      refused,
      newRow,
      h('div', { class: 'folder-foot' }, choose, newFolder, cancel)));
  document.body.append(dialog);
  const close = () => { dialog.close(); dialog.remove(); };
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
  cancel.addEventListener('click', close);

  // What is selected: a folder (its listing) or, for workspace files, a file path.
  let selected = { path: '', usable: false, file: '' };
  const select = (path, usable, file = '') => {
    selected = { path, usable, file };
    refused.hidden = true;
    for (const row of tree.querySelectorAll('.selected')) row.classList.remove('selected');
    const row = [...tree.querySelectorAll(file ? '.file-row' : '.folder-row')]
      .find((r) => samePath(file ? r.dataset.file : r.dataset.path, file || path));
    if (row) row.classList.add('selected');
    here.textContent = file || path;
    choose.disabled = files ? !file : !usable;
    choose.title = files && !file ? 'Select a workspace file first'
      : (!usable && !files ? 'This folder is only a step on the way to an allowed one' : '');
    if (newFolder) newFolder.disabled = !usable;
    paintCrumbs(path);
  };

  // Each breadcrumb opens that level: the top of what may be seen, down to the selected folder.
  let top = '';
  const paintCrumbs = (path) => {
    const parts = [];
    let at = path;
    while (at && !samePath(at, top) && at.length > top.length) {
      parts.unshift(at);
      at = at.replace(/[\\/][^\\/]+[\\/]?$/, '');
    }
    parts.unshift(top);
    crumbs.replaceChildren(...parts.flatMap((part, i) => [
      i ? h('span', { class: 'muted' }, ' › ') : null,
      h('button', { type: 'button', class: 'link picker-crumb', dataset: { path: part } }, i ? lastName(part) : part),
    ]).filter(Boolean));
  };

  // One folder's row and, once opened, its children under it.
  const rowFor = (entry, depth) => {
    const row = h('button', { type: 'button', role: 'treeitem', 'aria-expanded': 'false',
      class: `folder-row${entry.usable ? '' : ' step-only'}`, dataset: { path: entry.path },
      title: entry.usable ? entry.path : 'On the way to an allowed folder; open it to go further' },
    h('span', { class: 'picker-twisty' }, '▸'), icon('archive'), entry.name);
    row.usable = entry.usable;
    row.depth = depth;
    indent(row, depth);
    return h('div', { class: 'picker-node' }, row, h('div', { class: 'picker-children', hidden: true }));
  };

  // Opens a folder in place: its subfolders, then (for workspace files) its .code-workspace files.
  const expand = async (node, view) => {
    const row = node.querySelector(':scope > .folder-row');
    const kids = node.querySelector(':scope > .picker-children');
    let listing = view;
    if (!listing) {
      try { listing = await listingOf(row.dataset.path); } catch (error) { sayError(error); return null; }
    }
    const items = listing.entries.map((entry) => rowFor(entry, row.depth + 1));
    if (files && listing.usable) items.push(...await fileRows(listing.path, row.depth + 1));
    kids.replaceChildren(...items);
    if (!items.length) {
      kids.append(indent(h('p', { class: 'hint picker-empty indented' },
        files ? 'No folders or workspace files here.' : 'No folders in here.'), row.depth + 1));
    }
    kids.hidden = false;
    row.setAttribute('aria-expanded', 'true');
    row.querySelector('.picker-twisty').textContent = '▾';
    return listing;
  };
  const collapse = (node) => {
    const row = node.querySelector(':scope > .folder-row');
    node.querySelector(':scope > .picker-children').hidden = true;
    row.setAttribute('aria-expanded', 'false');
    row.querySelector('.picker-twisty').textContent = '▸';
  };

  const fileRows = async (folder, depth) => {
    let found = [];
    try {
      found = (await getJSON(`/api/session/workspace-files?folder=${encodeURIComponent(folder)}`)).files || [];
    } catch (error) { /* a folder that cannot be listed simply shows no files */ }
    return found.map((file) => indent(h('button', { type: 'button', class: 'file-row indented', role: 'treeitem',
      dataset: { file, folder }, title: file }, lastName(file)), depth));
  };

  // Drawn from the top of what may be seen, opened down to `path`.
  const reveal = async (path) => {
    const chain = [];
    try {
      let view = await listingOf(path);
      chain.unshift(view);
      while (view.parent) {
        view = await listingOf(view.parent);
        chain.unshift(view);
      }
    } catch (error) {
      if (path) return reveal('');
      tree.replaceChildren(h('p', { class: 'error-text' }, error && error.message ? error.message : String(error)));
      return;
    }
    top = chain[0].path;
    const level = chain[0];
    let parentKids = tree;
    tree.replaceChildren();
    const topItems = level.entries.map((entry) => rowFor(entry, 0));
    if (files && level.usable) topItems.push(...await fileRows(level.path, 0));
    tree.append(...topItems);
    if (!topItems.length) tree.append(h('p', { class: 'hint picker-empty' }, 'Nothing in here.'));
    for (let i = 1; i < chain.length; i += 1) {
      const node = [...parentKids.querySelectorAll(':scope > .picker-node')]
        .find((n) => samePath(n.querySelector('.folder-row').dataset.path, chain[i].path));
      if (!node) break;
      await expand(node, chain[i]);
      parentKids = node.querySelector(':scope > .picker-children');
    }
    const last = chain[chain.length - 1];
    select(last.path, last.usable);
    const row = tree.querySelector('.folder-row.selected');
    if (row) row.scrollIntoView({ block: 'nearest' });
  };

  tree.addEventListener('click', async (event) => {
    const file = event.target.closest('.file-row');
    if (file) { select(file.dataset.folder, true, file.dataset.file); return; }
    const row = event.target.closest('.folder-row');
    if (!row) return;
    const node = row.parentElement;
    const open = row.getAttribute('aria-expanded') === 'true';
    // A click selects the folder and opens it; a click on the open, selected one closes it.
    if (open && row.classList.contains('selected')) { collapse(node); return; }
    select(row.dataset.path, row.usable);
    if (!open) await expand(node);
    select(row.dataset.path, row.usable);
  });
  crumbs.addEventListener('click', (event) => {
    const crumb = event.target.closest('.picker-crumb');
    if (crumb) reveal(crumb.dataset.path);
  });
  if (newFolder) {
    newFolder.addEventListener('click', () => {
      newRow.hidden = !newRow.hidden;
      if (!newRow.hidden) newName.focus();
    });
    const create = async () => {
      try {
        const made = await postJSON('/api/folders/new', { parent: selected.path, name: newName.value });
        newName.value = '';
        newRow.hidden = true;
        await reveal(made.path);
      } catch (error) { sayError(error); }
    };
    make.addEventListener('click', create);
    newName.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); create(); } });
  }
  choose.addEventListener('click', () => {
    const chosen = files ? selected.file : selected.path;
    if (!chosen) return;
    // `refuse(path)` names why a choice is not wanted here (the share in a profile's folder list,
    // DECISIONS 64): said in the dialog, which stays open for another choice.
    const refusal = opts.refuse ? opts.refuse(chosen) : '';
    if (refusal) {
      refused.textContent = refusal;
      refused.hidden = false;
      return;
    }
    // A box that holds one folder is replaced; a list of them gains a line. Replacing a list
    // would throw away the folders already in it, which is not what Browse means.
    if (opts.append) {
      const lines = input.value.split('\n').map((line) => line.trim()).filter(Boolean);
      if (!lines.includes(chosen)) lines.push(chosen);
      input.value = lines.join('\n');
    } else {
      // `opts.value` turns the chosen folder into what the box holds - a repo NAME for the
      // primary repo field (D71), which is stored by name because its file is shared.
      input.value = opts.value ? opts.value(chosen) : chosen;
    }
    input.dispatchEvent(new Event('input', { bubbles: true }));
    close();
  });

  dialog.showModal();
  tree.replaceChildren(h('p', { class: 'hint' }, 'Loading folders...'));
  // Open where the box already points, so correcting a path does not start from the drive again.
  // A box that holds something other than a path (a repo name) opens at the first allowed
  // location; `start` names where an empty box opens (beside the home folder, D99).
  let start = '';
  if (opts.value) start = ((await folderRoots()).roots || [])[0] || '';
  else start = (input.value.trim().split('\n')[0] || '').trim() || (opts.start ? opts.start() : '');
  await reveal(start);
}

// Whether folders can be chosen at all, and whether this side may change the list. Cached for the
// life of the page: it is a setting, not a live reading, and every input asks for it.
let rootsPromise = null;

export function folderRoots(force) {
  if (force || !rootsPromise) rootsPromise = getJSON('/api/folders/roots').catch(() => ({ roots: [], editable: false }));
  return rootsPromise;
}

// Shown wherever a folder is chosen when nothing may be reached yet, so the answer is on the page
// that needs it rather than only in a refusal after the fact.
export function noFoldersYet(editable) {
  // The link opens Settings at the folders card rather than at the top of it. Naming the card was
  // not enough: it is two thirds of the way down one of two columns, and the first person to meet
  // this reported the setting as missing.
  return h('p', { class: 'hint' },
    editable
      ? ['No folders are allowed yet, so none can be chosen. ',
         h('a', { href: '#/settings/folders' }, 'Add one under Settings'),
         ', in "Folders the pages may reach".']
      : ['No folders are allowed yet. They are set on the machine the proxy runs on, under ',
         h('a', { href: '#/settings/folders' }, 'Settings'), '.']);
}
