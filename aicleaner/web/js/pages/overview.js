// Overview: status banner (exact wording approved for the three states), four tiles, recent
// requests, and the reference material that used to fill the landing page, collapsed.

import { getJSON } from '../api.js';
import { h, icon, shortTime, timeAgo } from '../dom.js';
import { clientHref, displayName, refreshStatus } from '../app.js';
import { mcpCard } from './mcpcard.js';

const view = { banner: null, problems: null, tiles: null, recent: null, lastTs: undefined, secrets: 0, mcp: null };

// Repainted only when the audit's answer changes: the status poll runs every few seconds, and a
// button rebuilt that often moves under the pointer (D69's lesson).
function paintMcp(status) {
  const key = JSON.stringify([status.mcp || [], Boolean(status.remote)]);
  if (key === view.mcpKey) return;
  view.mcpKey = key;
  view.mcp.replaceChildren(...[mcpCard(status, refreshStatus)].filter(Boolean));
}

function problemRows(status) {
  return [...(status.global ? [status.global] : []), ...status.clients]
    .filter((r) => r.state.some((s) => s === 'broken' || s === 'fallback' || s === 'conflict-copy' || s === 'held'));
}

function bannerContent(status) {
  // Before anything about the mapping: is this proxy in the path at all? A sound mapping says the
  // filter *could* work. With every client aimed straight at the provider the page used to say
  // client data was being filtered, which was a direct assurance and untrue (D37). This outranks
  // a broken mapping, because a broken one makes the proxy refuse traffic - nothing escapes -
  // while a client aimed past us is unfiltered data leaving now.
  // Bypass on: nothing is filtered, whatever else is true (D148).
  if (status.bypass) {
    return {
      level: 'red', symbol: 'cross', headline: 'Client-identity data filtering: OFF (bypass)',
      detail: ['Every request is forwarded untouched - client names reach the AI service as they are. ',
        h('a', { href: '#/settings/bypass' }, 'Turn bypass off')],
    };
  }
  const routing = status.routing || {};
  if (routing.state === 'bypassed') {
    const who = (routing.bypassed || []).join(' and ') || 'Your AI client';
    return {
      level: 'red', symbol: 'cross', headline: 'Client-identity data filtering: Not in use',
      detail: [`${who} ${(routing.bypassed || []).length === 1 ? 'is' : 'are'} sending directly to the AI service, `
        + 'so nothing is being filtered. ', h('a', { href: '#/settings' }, 'See what each client is pointed at')],
    };
  }
  const broken = problemRows(status).filter((r) => r.state.includes('broken'));
  if (broken.length) {
    const row = broken[0];
    const what = row.id === 'global'
      ? 'The global filter mapping is invalid.'
      : `The filter mapping is invalid for ${displayName(row)}.`;
    return {
      level: 'red', symbol: 'cross', headline: 'Client-identity data filtering: Stopped',
      detail: [`${what} AI communication stopped until resolved. `, h('a', { href: clientHref(row.id) }, 'Show the error')],
    };
  }
  // Something the filter meant to hold back and could not. Above the ordinary review prompt,
  // because those are things waiting for a decision and this is one that already went out (D12).
  if (status.unprotected > 0) {
    const n = status.unprotected;
    return {
      level: 'red', symbol: 'cross',
      headline: `${n} ${n === 1 ? 'identifier was' : 'identifiers were'} sent without being replaced`,
      detail: [`The filter had no placeholder left to use, so ${n === 1 ? 'it' : 'they'} went out as `
        + 'written. Mapping or ignoring them clears the backlog and stops it recurring. ',
        h('a', { href: '#/review' }, 'Review them')],
    };
  }
  if (status.pending > 0) {
    const n = status.pending;
    const held = status.held_back || 0;
    const asIs = status.sent_as_is !== undefined ? status.sent_as_is : n - held;
    const parts = [];
    if (held) parts.push(`${held} held back as ${held === 1 ? 'a placeholder' : 'placeholders'}`);
    if (asIs) parts.push(`${asIs} sent as-is and may contain client-specific information`);
    return {
      level: 'amber', symbol: 'alert', headline: 'Unrecognised client-identity data needs review',
      detail: [`${parts.join('; ')}. `, h('a', { href: '#/review' }, `Review ${n} ${n === 1 ? 'item' : 'items'}`)],
    };
  }
  // Pointed here, but nothing has arrived since this proxy started. True and worth saying: it is
  // what a misconfigured client looks like before anyone tries to use it.
  if (status.routing && status.routing.state === 'idle') {
    return {
      level: 'green', symbol: 'check', headline: 'Client-identity data filtering: Ready',
      detail: ['Set up and waiting. Nothing has come through since the proxy started, so there is '
        + 'nothing to report yet - the first request will be filtered.'],
    };
  }
  return {
    level: 'green', symbol: 'check', headline: 'Client-identity data filtering: On',
    detail: ['Anything that could identify a client is filtered out before it leaves this machine, and swapped back in when the AI service replies.'],
  };
}

