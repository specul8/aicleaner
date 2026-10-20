// The AI tools aicleaner knows (item H, 2026-10-19): found, filtered, signed in, and sign-in - one list
// for the Settings card and the setup wizard's "Your AI tools" step. Claude Code's sign-in runs from
// here, so it works through remote access too: its link opens on whatever device you are on, and the
// code its sign-in page shows is pasted back here.
//
// Each tool is one line - its name, whether it was found, whether aicleaner filters it and whether it is
// signed in - with a chevron to open the rest (J4, Brad 2026-10-20). Routing Claude Code through
// aicleaner works from anywhere (J2); taking it off does not, since that lets it reach the AI service
// unfiltered (J4: "it allows the AI tool to bypass the proxy").

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
  const note = h('div', { class: 'hint' }, row.filter_note);
  const actions = [];
  if (row.id === 'claude-code' && row.found) {
    actions.push(row.filtered
      ? localButton('Stop routing', remote, async () => {
        const answer = await postJSON('/api/setup/disable', { only: 'ClaudeCode' });
        say(answer.ok ? 'Claude Code goes straight to Anthropic now' : 'Not changed - see the setup log', answer.ok ? 'info' : 'warn');
        await refresh();
      }, 'aitools-route-off')
      : localButton('Route through aicleaner', false, async () => {
        const answer = await postJSON('/api/setup/enable', {});
        say(answer.ok ? 'Claude Code goes through aicleaner now' : 'Not switched - the readiness checks said why', answer.ok ? 'info' : 'warn');
        await refresh();
      }, 'aitools-route-on'));
  }
  if (row.id === 'codetab') actions.push(h('a', { href: '#/settings/codetab' }, 'The Code tab card'));
  if (row.id === 'copilot-vscode') actions.push(h('a', { href: '#/settings/copilot' }, 'The Copilot card'));
  return h('div', { class: 'aitools-filter' }, h('div', { class: 'aitools-label' }, 'Filtered by aicleaner'), note,
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

// Another agent (item K): Install when it is not here (into aicleaner's own folder, no prompt), then its
// own device-code sign-in - a link and a code shown here, entered on any device; the CLI finishes itself.
function deviceBox(row, device, refresh) {
  const box = h('div', { class: 'aitools-device' });
  let timer = null;
  const paint = (view) => {
    if (timer) { clearTimeout(timer); timer = null; }
    if (!row.sign_in.installed) {
      const install = h('button', { type: 'button', class: 'aitools-install' }, `Install ${row.name}`);
      install.addEventListener('click', async () => {
        install.disabled = true;
        install.textContent = 'Installing... (a minute or two)';
        try {
          const answer = await postJSON('/api/aitools/install', { agent: row.sign_in.agent });
          say(answer.ok ? `${row.name} installed.` : `Not installed: ${answer.output}`, answer.ok ? 'ok' : 'warn');
          await refresh();
        } catch (error) { sayError(error); install.disabled = false; install.textContent = `Install ${row.name}`; }
      });
      setChildren(box, h('div', { class: 'form-row' }, install), h('p', { class: 'hint' }, 'Needs Node.js on this machine.'));
      return;
    }
    if (view.state === 'waiting' && view.code) {
      const cancel = h('button', { type: 'button' }, 'Cancel');
      cancel.addEventListener('click', async () => paint(await postJSON('/api/aitools/device', { tool: row.id, action: 'cancel' })));
      setChildren(box,
        h('p', {}, '1. ', h('a', { href: view.url, target: '_blank', rel: 'noopener noreferrer', class: 'aitools-device-link' },
          view.url), ' (on this device)'),
        h('p', {}, '2. Enter this code: ', h('b', { class: 'aitools-device-code mono' }, view.code)),
        h('div', { class: 'form-row' }, h('span', { class: 'hint' }, 'Waiting for you to finish there...'), cancel));
      timer = setTimeout(async () => {
        try {
          const next = await getJSON(`/api/aitools/device?tool=${encodeURIComponent(row.id)}`);
          if (next.state === 'done') { say(`${row.name}: ${next.message || 'signed in'}`, 'ok'); await refresh(); return; }
          paint(next);
        } catch (error) { /* the next look will tell */ }
      }, 3000);
      return;
    }
    const start = h('button', { type: 'button', class: 'aitools-device-start' }, `Sign in to ${row.name}`);
    start.addEventListener('click', async () => {
      start.disabled = true;
      start.textContent = 'Starting...';
      try { paint(await postJSON('/api/aitools/device', { tool: row.id, action: 'start' })); } catch (error) { sayError(error); paint({ state: 'idle' }); }
    });
    setChildren(box, h('div', { class: 'form-row' }, start), h('p', { class: 'hint' }, row.sign_in.steps),
      view.message ? h('p', { class: `hint ${view.state === 'failed' ? 'bad' : ''}` }, view.message) : null);
  };
  paint(device || { state: 'idle' });
  return box;
}

function signInCell(row, login, refresh, devices) {
  const detail = row.detail ? h('div', { class: 'hint' }, row.detail) : null;
  const label = h('div', { class: 'aitools-label' }, 'Signing in');
  if (row.sign_in.kind === 'drive') return h('div', { class: 'aitools-signin' }, label, detail, loginBox(login, refresh));
  if (row.sign_in.kind === 'device') {
    return h('div', { class: 'aitools-signin' }, label, detail, deviceBox(row, (devices || {})[row.id], refresh));
  }
  return h('div', { class: 'aitools-signin' }, label, detail, h('div', { class: 'hint' }, row.sign_in.steps));
}

// The list, refreshed in place after anything changes. Which tools are open is kept across a refresh.
export function aiToolsTable() {
  const host = h('div', { class: 'aitools' }, h('p', { class: 'hint' }, 'Looking for AI tools...'));
  const open = new Set();
  const refresh = async () => {
    let data;
    try { data = await getJSON('/api/aitools'); } catch (error) {
      host.replaceChildren(h('p', { class: 'hint bad' }, `The AI tools could not be read: ${error.message || error}`));
      return;
    }
    const remote = Boolean(data.remote);
    // A sign-in left waiting keeps its row open, so the code box is where it was.
    if (data.login && data.login.state === 'waiting') open.add('claude-code');
    for (const [tool, device] of Object.entries(data.devices || {})) if (device.state === 'waiting') open.add(tool);
    const body = data.tools.map((row) => {
      const item = h('details', { class: 'aitools-row', dataset: { tool: row.id }, open: open.has(row.id) ? true : null },
        h('summary', { class: 'aitools-summary' },
          h('span', { class: 'aitools-name' }, row.name),
          chip(row.found, 'found', 'not found'),
          chip(row.filtered, 'filtered', 'not filtered'),
          chip(row.signed_in, 'signed in', 'not signed in', 'sign-in not checked')),
        h('div', { class: 'aitools-body' }, filterCell(row, remote, refresh), signInCell(row, data.login, refresh, data.devices)));
      item.addEventListener('toggle', () => { if (item.open) open.add(row.id); else open.delete(row.id); });
      return item;
    });
    setChildren(host, h('div', { class: 'aitools-list' }, body));
  };
  refresh();
  return host;
}
