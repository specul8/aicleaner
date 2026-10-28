// Start Work and Start Personal (feature 152, spec 2026-10-27-f152-start-work-personal.md).
//
// Two panels under the session picker. Each has its projects as chips, a "..." that chooses them (at most
// Sessions running at once - 1, so the set can start while another session is still finishing), and Go. A run is
// held by aicleaner, not by this page: the page only shows where it has got to, so it polls while one is going and
// picks it up again after a reload or a lost tunnel.

import { getJSON, postJSON, say, sayError } from './api.js';
import { h, tickRows } from './dom.js';
import { arrangeSheet } from './sheet.js';
import { keepChatInPlace } from './chatscroll.js';

const STATE_TEXT = {
  stopping: 'stopping',
  stopped: 'stopped',
  finishing: 'finishing (asked)',
  answer: 'waiting for your answer',
  waiting: 'waiting for a slot',
  starting: 'starting',
  started: 'started',
  running: 'already running',
  failed: 'failed',
};

const RUN_TEXT = { running: 'Going...', done: 'Done', cancelled: 'Cancelled', replaced: 'Replaced by another Go' };

const ACTIVE_POLL_MS = 1500;
const IDLE_POLL_MS = 15000;

function limitText(view) {
  if (view.limit === null || view.limit === undefined) {
    return 'Any number: Sessions running at once is 0 (no cap).';
  }
  return `Up to ${view.limit} - one fewer than Sessions running at once (${view.max_running}): one slot kept free so `
    + 'these can start while another finishes.';
}

function progressList(run) {
  return h('ul', { class: 'preset-progress' }, ...(run.items || []).map((item) => h('li', {
    class: `preset-item state-${item.state}`, dataset: { name: item.name, state: item.state } },
  h('span', { class: 'preset-item-name' }, item.name),
  h('span', { class: `preset-item-state${item.state === 'failed' ? ' warn' : ''}` }, STATE_TEXT[item.state] || item.state),
  item.reason ? h('span', { class: 'preset-item-reason muted' }, item.reason) : null)));
}

// The progress of the run this panel started, with Cancel while there is anything left for it to do.
function progressBox(view, preset) {
  const run = view.run;
  if (!run || run.preset !== preset.id) return null;
  const finishing = Object.keys(view.finishing || {}).length;
  const going = run.state === 'running' || finishing > 0;
  return h('div', { class: 'preset-run', dataset: { state: run.state } },
    h('div', { class: 'preset-run-head' },
      h('span', { class: 'preset-run-state' }, run.state === 'done' && finishing
        ? `Started - ${finishing} still finishing` : (RUN_TEXT[run.state] || run.state)),
      going ? h('button', { type: 'button', class: 'link preset-cancel' }, 'Cancel') : null),
    progressList(run),
    going ? h('p', { class: 'hint preset-restart-note' },
      'If aicleaner restarts, this ends: a session already asked to finish stays asked, but is not stopped for you.')
      : null);
}

function panel(view, preset) {
  const profiles = preset.profiles || [];
  return h('section', { class: 'card preset-panel', dataset: { preset: preset.id } },
    h('div', { class: 'preset-head' },
      h('h3', { class: 'preset-name' }, preset.name),
      h('button', { type: 'button', class: 'preset-more', title: `Choose the projects ${preset.name} starts`,
        'aria-label': `Configure ${preset.name}` }, '...')),
    profiles.length
      ? h('div', { class: 'preset-chips' }, ...profiles.map((name) => h('span', { class: 'chip preset-chip' }, name)))
      : h('p', { class: 'muted preset-empty' }, 'Choose projects with ...'),
    h('button', { type: 'button', class: 'primary preset-go', disabled: profiles.length ? null : true,
      title: profiles.length ? `Start ${profiles.join(', ')}; the other sessions stop once they are idle` : null }, 'Go'),
    progressBox(view, preset));
}

