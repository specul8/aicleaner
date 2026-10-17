// Finding a conversation by topic (2026-10-16; spec 2026-10-16-conversation-search-design.md).
//
// A dialog over the Session page: type, and the profile's conversations come back ranked, each with
// the passages that match and the words marked. "Let AI rank these" asks the model to put the best
// first and say why. Choosing a card hands it to the page, which selects it in the Continue list.
// Passages are the person's own transcripts (real names, as the file tabs show them) and are set as
// text, never as HTML.

import { getJSON, postJSON } from './api.js';
import { h } from './dom.js';

const AI_KEY = 'aicleaner-conversation-search-ai';
const WAIT_MS = 300;

function remembered() {
  try { return localStorage.getItem(AI_KEY) === '1'; } catch (error) { return false; }
}

function remember(on) {
  try { localStorage.setItem(AI_KEY, on ? '1' : '0'); } catch (error) { /* not remembered */ }
}

function when(seconds) {
  if (!seconds) return '';
  const at = new Date(seconds * 1000);
  const two = (n) => String(n).padStart(2, '0');
  const time = `${two(at.getHours())}:${two(at.getMinutes())}`;
  return at.toDateString() === new Date().toDateString() ? `today ${time}` : `${two(at.getDate())}/${two(at.getMonth() + 1)}/${at.getFullYear()} ${time}`;
}

// A passage with its matches in <mark>, built as text nodes.
function marked(passage) {
  const parts = [];
  let at = 0;
  for (const [start, end] of passage.marks || []) {
    if (start > at) parts.push(passage.text.slice(at, start));
    parts.push(h('mark', {}, passage.text.slice(start, end)));
    at = end;
  }
  parts.push(passage.text.slice(at));
  return h('p', { class: 'conv-passage' }, h('span', { class: 'conv-who' }, `${passage.role} ${when(passage.at)}`), ' ', ...parts);
}

function card(result, index, several, picked, verb) {
  const folder = several ? result.project.split(/[\\/]/).filter(Boolean).pop() : '';
  const head = [when(result.updated), result.branch ? `[${result.branch}]` : '', folder].filter(Boolean).join('  ');
  // A tick to act on it with others (consolidate, item F; delete); a click on it never opens the card.
  // One that is running or busy cannot be deleted, so it cannot be ticked for that.
  const locked = verb === 'Delete' && result.busy;
  const pick = verb ? h('input', { type: 'checkbox', class: 'conv-pick', 'aria-label': `Choose to ${verb.toLowerCase()}`,
    checked: picked && !locked ? true : null, disabled: locked ? true : null, title: locked ? 'Running or busy: it cannot be deleted now' : null,
    dataset: { id: result.id } }) : null;
  const count = typeof result.matches === 'number'
    ? `${result.matches} ${result.matches === 1 ? 'match' : 'matches'} in ${result.turns_matched} ${result.turns_matched === 1 ? 'turn' : 'turns'}`
    : '';
  return h('li', { class: 'conv-card', role: 'option', tabindex: '-1', dataset: { index: String(index) } },
    h('div', { class: 'conv-head muted' }, h('span', {}, pick, ' ', head), h('span', { class: 'conv-count' }, count)),
    result.label ? h('div', { class: 'conv-label' }, result.label) : null,
    h('div', { class: result.label ? 'conv-title muted' : 'conv-title' }, result.label ? `first: ${result.title}` : result.title),
    result.last && result.last !== result.title ? h('div', { class: 'conv-last muted' }, `latest: ${result.last}`) : null,
    result.why ? h('div', { class: 'conv-why' }, result.why) : null,
    ...(result.passages || []).map(marked));
}

