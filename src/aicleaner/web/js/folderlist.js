// f181 (Brad, 2026-11-15; spec specs/2026-11-15-f181-workspace-order.md): a list of paths shown as "pills" - a
// rectangular frame per path, a drag handle at its left and a remove (x) at its right, with move up / move down buttons
// for the keyboard and for touch, where a drag does not work. The paths live in a hidden text box, one per line, so
// what saves the profile, what tracks unsaved changes and the folder picker's "add a line" all work on it as before;
// the pills are drawn from it whenever it changes.
//
// Also the Files panel's roots, dragged among themselves (rootdrag below): the same move, the same rule - the first
// place is the home folder.
import { h } from './dom.js';

export const PILL_TYPE = 'application/x-aicleaner-pill';

// The list with the item at `from` put at `to` (its index once moved).
export function moved(list, from, to) {
  const out = list.slice();
  const [item] = out.splice(from, 1);
  out.splice(Math.max(0, Math.min(to, out.length)), 0, item);
  return out;
}

export const linesIn = (area) => area.value.split('\n').map((line) => line.trim()).filter(Boolean);

// Which gap a pointer at `y` is over, among `elements` stacked down the page: 0 is above the first, n below the last.
export function gapAt(elements, y) {
  let gap = 0;
  for (const element of elements) {
    const box = element.getBoundingClientRect();
    if (y > box.top + box.height / 2) gap += 1;
  }
  return gap;
}

// The move a drop in gap `gap` makes of the item at `from`: the index it ends up at.
export const targetOf = (from, gap) => (gap > from ? gap - 1 : gap);

const lastName = (path) => String(path).split(/[\\/]/).filter(Boolean).pop() || String(path);

// `area`: the hidden text box. Options:
//   fixed()            pills drawn first that never move: [{ path, label, title }]
//   badges(path, i)    small words shown on a pill ([] for none)
//   what               what a path is, in button labels ("folder", "workspace file")
//   movable            false: no handle, no up / down (one item at most)
//   badgeTitles        { badge: tooltip } for the badges
//   removeText         the remove button's word, in place of x ("Disconnect" for the workspace file, f183)
//   changed(change)    after a change made here: { kind: 'move' | 'remove', from, to, path, list, before }
export function pillList(area, opts = {}) {
  const what = opts.what || 'folder';
  const movable = opts.movable !== false;
  const list = h('ul', { class: `pill-list${movable ? '' : ' single'}`, 'aria-label': opts.label || null });
  let dragging = null;
  let focusAfter = null;

  const set = (next, change) => {
    const before = linesIn(area);
    area.value = next.join('\n');
    area.dispatchEvent(new Event('input', { bubbles: true }));
    if (opts.changed) opts.changed({ ...change, list: next, before });
  };

  const pill = (path, i, count) => {
    const name = lastName(path);
    const up = h('button', { type: 'button', class: 'pill-up', title: `Move ${name} up`, 'aria-label': `Move ${name} up`,
      disabled: i === 0 ? true : null, dataset: { index: String(i) } }, '▲');
    const down = h('button', { type: 'button', class: 'pill-down', title: `Move ${name} down`, 'aria-label': `Move ${name} down`,
      disabled: i === count - 1 ? true : null, dataset: { index: String(i) } }, '▼');
    const verb = opts.removeText || 'Remove';
    const remove = h('button', { type: 'button', class: `pill-remove${opts.removeText ? ' pill-remove-text' : ''}`,
      title: `${verb} ${name}`, 'aria-label': `${verb} ${name}`, dataset: { index: String(i) } }, opts.removeText || '×');
    const badges = (opts.badges ? opts.badges(path, i) : []).filter(Boolean);
    return h('li', { class: 'pill', draggable: movable ? 'true' : null, dataset: { index: String(i), path } },
      movable ? h('span', { class: 'pill-handle', title: 'Drag to move', 'aria-hidden': 'true' }, '⋮⋮') : null,
      ...badges.map((badge) => h('span', { class: `pill-badge badge-${badge.replace(/\W+/g, '-')}`,
        title: (opts.badgeTitles || {})[badge] || null }, badge)),
      h('span', { class: 'pill-path mono', title: path }, path),
      h('span', { class: 'pill-actions' }, movable ? up : null, movable ? down : null, remove));
  };

  const draw = () => {
    const paths = linesIn(area);
    const fixed = (opts.fixed ? opts.fixed() : []).filter((f) => f && f.path);
    list.replaceChildren(
      ...fixed.map((f) => h('li', { class: 'pill fixed', title: f.title || null, dataset: { path: f.path } },
        h('span', { class: 'pill-badge badge-assets' }, f.label), h('span', { class: 'pill-path mono', title: f.path }, f.path))),
      ...paths.map((path, i) => pill(path, i, paths.length)));
    if (focusAfter) {
      const [kind, index] = focusAfter;
      focusAfter = null;
      const target = list.querySelector(`li.pill:not(.fixed)[data-index="${index}"] .${kind}`);
      const fallback = list.querySelector(`li.pill:not(.fixed)[data-index="${index}"] button:not([disabled])`)
        || list.querySelector('li.pill:not(.fixed) button:not([disabled])');
      const chosen = target && !target.disabled ? target : fallback;
      if (chosen) chosen.focus();
    }
  };

  list.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button || !list.contains(button)) return;
    const paths = linesIn(area);
    const i = Number(button.dataset.index);
    if (!(i >= 0 && i < paths.length)) return;
    if (button.classList.contains('pill-remove')) {
      focusAfter = ['pill-remove', Math.min(i, paths.length - 2)];
      set(paths.filter((_p, k) => k !== i), { kind: 'remove', from: i, path: paths[i] });
      return;
    }
    const to = button.classList.contains('pill-up') ? i - 1 : i + 1;
    if (to < 0 || to >= paths.length) return;
    focusAfter = [button.classList.contains('pill-up') ? 'pill-up' : 'pill-down', to];
    set(moved(paths, i, to), { kind: 'move', from: i, to, path: paths[i] });
  });

  // Drag: a line shows where it will land (between two pills); the fixed pills are never a place to land above.
  const unmark = () => {
    for (const el of list.querySelectorAll('.drop-before, .drop-after')) el.classList.remove('drop-before', 'drop-after');
  };
  list.addEventListener('dragstart', (event) => {
    const li = event.target.closest && event.target.closest('li.pill:not(.fixed)');
    if (!li || !movable) return;
    dragging = Number(li.dataset.index);
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData(PILL_TYPE, li.dataset.path);
    li.classList.add('dragging');
  });
  list.addEventListener('dragover', (event) => {
    if (dragging === null || !event.dataTransfer || ![...event.dataTransfer.types].includes(PILL_TYPE)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    const pills = [...list.querySelectorAll('li.pill:not(.fixed)')];
    const gap = gapAt(pills, event.clientY);
    unmark();
    if (gap < pills.length) pills[gap].classList.add('drop-before');
    else if (pills.length) pills[pills.length - 1].classList.add('drop-after');
  });
  list.addEventListener('dragleave', (event) => { if (!list.contains(event.relatedTarget)) unmark(); });
  list.addEventListener('drop', (event) => {
    if (dragging === null || !event.dataTransfer || ![...event.dataTransfer.types].includes(PILL_TYPE)) return;
    event.preventDefault();
    const pills = [...list.querySelectorAll('li.pill:not(.fixed)')];
    const from = dragging;
    const to = targetOf(from, gapAt(pills, event.clientY));
    dragging = null;
    unmark();
    const paths = linesIn(area);
    if (to === from || !(from >= 0 && from < paths.length)) { draw(); return; }
    set(moved(paths, from, to), { kind: 'move', from, to, path: paths[from] });
  });
  list.addEventListener('dragend', () => { dragging = null; unmark(); for (const li of list.querySelectorAll('.dragging')) li.classList.remove('dragging'); });

  area.addEventListener('input', draw);
  list.redraw = draw;
  draw();
  return list;
}

