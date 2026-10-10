// The setup wizard (feature 106; spec docs/superpowers/specs/2026-10-09-onboarding-and-public-release-design.md,
// section 6). Opened by the installer at the end of its console part, and from Settings at any time.
// Five steps, each skippable: what happens, your first client, remote access, your AI tools, done.
// Routing the AI tools is last but one (feature 110): nothing is switched over before a client exists.
// Local only - the server refuses /api/setup through the tunnel - because it runs this machine's
// scripts. The Cloudflare and DNS guidance is text the owner checks (TESTING marks it so).

import { getJSON, postJSON, sayError } from '../api.js';
import { h, setChildren } from '../dom.js';

const STEPS = ['What happens', 'Your first client', 'Remote access', 'Your AI tools', 'Done'];
const view = { step: 0, main: null, ctx: null, dns: null };

export async function render(main, ctx) {
  view.main = main;
  view.ctx = ctx || {};
  document.title = 'Set up - aicleaner';
  paint();
}

function paint() {
  const status = view.ctx.status || {};
  const tabs = h('ol', { class: 'setup-steps', 'aria-label': 'Setup steps' },
    STEPS.map((name, i) => {
      const item = h('li', { class: i === view.step ? 'current' : (i < view.step ? 'done' : '') },
        h('button', { type: 'button', class: 'link', dataset: { step: String(i) } }, `${i + 1}. ${name}`));
      if (i === view.step) item.setAttribute('aria-current', 'step');
      return item;
    }));
  tabs.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-step]');
    if (button) { view.step = Number(button.dataset.step); paint(); }
  });
  const body = [welcome, firstClient, remote, tools, done][view.step](status);
  const back = h('button', { type: 'button', class: 'setup-back', disabled: view.step === 0 }, 'Back');
  const next = h('button', { type: 'button', class: 'primary setup-next' }, view.step === STEPS.length - 1 ? 'Finish' : 'Next');
  back.addEventListener('click', () => { view.step -= 1; paint(); });
  next.addEventListener('click', () => {
    if (view.step === STEPS.length - 1) { location.hash = '#/overview'; return; }
    view.step += 1;
    paint();
  });
  setChildren(view.main, h('h1', {}, 'Set up aicleaner'), tabs,
    h('section', { class: 'card setup-card', dataset: { step: String(view.step) } }, ...body),
    h('div', { class: 'form-row setup-nav' }, back, next));
}

function welcome() {
  return [
    h('h2', {}, 'What happens'),
    h('p', {}, 'aicleaner sits between your AI tools and the AI service, on this machine. Before anything leaves, '
      + 'each client\'s names, domains, tenants, addresses and resource names are swapped for neutral stand-ins; '
      + 'when the reply comes back, the real names are put back. Nothing about your clients leaves this machine '
      + 'except as a stand-in.'),
    h('p', {}, 'The next steps: tell aicleaner your first client, set up remote access - only if you want to use it from '
      + 'another device - and then point your AI tools at aicleaner, last, once it knows who to protect. Every step can be '
      + 'skipped and done later from Settings.'),
  ];
}

function tools(status) {
  const rows = ((status.routing || {}).clients) || [];
  const table = rows.length ? h('table', { class: 'routing-table' }, h('tbody', {}, rows.map((row) => h('tr', {},
    h('th', {}, row.client),
    h('td', {}, h('span', { class: `chip ${row.routed ? 'ok' : 'bad'}` }, row.routed ? 'through aicleaner' : 'direct to the AI service')))))) :
    h('p', { class: 'hint' }, 'No AI tool settings were found on this machine yet. Install Claude Code or Claude Desktop, then come back.');
  const output = h('pre', { class: 'setup-output', hidden: true });
  const go = h('button', { type: 'button', class: 'primary setup-enable' }, 'Route them through aicleaner');
  go.addEventListener('click', async () => {
    go.disabled = true;
    go.textContent = 'Checking the filter is ready, then switching over...';
    try {
      const answer = await postJSON('/api/setup/enable', {});
      output.hidden = false;
      output.textContent = answer.output;
      go.textContent = answer.ok ? 'Done - they go through aicleaner now' : 'Not switched: see why below';
      if (view.ctx.refresh) view.ctx.status = await view.ctx.refresh();
    } catch (error) {
      go.disabled = false;
      go.textContent = 'Route them through aicleaner';
      sayError(error);
    }
  });
  return [
    h('h2', {}, 'Your AI tools'),
    h('p', {}, 'Each AI tool has a setting that says where to send requests. Routing it through aicleaner changes that one '
      + 'setting, after checking the filter is ready - a running tool picks it up without a restart.'),
    table, h('div', { class: 'form-row' }, go), output,
  ];
}

