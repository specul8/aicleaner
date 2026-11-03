// Resizable table columns, remembered between sessions.
//
// A table opts in with data-resizable="<name>" and a data-col on each header cell. Nothing else is
// wired up: a MutationObserver fits out tables as they appear, because pages rebuild their tables
// after an action, on a poll and on a route change, and several do so without going through the
// router. Hooking the router would miss those; hooking each call site would mean remembering to do
// it in every table written from here on.

const PREFIX = 'aicleaner.columns.';
const MIN_WIDTH = 48;       // a column can be made narrow, never lost
const STEP = 16;            // arrow key
const FINE_STEP = 4;        // shift with arrow key

function load(name) {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFIX + name) || '{}');
    return saved && typeof saved === 'object' ? saved : {};
  } catch (e) {
    return {};               // private window, cleared data, policy: no saved widths is fine
  }
}

function save(name, widths) {
  try {
    localStorage.setItem(PREFIX + name, JSON.stringify(widths));
  } catch (e) {
    /* storage unavailable: the drag still worked, it just will not be remembered */
  }
}

function headers(table) {
  const row = table.tHead && table.tHead.rows[0];
  return row ? Array.from(row.cells) : [];
}

// One <col> per header cell. Widths live here rather than on the th so that a rule meant for one
// column cannot be read as a minimum for the cells below it.
function columnGroup(table, cells) {
  let group = table.querySelector(':scope > colgroup');
  if (!group) {
    group = document.createElement('colgroup');
    table.prepend(group);
  }
  while (group.children.length > cells.length) group.lastElementChild.remove();
  while (group.children.length < cells.length) group.append(document.createElement('col'));
  return Array.from(group.children);
}

// Switching a table to fixed layout changes how it looks, so it is done at the last possible
// moment and from the widths the automatic layout had already chosen. Doing it up front re-laid out
// every table that had been happily automatic - columns took an equal share whether their content
// wanted it or not, which moved cells out from under their own buttons. Pinning what is already on
// screen means the first drag moves one edge and nothing else.
function pin(state) {
  if (state.table.dataset.colsFixed) return;
  state.cells.forEach((cell, i) => {
    if (!state.cols[i].style.width) state.cols[i].style.width = `${cell.getBoundingClientRect().width}px`;
  });
  state.table.dataset.colsFixed = 'yes';
}

function applyWidths(state) {
  let any = false;
  state.cells.forEach((cell, i) => {
    const width = state.widths[cell.dataset.col];
    const wanted = Number.isFinite(width) && width >= MIN_WIDTH;
    state.cols[i].style.width = wanted ? `${width}px` : '';
    any = any || wanted;
  });
  if (any) pin(state);           // something was saved, so this table is already a sized one
}

function setWidth(state, index, pixels) {
  const key = state.cells[index].dataset.col;
  if (!key) return;
  pin(state);
  const width = Math.max(MIN_WIDTH, Math.round(pixels));
  state.widths[key] = width;
  state.cols[index].style.width = `${width}px`;
  save(state.name, state.widths);
}

function clearWidth(state, index) {
  const key = state.cells[index].dataset.col;
  if (!key) return;
  delete state.widths[key];
  state.cols[index].style.width = '';
  // The last width to go takes the pinning with it, so a table that has been fully reset looks
  // exactly as it did before anybody touched it rather than keeping the shape of the drag.
  if (!Object.keys(state.widths).length) {
    state.cols.forEach((col) => { col.style.width = ''; });
    delete state.table.dataset.colsFixed;
  }
  save(state.name, state.widths);
}

function grip(state, index) {
  const handle = document.createElement('span');
  handle.className = 'col-grip';
  handle.tabIndex = 0;
  handle.setAttribute('role', 'separator');
  handle.setAttribute('aria-orientation', 'vertical');
  handle.setAttribute('aria-label', `Resize ${state.cells[index].textContent.trim() || 'column'}`);
  handle.title = 'Drag to resize, double-click to reset';

  let startX = 0;
  let startWidth = 0;

  handle.addEventListener('pointerdown', (event) => {
    event.preventDefault();            // a drag on a header is not a text selection
    startX = event.clientX;
    startWidth = state.cells[index].getBoundingClientRect().width;
    handle.setPointerCapture(event.pointerId);
    handle.classList.add('dragging');
  });

  handle.addEventListener('pointermove', (event) => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    setWidth(state, index, startWidth + (event.clientX - startX));
  });

  const stop = (event) => {
    if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    handle.classList.remove('dragging');
  };
  handle.addEventListener('pointerup', stop);
  handle.addEventListener('pointercancel', stop);

  handle.addEventListener('dblclick', (event) => {
    event.preventDefault();
    clearWidth(state, index);
  });

  handle.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? FINE_STEP : STEP;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      const current = state.cells[index].getBoundingClientRect().width;
      setWidth(state, index, current + (event.key === 'ArrowRight' ? step : -step));
    } else if (event.key === 'Home') {
      event.preventDefault();
      clearWidth(state, index);
    }
  });

  return handle;
}