// ---- the Files panel's roots, dragged among themselves (f181) ----------------------------------------------------
//
// Only a root's name is a handle (`.tree-root:not(.share) button.tree-root-name`, draggable); the drag carries its own
// type, so it is never mistaken for a file dropped from the desktop into a folder (filedrop.js answers only to 'Files')
// or for files dragged out to the conversation, the prompt or the desktop (treedrag.js skips roots). While it is over
// the panel a line shows the gap it will land in - between two roots, never on one, so it never reads as "into this
// folder". The project assets folder is fixed: no handle, and a drop above it lands first after it.
export const ROOT_TYPE = 'application/x-aicleaner-root';

// `move(fromPath, toIndex)`: the root at that path to that place among the movable roots.
export function attachRootDrag(panel, { move }) {
  let from = null;
  const heads = () => [...panel.querySelectorAll(':scope > .tree-root[data-movable="1"]')];
  const unmark = () => {
    for (const el of panel.querySelectorAll('.root-drop-before, .root-drop-after')) {
      el.classList.remove('root-drop-before', 'root-drop-after');
    }
  };
  panel.addEventListener('dragstart', (event) => {
    const button = event.target.closest && event.target.closest('.tree-root[data-movable="1"] > button.tree-root-name');
    if (!button) return;
    from = button.dataset.full;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData(ROOT_TYPE, from);
    event.dataTransfer.setData('text/plain', from);
  });
  panel.addEventListener('dragover', (event) => {
    if (from === null || !event.dataTransfer || ![...event.dataTransfer.types].includes(ROOT_TYPE)) return;
    const list = heads();
    if (!list.length) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    const gap = gapAt(list, event.clientY);
    unmark();
    if (gap < list.length) list[gap].classList.add('root-drop-before');
    else {
      const last = list[list.length - 1];
      const body = last.nextElementSibling;
      (body && body.classList.contains('tree-branch') && body.offsetHeight ? body : last).classList.add('root-drop-after');
    }
  });
  panel.addEventListener('dragleave', (event) => { if (!panel.contains(event.relatedTarget)) unmark(); });
  panel.addEventListener('drop', (event) => {
    if (from === null || !event.dataTransfer || ![...event.dataTransfer.types].includes(ROOT_TYPE)) return;
    event.preventDefault();
    const list = heads();
    const index = list.findIndex((head) => head.dataset.root === from);
    const gap = gapAt(list, event.clientY);
    const path = from;
    from = null;
    unmark();
    if (index < 0) return;
    const to = targetOf(index, gap);
    if (to !== index) move(path, to);
  });
  panel.addEventListener('dragend', () => { from = null; unmark(); });
}