function firstClient(status) {
  const clients = (status.clients || []).filter((c) => c !== 'global');
  const add = h('button', { type: 'button', class: 'primary setup-add-client' }, clients.length ? 'Add another client' : 'Add your first client');
  add.addEventListener('click', async () => {
    const { askForNewClient } = await import('./newclient.js');
    askForNewClient();
  });
  return [
    h('h2', {}, 'Your first client'),
    h('p', {}, 'Tell aicleaner who your client is: their name, and the domains and Microsoft tenant they use. Everything else '
      + 'it learns as you work - new names it finds wait for you in Review.'),
    clients.length ? h('p', { class: 'callout ok' }, `You have ${clients.length} client${clients.length === 1 ? '' : 's'} set up: `,
      h('a', { href: '#/clients' }, 'see them on Client Filters'), '.') : null,
    h('div', { class: 'form-row' }, add),
  ];
}

function remote() {
  const box = h('input', { type: 'text', class: 'setup-domain', placeholder: 'example.com', 'aria-label': 'Your domain',
    value: view.dns ? view.dns.domain : '', 'data-unsaved-ignore': '' });
  const look = h('button', { type: 'button', class: 'setup-lookup' }, 'Look it up');
  const report = h('div', { class: 'setup-dns' });
  const show = () => setChildren(report, ...dnsAdvice(view.dns));
  look.addEventListener('click', async () => {
    look.disabled = true;
    look.textContent = 'Looking...';
    try {
      view.dns = await getJSON(`/api/setup/dns?domain=${encodeURIComponent(box.value)}`);
      show();
    } catch (error) { sayError(error); }
    look.disabled = false;
    look.textContent = 'Look it up';
  });
  box.addEventListener('keydown', (event) => { if (event.key === 'Enter') look.click(); });
  if (view.dns) show();
  return [
    h('h2', {}, 'Remote access (optional)'),
    h('p', {}, 'To use aicleaner from your phone or another computer, it is reached through a Cloudflare tunnel at an address '
      + 'on a domain you own, behind Cloudflare\'s sign-in. Nothing is opened on this machine: the tunnel dials out. '
      + 'Skip this if you only work at this PC.'),
    h('p', {}, 'Which domain would you use? aicleaner looks at its DNS (read-only) to tell you what setting it up involves.'),
    h('div', { class: 'form-row' }, box, look), report,
  ];
}

// What the lookup found, and what to do next, in plain steps.
export function dnsAdvice(dns) {
  if (!dns) return [];
  if (dns.error) return [h('p', { class: 'callout bad' }, dns.error)];
  const facts = h('dl', { class: 'setup-facts' },
    h('dt', {}, 'DNS hosted by'), h('dd', {}, dns.host || `not recognised (${dns.nameservers.join(', ')})`),
    h('dt', {}, 'Email'), h('dd', {}, dns.mail.length ? dns.mail.join(', ') : 'no mail records'),
    h('dt', {}, 'Mail protection'), h('dd', {}, [dns.spf ? 'SPF' : '', dns.dmarc ? 'DMARC' : ''].filter(Boolean).join(' and ') || 'none found'),
    h('dt', {}, 'Website'), h('dd', {}, dns.apex.length || dns.www.length ? [...new Set([...dns.apex, ...dns.www])].join(', ') : 'none found'));
  const address = `ai.${dns.domain}`;
  const token = h('div', {},
    h('h3', {}, 'Then: a Cloudflare API token'),
    h('ol', {},
      h('li', {}, 'In the Cloudflare dashboard, open My Profile > API Tokens > Create Token > Create Custom Token.'),
      h('li', {}, `Permissions: Account - Cloudflare Tunnel - Edit; Account - Access: Apps and Policies - Edit; Account - Account Settings - Read; Zone - DNS - Edit. Zone resources: ${dns.domain}.`),
      h('li', {}, 'Create it and copy the token (Cloudflare shows it once), then fill this in:')),
    remoteForm(address));
  if (dns.on_cloudflare) {
    return [facts, h('p', { class: 'callout ok' }, `${dns.domain} is already on Cloudflare - nothing to move. aicleaner would be at ${address}.`), token];
  }
  const keep = dns.mail.length ? h('p', { class: 'callout warn' },
    `${dns.domain} carries email (${dns.mail[0]}${dns.mail.length > 1 ? ' and more' : ''}). Moving its DNS must keep every mail record - `
    + 'a missed one stops your email. If you are not sure, use a separate domain (option B).') : null;
  const cpanel = dns.kind === 'cpanel' ? h('li', {}, 'Your host uses cPanel: sign in, open Zone Editor, choose the domain, and Export the zone file '
    + '(or copy every record down). Cloudflare can import that file under DNS > Records > Import and Export.') : null;
  return [facts, keep,
    h('h3', {}, 'Option A: move this domain\'s DNS to Cloudflare (free)'),
    h('ol', {},
      h('li', {}, 'Create a free Cloudflare account and choose Add a domain; enter ' + dns.domain + ' and pick the Free plan.'),
      cpanel,
      h('li', {}, 'Cloudflare scans the existing records. Compare its list with what is shown above - every mail record, the website, '
        + 'and anything else you use - and add whatever is missing before going on.'),
      h('li', {}, `Cloudflare then shows two nameservers. At ${dns.host || 'the company you registered the domain with'}, replace the current `
        + 'nameservers with those two.'),
      h('li', {}, 'Wait until Cloudflare shows the domain as Active (minutes to a few hours), then look it up here again.')),
    h('h3', {}, 'Option B: a separate domain just for aicleaner (about US$10 a year)'),
    h('ol', {},
      h('li', {}, 'In the Cloudflare dashboard, open Domain Registration > Register Domains, and buy a short domain you do not use for anything else.'),
      h('li', {}, 'It is on Cloudflare from the start - nothing about your main domain changes. Look the new domain up here.')),
    token];
}

