// Suggest titles in its own dialog (E3 part 1, Brad 2026-10-19; feedback #3).
//
// Every conversation of the profile, newest first, each with a tick left of its title. At the top two
// ticks: All, and Unnamed - ticked when it opens, with the unnamed conversations ticked too. A row's
// click toggles its tick, as on Review (tickRows). The action titles exactly the ticked ones, a titled
// conversation included when ticked, through the same background queue and progress toast.

import { getJSON, postJSON, say, sayError } from './api.js';
import { h, tickRows } from './dom.js';
import { arrangeSheet } from './sheet.js';
import { watchTitles } from './titleprogress.js';

function when(seconds) {
  if (!seconds) return '';
  const date = new Date(seconds * 1000);
  return date.toLocaleDateString(undefined, { day: '2-digit', month: '2-digit' })
    + ' ' + date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

// name: the profile; onTitle: called as titles land (Continue reloads).
export async function openSuggestTitles(name, onTitle) {
  let rows;
  try {
    const answer = await getJSON(`/api/session/past?name=${encodeURIComponent(name)}&limit=5000`);
    rows = (answer.projects || []).filter((group) => !group.outside).flatMap((group) => group.sessions || []);
  } catch (error) { sayError(error); return; }
  rows.sort((a, b) => (b.updated || 0) - (a.updated || 0));
  if (!rows.length) { say('No conversations to title in this profile yet.', 'warn'); return; }

  const unnamed = (row) => !String(row.label || '').trim();
  const allTick = h('input', { type: 'checkbox', class: 'titles-all row-tick' });
  const unnamedTick = h('input', { type: 'checkbox', class: 'titles-unnamed row-tick', checked: true });
  const boxes = rows.map((row) => h('input', { type: 'checkbox', class: 'titles-pick row-tick', checked: unnamed(row) ? true : null,
    'aria-label': `Suggest a title for: ${row.label || row.title}`, dataset: { id: row.id } }));
  const list = h('ul', { class: 'titles-list' }, ...rows.map((row, i) => h('li', { class: 'titles-row' }, boxes[i],
    h('span', { class: 'titles-text' },
      h('span', { class: row.label ? 'titles-name' : 'titles-name muted' }, row.label || '(no title yet)'),
      h('span', { class: 'titles-first muted' }, ` - ${row.title}`),
      h('span', { class: 'titles-when muted' }, `  ${when(row.updated)}`)))));
  tickRows(list, '.titles-row', '.titles-pick');
  const go = h('button', { type: 'button', class: 'titles-go' }, 'Suggest titles');
  const ticked = () => boxes.filter((box) => box.checked);
  const show = () => {
    const count = ticked().length;
    go.textContent = `Suggest titles (${count})`;
    go.disabled = count === 0;
    // All and Unnamed say whether they still describe what is ticked.
    allTick.checked = count === boxes.length;
    unnamedTick.checked = count > 0 && boxes.every((box, i) => box.checked === unnamed(rows[i]));
  };
  allTick.addEventListener('change', () => { for (const box of boxes) box.checked = allTick.checked; show(); });
  unnamedTick.addEventListener('change', () => {
    boxes.forEach((box, i) => { if (unnamed(rows[i])) box.checked = unnamedTick.checked; });
    show();
  });
  list.addEventListener('change', show);
  const dialog = h('dialog', { id: 'suggest-titles', class: 'sheet titles-dialog', 'aria-label': 'Suggest titles' },
    h('h2', {}, 'Suggest titles'),
    h('p', { class: 'hint' }, 'The model reads each ticked conversation and writes it a title. A conversation that already has one is retitled only if you tick it; a title you wrote yourself is replaced only then too.'),
    h('div', { class: 'form-row titles-ticks' },
      h('label', { class: 'check' }, allTick, ' All'),
      h('label', { class: 'check' }, unnamedTick, ' Unnamed')),
    list,
    h('div', { class: 'fb-footer' }, go));
  const close = () => { dialog.close(); dialog.remove(); };
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
  go.addEventListener('click', async () => {
    const ids = ticked().map((box) => box.dataset.id);
    go.disabled = true;
    try {
      const answer = await postJSON('/api/session/conversations/suggest-titles', { name, ids });
      close();
      if (answer.started) watchTitles(onTitle, true);
      else if (answer.progress && answer.progress.running) watchTitles(onTitle);
      else say('Nothing to title.', 'ok');
    } catch (error) { go.disabled = false; sayError(error); }
  });
  document.body.append(dialog);
  arrangeSheet(dialog, { dirty: () => false });
  show();
  dialog.showModal();
  return dialog;
}