// Copilot in VS Code (release 6): what is covered, said plainly - a filtered chat beside an unfiltered
// completion is easy to mistake for full coverage (walkthrough 19) - and a warning when it goes quiet.
function copilotLine(status) {
  const c = status.copilot || {};
  if (!c.enabled) return null;
  const settings = h('a', { href: '#/settings/copilot' }, 'Copilot settings');
  let warn = '';
  if (c.error) warn = c.error;
  else if (c.waiting) warn = `waiting - ${c.waiting}`;
  else if (!c.vscode_found) warn = 'no VS Code found on this machine';
  else if (!c.pointed) warn = 'VS Code is not pointed at aicleaner';
  else if (c.quiet) warn = 'nothing from Copilot for a day - GitHub can change its hosts or shapes; check VS Code is still pointed here';
  if (warn) {
    return h('div', { class: 'callout warn copilot-coverage' }, h('b', {}, 'GitHub Copilot in VS Code: '), `${warn}. `, settings);
  }
  return h('div', { class: 'callout ok copilot-coverage' }, h('b', {}, 'GitHub Copilot in VS Code: '),
    'chat, agent, inline completions, next edit suggestions and telemetry are filtered. '
    + 'Microsoft 365 Copilot is not (it runs in Microsoft\'s cloud). ', settings);
}

function paintBanner(status) {
  const b = bannerContent(status);
  view.banner.dataset.level = b.level;
  view.banner.replaceChildren(
    h('div', { class: 'headline' }, icon(b.symbol), h('span', {}, b.headline)),
    h('div', { class: 'detail' }, ...b.detail));
  const rows = problemRows(status).filter((r) => !r.state.includes('broken'));
  view.problems.replaceChildren(...rows.map((r) => h('div', { class: 'callout bad' },
    h('b', {}, `${displayName(r)}: `),
    r.state.includes('fallback')
      ? `an edit was rejected, so the last good version${r.fallback_at ? ` (${shortTime(r.fallback_at)})` : ''} is in use. `
      : `a conflicting copy of this file was found (${(r.conflicts || []).join(', ')}). `,
    h('a', { href: clientHref(r.id) }, 'Show the error'))), ...[copilotLine(status)].filter(Boolean));
}

function tile(key, label, value, sub, warn) {
  return h('div', { class: `card tile${warn ? ' warn' : ''}`, dataset: { tile: key } },
    h('span', { class: 'label' }, label), h('span', { class: 'value' }, value), h('span', { class: 'sub' }, sub));
}

