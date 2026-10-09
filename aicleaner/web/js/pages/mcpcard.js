// MCP servers on this machine, as the audit last found them (H11, DECISIONS 52).
//
// The audit runs when the proxy and each session start. A server reached over http(s) - or a local
// bridge relaying to one - gets tokens in its tool arguments until approved here; one running on
// this machine gets real names. Approving is local only: who receives client names is decided on
// the machine aicleaner runs on.

import { postJSON, say, sayError } from '../api.js';
import { h } from '../dom.js';

const KIND = {
  hosted: 'hosted elsewhere', bridged: 'relays to a remote server',
  egress: 'runs here, talks to a service', local: 'runs here',
};

function row(server, remote, after) {
  let names = h('span', { class: 'chip' }, 'real names');
  if (server.kept_by === 'config') names = h('span', { class: 'chip' }, 'tokens (kept by your settings)');
  else if (server.names === 'tokens') names = h('span', { class: 'chip warn' }, 'tokens until approved');
  let action = null;
  if (server.approvable) {
    action = h('button', { type: 'button', class: 'mcp-approve', disabled: remote,
      title: remote ? 'MCP servers can only be approved on the local machine' : '' }, 'Allow real names');
    action.addEventListener('click', async () => {
      try {
        await postJSON('/api/mcp/approve', { pattern: server.pattern });
        say(`${server.pattern} now gets real names.`, 'ok');
        await after();
      } catch (error) { sayError(error); }
    });
  }
  return h('tr', { dataset: { pattern: server.pattern } },
    h('td', { class: 'mono' }, server.pattern),
    h('td', {}, KIND[server.verdict] || server.verdict, h('div', { class: 'muted small' }, server.reason)),
    h('td', {}, names), h('td', {}, action));
}

export function mcpCard(status, after) {
  const servers = status.mcp || [];
  if (!servers.length) return null;
  return h('section', { class: 'card', id: 'mcp-servers' },
    h('h2', {}, 'MCP servers'),
    h('p', { class: 'hint' }, 'Checked when aicleaner and each session start. A server reached over the '
      + 'internet gets tokens, not client names, until you allow it here.'),
    h('table', { class: 'mcp-table' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Tools'), h('th', {}, 'What it is'), h('th', {}, 'Gets'), h('th', {}, ''))),
      h('tbody', {}, ...servers.map((s) => row(s, Boolean(status.remote), after)))));
}