// opts: { name: () => profile shown, choose(result), initial: rows shown while nothing is typed (optional),
//         action: { verb, min, run(results) } (optional) - ticks on the cards, an All tick, and a button
//         that acts on the ticked ones: Consolidate (item F, 2 or more) or Delete (1 or more) }
export function openConversationSearch(opts) {
  const input = h('input', { type: 'search', class: 'conv-query', placeholder: 'What was it about? e.g. code signing certificates',
    'aria-label': 'Search conversations', autocomplete: 'off' });
  const scope = h('select', { class: 'conv-scope', 'aria-label': 'Where to search' },
    h('option', { value: 'profile' }, 'This project profile'), h('option', { value: 'all' }, 'All my profiles'));
  const ai = h('label', { class: 'check conv-ai', title: 'The top matches go to the AI service, filtered like every request, to put the best first and say why' },
    h('input', { type: 'checkbox', checked: remembered() ? true : null }), ' Let AI rank these');
  const status = h('p', { class: 'hint conv-status' }, 'Type what the conversation was about.');
  const list = h('ul', { class: 'conv-results', role: 'listbox', 'aria-label': 'Matching conversations' });
  const close = h('button', { type: 'button', class: 'conv-close' }, 'Close');
  const action = opts.action || null;
  const verb = action ? action.verb : '';
  const actButton = h('button', { type: 'button', class: `primary conv-act conv-${verb.toLowerCase() || 'none'}`, hidden: true }, verb);
  // Every card shown ticked, or none (the user, 2026-10-17).
  const all = action ? h('label', { class: 'check conv-all' }, h('input', { type: 'checkbox', class: 'conv-all-tick' }), ' All') : null;
  const dialog = h('dialog', { id: 'conv-search', class: 'sheet conv-search', 'aria-label': 'Search conversations' },
    h('h2', {}, verb ? `${verb} conversations` : 'Search conversations'),
    h('div', { class: 'form-row conv-bar' }, input, scope, ai),
    status, all, list, h('div', { class: 'fb-footer' }, close, actButton));
  document.body.append(dialog);

  const state = { results: [], active: 0, timer: null, asked: 0, picked: new Map() };
  const tickable = () => state.results.filter((r) => !(verb === 'Delete' && r.busy));
  const showPicked = () => {
    actButton.hidden = !action || state.picked.size < action.min;
    actButton.textContent = `${verb} (${state.picked.size})`;
    if (all) {
      const box = all.querySelector('input');
      const shown = tickable();
      const ticked = shown.filter((r) => state.picked.has(r.id)).length;
      box.checked = shown.length > 0 && ticked === shown.length;
      box.indeterminate = ticked > 0 && ticked < shown.length;
      all.hidden = !shown.length;
    }
  };
  const draw = () => {
    const several = new Set(state.results.map((r) => r.project)).size > 1;
    list.replaceChildren(...state.results.map((result, i) => card(result, i, several, state.picked.has(result.id), verb)));
    highlight();
    showPicked();
  };
  // With nothing typed: the conversations the page was given (Consolidate... from Continue).
  const showInitial = () => {
    state.results = opts.initial || [];
    draw();
    const how = action ? `Tick ${action.min > 1 ? `${action.min} or more` : 'the ones'} to ${verb.toLowerCase()}, or type to search.` : '';
    status.textContent = state.results.length && how ? how : 'Type what the conversation was about.';
  };
  const highlight = () => {
    list.querySelectorAll('.conv-card').forEach((li, i) => {
      li.classList.toggle('active', i === state.active);
      li.setAttribute('aria-selected', String(i === state.active));
    });
    const active = list.querySelector('.conv-card.active');
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
  };
  const finish = () => { dialog.close(); dialog.remove(); };
  const choose = (index) => {
    const result = state.results[index];
    if (!result) return;
    finish();
    opts.choose(result);
  };

  const rank = async (asked, query) => {
    const top = state.results.slice(0, 15).map((r) => ({ id: r.id, title: r.title, passages: r.passages }));
    status.textContent = `Asking the AI to rank the top ${top.length}...`;
    try {
      const answer = await postJSON('/api/session/conversations/rank', { name: opts.name(), q: query, results: top });
      if (asked !== state.asked) return;
      const byId = new Map(state.results.map((r) => [r.id, r]));
      const ranked = (answer.ranked || []).map((row) => ({ ...byId.get(row.id), why: row.why })).filter((r) => r.id);
      const rest = state.results.filter((r) => !ranked.some((x) => x.id === r.id));
      state.results = [...ranked, ...rest];
      state.active = 0;
      draw();
      status.textContent = `${ranked.length} chosen by the AI, best first; ${rest.length} more below.`;
    } catch (error) {
      if (asked !== state.asked) return;
      status.textContent = `The AI ranking failed (${error.message || error}); shown in the local order.`;
    }
  };

  const run = async () => {
    clearTimeout(state.timer);
    state.timer = null;
    const query = input.value.trim();
    const asked = state.asked + 1;
    state.asked = asked;
    if (!query) { showInitial(); return; }
    status.textContent = 'Searching...';
    try {
      const answer = await getJSON(`/api/session/conversations/search?name=${encodeURIComponent(opts.name())}&q=${encodeURIComponent(query)}&scope=${scope.value}`);
      if (asked !== state.asked) return;
      state.results = answer.results || [];
      state.active = 0;
      draw();
      const read = answer.read ? `, ${answer.read} read afresh` : '';
      status.textContent = state.results.length
        ? `${state.results.length} found in ${answer.searched} conversations (${(answer.ms / 1000).toFixed(1)} s${read}).`
        : `Nothing found in ${answer.searched} conversations.`;
      if (state.results.length > 1 && ai.querySelector('input').checked) rank(asked, query);
    } catch (error) {
      if (asked === state.asked) status.textContent = `The search failed: ${error.message || error}`;
    }
  };
  const later = () => { clearTimeout(state.timer); state.timer = setTimeout(run, WAIT_MS); };

  input.addEventListener('input', later);
  scope.addEventListener('change', run);
  ai.querySelector('input').addEventListener('change', (event) => { remember(event.target.checked); run(); });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!state.results.length) return;
      state.active = (state.active + (event.key === 'ArrowDown' ? 1 : -1) + state.results.length) % state.results.length;
      highlight();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      // Typed and not searched yet: search now. Otherwise open the highlighted card.
      if (state.timer || !state.results.length) run();
      else choose(state.active);
    }
  });
  list.addEventListener('click', (event) => {
    const tick = event.target.closest('.conv-pick');
    if (tick) {
      const result = state.results.find((r) => r.id === tick.dataset.id);
      if (tick.checked && result) state.picked.set(result.id, result);
      else state.picked.delete(tick.dataset.id);
      showPicked();
      return;
    }
    const li = event.target.closest('.conv-card');
    if (li) choose(Number(li.dataset.index));
  });
  if (all) {
    all.querySelector('input').addEventListener('change', (event) => {
      for (const result of tickable()) {
        if (event.target.checked) state.picked.set(result.id, result); else state.picked.delete(result.id);
      }
      draw();
    });
  }
  actButton.addEventListener('click', () => {
    const chosen = [...state.picked.values()];
    finish();
    action.run(chosen);
  });
  close.addEventListener('click', finish);
  dialog.addEventListener('close', () => { if (dialog.isConnected) dialog.remove(); });
  dialog.showModal();
  input.focus();
  if (opts.initial) showInitial();
  return dialog;
}