// ---- sorting -----------------------------------------------------------------------------
//
// Here rather than on the Clients page, for the same reason column widths are: this module owns
// "anything about how a table behaves, applied to every table that opts in", and five do. Built
// into the Clients page it would have to be built again for Review, which is the page where
// finding a row matters more (33).
//
// What a cell sorts BY is the cell's own business: data-sort when it has one, its text when it
// does not - so a date can sort by its timestamp and a count by its number while both read as
// people expect.
const SORT_PREFIX = 'aicleaner.sort.';

function sortKey(row, index) {
  const cell = row.children[index];
  if (!cell) return '';
  const declared = cell.dataset ? cell.dataset.sort : undefined;
  return (declared !== undefined ? declared : cell.textContent || '').trim();
}

function compare(a, b) {
  const na = Number(a);
  const nb = Number(b);
  if (a !== '' && b !== '' && !Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
}

function applySort(table, index, direction) {
  const body = table.tBodies[0];
  if (!body) return;
  // Only whole rows move. A row the page has put into an edit state keeps its place, because
  // reordering the row somebody is typing into is never what they asked for.
  const rows = [...body.rows].filter((r) => !r.classList.contains('editing'));
  rows.sort((x, y) => compare(sortKey(x, index), sortKey(y, index)) * (direction === 'desc' ? -1 : 1));
  rows.forEach((row) => body.append(row));
  for (const cell of headers(table)) {
    cell.removeAttribute('aria-sort');
    cell.classList.remove('sorted-asc', 'sorted-desc');
  }
  const cell = headers(table)[index];
  if (cell) {
    cell.setAttribute('aria-sort', direction === 'desc' ? 'descending' : 'ascending');
    cell.classList.add(direction === 'desc' ? 'sorted-desc' : 'sorted-asc');
  }
}

function rememberSort(name, index, direction) {
  try {
    localStorage.setItem(SORT_PREFIX + name, JSON.stringify({ index, direction }));
  } catch (e) {
    /* private window, cleared data: the sort still happened, it just will not be remembered */
  }
}

function recallSort(name) {
  try {
    const saved = JSON.parse(localStorage.getItem(SORT_PREFIX + name) || 'null');
    return saved && typeof saved.index === 'number' ? saved : null;
  } catch (e) {
    return null;
  }
}

function makeSortable(state) {
  state.cells.forEach((cell, index) => {
    // A table that already sorts itself keeps its own: the Review inbox has sort buttons in its
    // headers and a key per column, and two handlers on one heading would sort twice per click.
    // data-sortable="no" is the explicit opt-out for anything else.
    if (cell.dataset.sortable === 'no' || cell.dataset.sortReady) return;
    if (cell.dataset.sort !== undefined || cell.querySelector('button.sort')) return;
    cell.dataset.sortReady = 'yes';
    cell.tabIndex = 0;
    cell.classList.add('sortable');
    const turn = () => {
      const next = cell.classList.contains('sorted-asc') ? 'desc' : 'asc';
      applySort(state.table, index, next);
      rememberSort(state.name, index, next);
    };
    // Not the grip: a drag to resize would otherwise sort on release.
    cell.addEventListener('click', (event) => {
      if (event.target.closest('.col-grip')) return;
      turn();
    });
    cell.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); turn(); }
    });
  });
  const saved = recallSort(state.name);
  if (saved && saved.index < state.cells.length) applySort(state.table, saved.index, saved.direction);
}

export function fitOut(table) {
  const name = table.dataset.resizable;
  if (!name || table.dataset.colsReady === name) return;
  const cells = headers(table);
  if (!cells.length) return;
  const state = { name, table, cells, widths: load(name), cols: columnGroup(table, cells) };
  applyWidths(state);
  // The last column takes what is left, so the table always fills its container: a grip on it
  // would fight the one beside it and leave a ragged edge.
  cells.slice(0, -1).forEach((cell, i) => {
    if (!cell.dataset.col) return;
    cell.querySelectorAll(':scope > .col-grip').forEach((old) => old.remove());
    cell.append(grip(state, i));
  });
  makeSortable(state);
  table.dataset.colsReady = name;
}

function fitOutAll(root) {
  root.querySelectorAll('table[data-resizable]').forEach(fitOut);
}

export function watchTables(root) {
  fitOutAll(root);
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (!(node instanceof Element)) continue;
        if (node.matches('table[data-resizable]')) fitOut(node);
        else fitOutAll(node);
      }
    }
  }).observe(root, { childList: true, subtree: true });
}