function paintTiles(status) {
  const entries = status.clients.reduce((sum, r) => sum + (r.entries || 0), 0);
  const globalEntries = status.global ? status.global.entries : 0;
  const last = status.last_request;
  view.tiles.replaceChildren(
    tile('review', 'To review', String(status.pending),
      status.pending ? h('a', { href: '#/review' }, 'Review now') : 'Nothing waiting', status.pending > 0),
    tile('clients', 'Clients', String(status.clients.length), `${entries} entries, ${globalEntries} global`),
    tile('credentials', 'Credentials', status.secrets_mode.charAt(0).toUpperCase() + status.secrets_mode.slice(1),
      `${view.secrets} saved ${view.secrets === 1 ? 'secret' : 'secrets'}`),
    tile('last', 'Last request', last ? timeAgo(last.ts) : 'None yet',
      last ? `${last.outbound} filtered, ${last.inbound} restored` : 'No model traffic since the logs began'));
}

function resultText(line) {
  if (line.note && line.note.startsWith('refused')) return { text: line.note, cls: 'bad' };
  if (line.canary === 'leak') return { text: 'refused: a real name was still present', cls: 'bad' };
  if (line.note) return { text: line.note, cls: 'warn' };
  if (line.status >= 400) return { text: `upstream error ${line.status}`, cls: 'warn' };
  return { text: 'sent', cls: 'ok' };
}

function attachmentNote(a) {
  if (!a) return null;
  const parts = [];
  if (a.pdf_converted) parts.push(`${a.pdf_converted} PDF converted`);
  if (a.pdf_withheld) parts.push(`${a.pdf_withheld} PDF withheld`);
  if (a.image_withheld) parts.push(`${a.image_withheld} image${a.image_withheld === 1 ? '' : 's'} withheld`);
  if (a.image_allowed) parts.push(`${a.image_allowed} image${a.image_allowed === 1 ? '' : 's'} sent`);
  if (a.other_withheld) parts.push(`${a.other_withheld} other withheld`);
  return parts.length ? h('div', { class: 'hint' }, parts.join(', ')) : null;
}

function sum(obj) {
  return Object.values(obj || {}).reduce((a, v) => a + Math.abs(Number(v) || 0), 0);
}

async function paintRecent() {
  let lines = [];
  try {
    lines = (await getJSON('/api/evidence?n=10')).lines || [];
  } catch (e) {
    lines = [];
  }
  const body = h('tbody', {}, lines.slice().reverse().map((line) => {
    const result = resultText(line);
    return h('tr', {},
      h('td', {}, shortTime(line.ts)),
      h('td', { class: 'mono' }, `${line.dialect} ${line.path}`),
      h('td', {}, String(sum(line.outbound)), sum(line.held_back) ? ` (+${sum(line.held_back)} held back)` : ''),
      h('td', {}, String(sum(line.inbound))),
      h('td', {}, h('span', { class: `chip ${result.cls}` }, result.text), attachmentNote(line.attachments)));
  }));
  const table = h('table', { id: 'recent', dataset: { resizable: 'recent' } },
    h('thead', {}, h('tr', {},
      h('th', { dataset: { col: 'time' } }, 'Time'),
      h('th', { dataset: { col: 'route' } }, 'Route'),
      h('th', { dataset: { col: 'filtered' } }, 'Filtered'),
      h('th', { dataset: { col: 'restored' } }, 'Restored'),
      h('th', {}, 'Result'))), body);
  view.recent.replaceChildren(h('h2', {}, 'Recent requests'),
    lines.length ? table : h('p', { class: 'empty' }, 'No requests yet. Point a tool at this proxy and they will appear here.'));
  // Keep the id present even when empty, so tests and links can find the table.
  if (!lines.length) view.recent.append(h('table', { id: 'recent', hidden: true }, h('tbody')));
}

const DIAGRAM = [
  'YOUR MACHINE (real names)                         ON THE WIRE (tokens)',
  '------------------------------------------------  --------------------------------------------',
  'Claude Code / Claude Desktop / other AI tools',
  '  "list Fabrikam Inc\'s resource groups"',
  '                |',
  '                v',
  'aicleaner proxy',
  ' 1 tokenise   Fabrikam Inc, fabrikam.com ------->   Client4821 Pty, client4821.invalid',
  ' 2 redact     AccountKey=..., PATs, SAS, JWTs -->   [REDACTED-azure-account-key]',
  ' 3 learn      identifier-shaped leftovers ......   (queued locally for review)',
  ' 4 canary     a real form still present? ......   refused: nothing sent',
  ' 5 credential local gateway token ............>   real API key  ---->  model provider',
  ' 6 rehydrate  Fabrikam Inc  <------------------   Client4821 Pty  <---- reply (streamed)',
  ' 7 evidence   one line per request ............   counts and outcomes, never content',
  ' 8 detail     Fabrikam Inc -> Client4821 Pty ..   what was replaced, kept here for audit',
].join('\n');