// The token, the address and who may sign in; then Enable-RemoteControl.ps1 runs on this machine
// (POST /api/setup/remote). It restarts the proxy at the end, so its log is read until it says it is
// done, through the restart.
function remoteForm(address) {
  const where = h('input', { type: 'text', class: 'setup-remote-host', value: address, 'aria-label': 'Address for aicleaner', 'data-unsaved-ignore': '' });
  const who = h('input', { type: 'email', class: 'setup-remote-email', placeholder: 'you@example.com', 'aria-label': 'Who may sign in', 'data-unsaved-ignore': '' });
  const secret = h('input', { type: 'password', class: 'setup-remote-token', autocomplete: 'off', 'aria-label': 'Cloudflare API token', 'data-unsaved-ignore': '' });
  const go = h('button', { type: 'button', class: 'primary setup-remote-go' }, 'Set up remote access');
  const out = h('pre', { class: 'setup-output setup-remote-log', hidden: true });
  const said = h('p', { class: 'setup-remote-said', role: 'status' });
  go.addEventListener('click', async () => {
    go.disabled = true;
    try {
      await postJSON('/api/setup/remote', { hostname: where.value, email: who.value, token: secret.value });
    } catch (error) { go.disabled = false; sayError(error); return; }
    secret.value = '';                                  // not kept in the page once sent
    out.hidden = false;
    said.textContent = 'Setting up - the tunnel, its DNS record and the sign-in rule. The proxy restarts at the end.';
    for (let i = 0; i < 150; i += 1) {
      await new Promise((resolve) => { setTimeout(resolve, 2000); });
      let log;
      try { log = await getJSON('/api/setup/remote/log'); } catch (error) { continue; }   // restarting
      out.textContent = log.text;
      out.scrollTop = out.scrollHeight;
      if (log.done) {
        said.textContent = log.ok ? `Remote access is on: https://${where.value} - sign in with ${who.value}.`
          : 'It stopped with an error - the log above says where. Fix that and try again.';
        go.disabled = false;
        return;
      }
    }
    said.textContent = 'Still running after five minutes - see logs\\remote-setup.log.';
    go.disabled = false;
  });
  return h('div', { class: 'setup-remote' },
    h('label', { class: 'field' }, 'Address for aicleaner', where),
    h('label', { class: 'field' }, 'Email that may sign in (Cloudflare sends it a one-time code)', who),
    h('label', { class: 'field' }, 'Cloudflare API token', secret),
    h('div', { class: 'form-row' }, go), said, out);
}

function done(status) {
  const routed = (((status.routing || {}).clients) || []).some((c) => c.routed);
  const clients = (status.clients || []).filter((c) => c !== 'global').length;
  return [
    h('h2', {}, 'Done'),
    h('ul', {},
      h('li', {}, clients ? `${clients} client${clients === 1 ? '' : 's'} set up.` : 'No client yet - step 2 adds one.'),
      h('li', {}, routed ? 'Your AI tools go through aicleaner.' : 'Your AI tools are not routed yet - step 4 does it.')),
    h('p', {}, 'Try it: ask Claude something that names your client, then open the Log to see what was replaced. '
      + 'New names it finds wait in Review. '),
    h('p', {}, h('a', { href: '#/wiki/Getting-started' }, 'Getting started'), ' · ', h('a', { href: '#/wiki/How-it-works' }, 'How it works'),
      ' · ', h('a', { href: '#/wiki' }, 'All wiki pages')),
  ];
}
