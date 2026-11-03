// Dragging files and folders into the file tree (2026-10-16, item H2; spec 2026-10-16-h2-drag-drop-files-design.md).
//
// A browser hands over what was dropped, never where it came from, so a drop is a copy: each file is
// uploaded into the folder it was dropped on (a file row means its folder), folders walked and
// recreated, empty ones too. One question covers every name already there; a bar shows the copying,
// and Cancel stops after the file being sent.
import { postJSON, say, sayError } from './api.js';
import { h } from './dom.js';
import { sendInPieces } from './pieces.js';
import { arrangeSheet } from './sheet.js';

// The folder a row stands for: a folder row itself, a file row its folder.
function folderOf(row) {
  const button = row && row.querySelector('button.tree-name');
  if (!button || button.disabled) return null;
  const { root, path = '', kind, name } = button.dataset;
  if (!root) return null;
  if (kind === 'dir') return { root, path, label: name || path || root };
  const cut = path.lastIndexOf('/');
  const parent = cut >= 0 ? path.slice(0, cut) : '';
  return { root, path: parent, label: parent ? parent.split('/').pop() : root };
}

function readEntries(reader) {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

// Every file and folder under the dropped items, as {relative, file} and {relative, dir: true}.
async function collect(dataTransfer) {
  const found = [];
  const walk = async (entry, prefix) => {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isFile) {
      found.push({ relative, file: await new Promise((resolve, reject) => entry.file(resolve, reject)) });
    } else if (entry.isDirectory) {
      found.push({ relative, dir: true });
      const reader = entry.createReader();
      for (let batch = await readEntries(reader); batch.length; batch = await readEntries(reader)) {
        for (const child of batch) await walk(child, relative);
      }
    }
  };
  const items = [...(dataTransfer.items || [])].filter((item) => item.kind === 'file');
  const entries = items.map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null));
  if (entries.length && entries.every(Boolean)) {
    for (const entry of entries) await walk(entry, '');
  } else {
    for (const file of dataTransfer.files || []) found.push({ relative: file.name, file });
  }
  return found;
}

// Replace, Keep both, Skip them or Cancel, for the whole drop. Resolves the mode, or null.
function askClashes(clashes) {
  return new Promise((resolve) => {
    const choose = (mode) => { dialog.close(); dialog.remove(); resolve(mode); };
    const button = (label, mode, primary = false) => {
      const b = h('button', { type: 'button', class: `${primary ? 'primary ' : ''}drop-${mode || 'cancel'}` }, label);
      b.addEventListener('click', () => choose(mode));
      return b;
    };
    const shown = clashes.slice(0, 12);
    const dialog = h('dialog', { id: 'drop-clash', class: 'sheet confirm-dialog drop-dialog', 'aria-label': 'Names already there' },
      h('p', {}, `${clashes.length} of these ${clashes.length === 1 ? 'is' : 'are'} there already:`),
      h('ul', { class: 'drop-clashes mono' }, ...shown.map((name) => h('li', {}, name)),
        clashes.length > shown.length ? h('li', {}, `and ${clashes.length - shown.length} more`) : null),
      h('div', { class: 'fb-footer' }, button('Cancel', null),
        h('span', {}, button('Skip them', 'skip'), ' ', button('Keep both', 'keep'), ' ', button('Replace', 'replace', true))));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); choose(null); });
    document.body.append(dialog);
    arrangeSheet(dialog, { dirty: () => false });
    dialog.showModal();
  });
}

function progressSheet(total) {
  const bar = h('progress', { max: String(total || 1), value: '0' });
  const text = h('p', { class: 'drop-progress-text' }, 'Starting...');
  const stop = h('button', { type: 'button', class: 'drop-stop' }, 'Cancel');
  const dialog = h('dialog', { id: 'drop-progress', class: 'sheet confirm-dialog drop-dialog', 'aria-label': 'Copying' },
    text, bar, h('div', { class: 'fb-footer' }, stop));
  const sheet = { stopped: false, done: () => { dialog.close(); dialog.remove(); } };
  sheet.show = (n, name) => { bar.value = n; text.textContent = `Copying ${n} of ${total} - ${name}`; };
  stop.addEventListener('click', () => { sheet.stopped = true; stop.disabled = true; stop.textContent = 'Stopping after this file...'; });
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); stop.click(); });
  document.body.append(dialog);
  arrangeSheet(dialog, { dirty: () => true });        // copying: a stray click behind must not stop it
  dialog.showModal();
  return sheet;
}

