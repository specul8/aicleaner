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

function card(result, index, several) {
  const folder = several ? result.project.split(/[\\/]/).filter(Boolean).pop() : '';
  const head = [when(result.updated), result.branch ? `[${result.branch}]` : '', folder].filter(Boolean).join('  ');
  return h('li', { class: 'conv-card', role: 'option', tabindex: '-1', dataset: { index: String(index) } },
    h('div', { class: 'conv-head muted' }, head,
      h('span', { class: 'conv-count' }, `${result.matches} ${result.matches === 1 ? 'match' : 'matches'} in ${result.turns_matched} ${result.turns_matched === 1 ? 'turn' : 'turns'}`)),
    h('div', { class: 'conv-title' }, result.title),
    result.last && result.last !== result.title ? h('div', { class: 'conv-last muted' }, `latest: ${result.last}`) : null,
    result.why ? h('div', { class: 'conv-why' }, result.why) : null,
    ...(result.passages || []).map(marked));
}

// opts: { name: () => profile shown, choose(result) }
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
  const dialog = h('dialog', { id: 'conv-search', class: 'sheet conv-search', 'aria-label': 'Search conversations' },
    h('h2', {}, 'Search conversations'),
    h('div', { class: 'form-row conv-bar' }, input, scope, ai),
    status, list, h('div', { class: 'form-row' }, close));
  document.body.append(dialog);

  const state = { results: [], active: 0, timer: null, asked: 0 };
  const draw = () => {
    const several = new Set(state.results.map((r) => r.project)).size > 1;
    list.replaceChildren(...state.results.map((result, i) => card(result, i, several)));
    highlight();
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
    if (!query) { state.results = []; draw(); status.textContent = 'Type what the conversation was about.'; return; }
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
    const li = event.target.closest('.conv-card');
    if (li) choose(Number(li.dataset.index));
  });
  close.addEventListener('click', finish);
  dialog.addEventListener('close', () => { if (dialog.isConnected) dialog.remove(); });
  dialog.showModal();
  input.focus();
  return dialog;
}