// The "..." dialog: the panel's name, and a tick for each project profile, at most `limit` ticked.
function openSettings(view, preset, saved) {
  const nameInput = h('input', { type: 'text', id: 'preset-name-input', maxlength: '40', value: preset.name,
    'aria-label': 'Panel name' });
  const chosen = new Set((preset.profiles || []).map((n) => n.toLowerCase()));
  const boxes = (view.profiles || []).map((name) => h('input', { type: 'checkbox', class: 'preset-pick row-tick',
    checked: chosen.has(name.toLowerCase()) ? true : null, dataset: { name }, 'aria-label': name }));
  const list = h('ul', { class: 'preset-pick-list' }, ...boxes.map((box) => h('li', { class: 'preset-pick-row' },
    box, h('span', {}, box.dataset.name))));
  tickRows(list, '.preset-pick-row', '.preset-pick');
  const count = h('span', { class: 'hint preset-count' }, '');
  const save = h('button', { type: 'button', class: 'preset-save' }, 'Save');
  const limit = view.limit === null || view.limit === undefined ? Infinity : view.limit;
  const show = () => {
    const ticked = boxes.filter((box) => box.checked).length;
    for (const box of boxes) box.disabled = !box.checked && ticked >= limit;
    count.textContent = limit === Infinity ? `${ticked} chosen` : `${ticked} of ${limit} chosen`;
  };
  list.addEventListener('change', show);
  const dialog = h('dialog', { id: 'preset-dialog', class: 'sheet preset-dialog', 'aria-label': `Configure ${preset.name}` },
    h('h2', {}, `Configure ${preset.name}`),
    h('label', { class: 'field' }, 'Name', nameInput),
    h('p', { class: 'hint preset-limit' }, limitText(view)),
    boxes.length ? list : h('p', { class: 'muted' }, 'No project profiles yet. Add one with Edit project profiles.'),
    count,
    h('div', { class: 'fb-footer' }, save));
  const close = () => { dialog.close(); dialog.remove(); };
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
  save.addEventListener('click', async () => {
    const edited = { id: preset.id, name: nameInput.value.trim() || preset.name,
      profiles: boxes.filter((box) => box.checked).map((box) => box.dataset.name) };
    save.disabled = true;
    try {
      const answer = await postJSON('/api/session/presets', {
        presets: (view.presets || []).map((p) => (p.id === preset.id ? edited : p)) });
      close();
      saved(answer);
    } catch (error) { save.disabled = false; sayError(error); }
  });
  document.body.append(dialog);
  arrangeSheet(dialog);
  show();
  dialog.showModal();
  nameInput.focus();
  return dialog;
}

// Mounts the two panels in `slot`. onRun(): called when a run changes the sessions, so the page can refresh.
export function mountPresets(slot, { onRun = () => {} } = {}) {
  let view = null;
  let timer = null;
  let stopped = false;
  let lastSig = '';

  const active = () => Boolean(view && (view.active || (view.run && view.run.state === 'running')));
  const draw = () => {
    if (!view || !slot.isConnected) return;
    const sig = JSON.stringify([view.presets, view.run, view.finishing, view.limit]);
    if (sig === lastSig) return;
    // A run moving sessions on: the page's own picker and transcript catch up.
    if (lastSig) onRun();
    lastSig = sig;
    // The panels sit above the chat band and arrive after the page may have scrolled, or be scrolling, to it.
    keepChatInPlace(() => {
      slot.replaceChildren(...(view.presets || []).map((preset) => panel(view, preset)));
      slot.hidden = !(view.presets || []).length;
    });
  };
  const schedule = () => {
    if (stopped) return;
    clearTimeout(timer);
    timer = setTimeout(load, active() ? ACTIVE_POLL_MS : IDLE_POLL_MS);
  };
  async function load() {
    try {
      view = await getJSON('/api/session/presets');
      draw();
    } catch (error) { /* switched off, or the link is down: tried again on the next tick */ }
    schedule();
  }
  const take = (answer) => { view = answer; draw(); schedule(); };

  slot.addEventListener('click', async (event) => {
    const host = event.target.closest('.preset-panel');
    if (!host || !view) return;
    const preset = (view.presets || []).find((p) => p.id === host.dataset.preset);
    if (!preset) return;
    if (event.target.closest('.preset-more')) {
      openSettings(view, preset, take);
      return;
    }
    const go = event.target.closest('.preset-go');
    if (go) {
      go.disabled = true;
      try {
        take(await postJSON('/api/session/preset/go', { id: preset.id }));
        say(`${preset.name}: going. Busy sessions are asked to finish first.`, 'ok');
      } catch (error) { go.disabled = false; sayError(error); }
      return;
    }
    if (event.target.closest('.preset-cancel')) {
      try { take(await postJSON('/api/session/preset/cancel', {})); } catch (error) { sayError(error); }
    }
  });

  load();
  return { refresh: load, stop: () => { stopped = true; clearTimeout(timer); } };
}
