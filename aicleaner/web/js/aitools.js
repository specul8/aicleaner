// The AI tools aicleaner knows (item H, 2026-10-19): found, filtered, signed in, and sign-in - one table
// for the Settings card and the setup wizard's "Your AI tools" step. Claude Code's sign-in runs from
// here, so it works through remote access too: its link opens on whatever device you are on, and the
// code its sign-in page shows is pasted back here. Switches that act on this machine keep their current
// reach (routing Claude Code is local only until the settings review, Brad 2026-10-19).

import { getJSON, postJSON, say, sayError } from './api.js';
import { h, setChildren } from './dom.js';

const LOCAL_ONLY = 'Can only be changed where aicleaner is deployed';

function chip(on, yes, no, unknown = 'not checked') {
  if (on === null || on === undefined) return h('span', { class: 'chip' }, unknown);
  return h('span', { class: `chip ${on ? 'ok' : 'bad'}` }, on ? yes : no);
}

function localButton(label, remote, run, cls = '') {
  const button = h('button', { type: 'button', class: cls }, label);
  if (remote) {
    button.disabled = true;
    button.title = LOCAL_ONLY;
  } else {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await run(); } catch (error) { sayError(error); } finally { button.disabled = false; }
    });
  }
  return button;
}

// What can be done about filtering, per tool: a switch here, a link to the tool's own card, or a note.
function filterCell(row, remote, refresh) {
  const state = chip(row.filtered, 'filtered', 'not filtered');
  const note = h('div', { class: 'hint' }, row.filter_note);
  const actions = [];
  if (row.id === 'claude-code' && row.found) {
    actions.push(row.filtered
      ? localButton('Stop routing', remote, async () => {
        const answer = await postJSON('/api/setup/disable', { only: 'ClaudeCode' });
        say(answer.ok ? 'Claude Code goes straight to Anthropic now' : 'Not changed - see the setup log', answer.ok ? 'info' : 'warn');
        await refresh();
      }, 'aitools-route-off')
      : localButton('Route through aicleaner', remote, async () => {
        const answer = await postJSON('/api/setup/enable', {});
        say(answer.ok ? 'Claude Code goes through aicleaner now' : 'Not switched - the readiness checks said why', answer.ok ? 'info' : 'warn');
        await refresh();
      }, 'aitools-route-on'));
  }
  if (row.id === 'codetab') actions.push(h('a', { href: '#/settings/codetab' }, 'The Code tab card'));
  if (row.id === 'copilot-vscode') actions.push(h('a', { href: '#/settings/copilot' }, 'The Copilot card'));
  return h('div', { class: 'aitools-filter' }, h('div', { class: 'aitools-label' }, 'Filtered by aicleaner'), state, note,
    actions.length ? h('div', { class: 'form-row' }, ...actions) : null);
}

function loginBox(login, refresh) {
  const box = h('div', { class: 'aitools-login' });
  const paint = (view) => {
    if (view.state === 'waiting') {
      const code = h('input', { type: 'text', name: 'login-code', placeholder: 'Code from the sign-in page', autocomplete: 'off',
        'aria-label': 'Sign-in code' });
      const finish = h('button', { type: 'button', class: 'primary' }, 'Finish signing in');
      finish.addEventListener('click', async () => {
        finish.disabled = true;
        finish.textContent = 'Checking...';
        try {
          const done = await postJSON('/api/aitools/claude-code/login/code', { code: code.value });
          paint(done);
          if (done.state === 'done') { say(done.message); await refresh(); }
        } catch (error) { sayError(error); paint(view); }
      });
      const cancel = h('button', { type: 'button' }, 'Cancel');
      cancel.addEventListener('click', async () => paint(await postJSON('/api/aitools/claude-code/login/cancel', {})));
      setChildren(box,
        h('p', {}, '1. ', h('a', { href: view.url, target: '_blank', rel: 'noopener noreferrer', class: 'aitools-login-link' },
          'Open the Claude sign-in page'), ' (on this device) and sign in.'),
        h('p', {}, '2. Paste the code it shows you:'),
        h('div', { class: 'form-row' }, code, finish, cancel),
        view.message ? h('p', { class: 'hint' }, view.message) : null);
      return;
    }
    const start = h('button', { type: 'button', class: 'aitools-login-start' }, 'Sign in to Claude Code');
    start.addEventListener('click', async () => {
      start.disabled = true;
      start.textContent = 'Starting...';
      try { paint(await postJSON('/api/aitools/claude-code/login', {})); } catch (error) { sayError(error); paint({ state: 'idle' }); }
    });
    setChildren(box, h('div', { class: 'form-row' }, start),
      view.message ? h('p', { class: `hint ${view.state === 'failed' ? 'bad' : ''}` }, view.message) : null);
  };
  paint(login || { state: 'idle' });
  return box;
}

function signInCell(row, login, refresh) {
  const detail = row.detail ? h('div', { class: 'hint' }, row.detail) : null;
  const state = chip(row.signed_in, 'signed in', 'not signed in');
  const label = h('div', { class: 'aitools-label' }, 'Signed in');
  if (row.sign_in.kind === 'drive') return h('div', { class: 'aitools-signin' }, label, state, detail, loginBox(login, refresh));
  return h('div', { class: 'aitools-signin' }, label, state, detail, h('div', { class: 'hint' }, row.sign_in.steps));
}

// The table, refreshed in place after anything changes. `remote` locks what runs on this machine.
export function aiToolsTable() {
  const host = h('div', { class: 'aitools' }, h('p', { class: 'hint' }, 'Looking for AI tools...'));
  const refresh = async () => {
    let data;
    try { data = await getJSON('/api/aitools'); } catch (error) {
      host.replaceChildren(h('p', { class: 'hint bad' }, `The AI tools could not be read: ${error.message || error}`));
      return;
    }
    const remote = Boolean(data.remote);
    const body = data.tools.map((row) => h('div', { class: 'aitools-row', dataset: { tool: row.id } },
      h('div', { class: 'aitools-name' }, row.name, ' ', chip(row.found, 'found', 'not found on this machine')),
      filterCell(row, remote, refresh),
      signInCell(row, data.login, refresh)));
    // setChildren, not replaceChildren: the latter writes a null as the text "null".
    setChildren(host, h('div', { class: 'aitools-list' }, body),
    remote ? h('p', { class: 'chip local-only-note' }, `${LOCAL_ONLY}: routing Claude Code on or off runs on that machine. Signing in works from here.`) : null);
  };
  refresh();
  return host;
}