function reference() {
  return h('details', { class: 'more', id: 'reference' }, h('summary', {}, 'Reference'),
    h('div', {},
      h('h3', {}, 'Endpoints'),
      h('ul', {},
        h('li', {}, h('code', {}, '/anthropic/*'), ' and ', h('code', {}, '/openai/*'), ': the proxied model routes your tools are pointed at'),
        h('li', {}, h('code', {}, '/healthz'), ': status for scripts (clients, entries, pending, credential swap)'),
        h('li', {}, h('code', {}, 'POST /api/tokenise'), ', ', h('code', {}, 'POST /api/rehydrate'), ': text in, text out, for hooks')),
      h('h3', {}, 'Files'),
      h('ul', {},
        h('li', {}, h('code', {}, 'clients\\<id>.json'), ': one client per file (the unit of sharing); ', h('code', {}, '_global.json'), ' beside the config for Global (yours alone, never in the shared clients folder)'),
        h('li', {}, h('code', {}, 'ignore-list.json'), ' and the local ', h('code', {}, 'candidates-<host>.jsonl'), ' beside the mapping'),
        h('li', {}, h('code', {}, 'logs\\'), ': the evidence log (counts only) and the optional substitution detail log'),
        h('li', {}, h('code', {}, '%USERPROFILE%\\.aicleaner\\lastgood\\'), ': last good copy of each client file, used when an edit is rejected'))));
}

function howItWorks() {
  return h('details', { class: 'more', id: 'how' }, h('summary', {}, 'How it works'),
    h('div', {},
      h('p', {}, 'A local proxy between your AI tools and the model provider. Client identifiers are swapped for tokens on the way out and back on the way in, so real names stay on this machine and only tokens go on the wire. Anything the proxy cannot inspect is refused. Every request leaves an evidence line of counts, and what was replaced - each real name and the token that stood in for it - is written to a detail log so a request can be audited afterwards. Both stay on this machine and neither is ever sent anywhere.'),
      h('pre', { class: 'diagram' }, DIAGRAM),
      h('p', { class: 'muted' }, 'The proxy only listens on the loopback address: no other machine can reach it, and the only outbound connections are to the model provider itself, carrying tokens.')));
}

export async function render(main, ctx) {
  view.banner = h('div', { class: 'banner', id: 'banner', role: 'status' });
  view.problems = h('div', { class: 'problems' });
  view.tiles = h('div', { class: 'grid-4', id: 'tiles' });
  view.recent = h('div', { class: 'card' });
  view.mcp = h('div', { id: 'mcp-slot' });
  view.mcpKey = null;
  main.append(h('div', { class: 'page-head' }, h('h1', {}, 'Overview')),
    view.banner, view.problems, view.tiles, view.recent, view.mcp, howItWorks(), reference());
  paintMcp(ctx.status);
  try {
    view.secrets = ((await getJSON('/api/secrets')).entries || []).length;
  } catch (e) {
    view.secrets = 0;
  }
  paintBanner(ctx.status);
  paintTiles(ctx.status);
  view.lastTs = ctx.status.last_request ? ctx.status.last_request.ts : null;
  await paintRecent();
}

export function onStatus(status) {
  if (!view.banner || !view.banner.isConnected) return;
  paintBanner(status);
  paintTiles(status);
  paintMcp(status);
  const ts = status.last_request ? status.last_request.ts : null;
  if (ts !== view.lastTs) {
    view.lastTs = ts;
    paintRecent();
  }
}