async function copyInto(name, target, found) {
  const spot = { name, root: target.root, path: target.path };
  const items = found.map((f) => (f.dir ? { relative: f.relative, dir: true } : { relative: f.relative, size: f.file.size }));
  const checked = await postJSON('/api/session/drop/check', { ...spot, items });
  if ((checked.problems || []).length) {
    say(`Nothing was copied: ${checked.problems.slice(0, 3).join('; ')}`, 'warn');
    return null;
  }
  let mode = 'replace';
  if ((checked.clashes || []).length) {
    mode = await askClashes(checked.clashes);
    if (!mode) return null;
  }
  const files = found.filter((f) => !f.dir);
  const withFiles = new Set(files.map((f) => f.relative.split('/').slice(0, -1).join('/')));
  const holding = (folder) => [...withFiles].some((d) => d === folder || d.startsWith(`${folder}/`));
  const sheet = progressSheet(files.length);
  let copied = 0;
  let skipped = 0;
  try {
    for (const [index, item] of files.entries()) {
      if (sheet.stopped) break;
      sheet.show(index + 1, item.relative);
      const answer = await sendInPieces('/api/session/drop/file', { ...spot, relative: item.relative, mode }, item.file);
      if (answer && answer.skipped) skipped += 1; else copied += 1;
    }
    if (!sheet.stopped) {
      for (const folder of found.filter((f) => f.dir && !holding(f.relative))) {
        await postJSON('/api/session/drop/folder', { ...spot, relative: folder.relative });
      }
    }
  } finally { sheet.done(); }
  return { copied, skipped, stopped: sheet.stopped, total: files.length };
}

// Wires the drop to the Files panel. `name()` is the session shown; `dropped(target)` is called after
// a copy, to open that folder and redraw the tree.
export function attachFileDrop(panel, { name, dropped }) {
  // Put back on each drag: the tree redraws by replacing the panel's children.
  const hint = h('div', { class: 'drop-hint', hidden: true });
  let over = null;
  const clear = () => {
    if (over) over.classList.remove('drop-over');
    over = null;
    hint.hidden = true;
  };
  panel.addEventListener('dragover', (event) => {
    if (!event.dataTransfer || ![...event.dataTransfer.types].includes('Files')) return;
    const row = event.target.closest('.tree-row, .tree-root');
    const target = folderOf(row);
    if (!target || !name()) { clear(); return; }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    if (over !== row) {
      clear();
      over = row;
      row.classList.add('drop-over');
    }
    if (!panel.contains(hint)) panel.append(hint);
    hint.textContent = `Drop to copy into ${target.label}`;
    hint.hidden = false;
  });
  panel.addEventListener('dragleave', (event) => { if (!panel.contains(event.relatedTarget)) clear(); });
  panel.addEventListener('drop', async (event) => {
    const target = folderOf(event.target.closest('.tree-row, .tree-root'));
    clear();
    if (!target || !name()) return;
    event.preventDefault();
    try {
      const found = await collect(event.dataTransfer);
      if (!found.length) return;
      const result = await copyInto(name(), target, found);
      if (!result) return;
      const where = target.path ? `${target.root}/${target.path}` : target.root;
      const skippedText = result.skipped ? `, ${result.skipped} skipped` : '';
      say(result.stopped
        ? `Stopped: ${result.copied} of ${result.total} copied into ${where}${skippedText}; those stay.`
        : `Copied ${result.copied} file${result.copied === 1 ? '' : 's'} into ${where}${skippedText}.`, 'ok');
      dropped(target);
    } catch (error) { sayError(error); }
  });
}
