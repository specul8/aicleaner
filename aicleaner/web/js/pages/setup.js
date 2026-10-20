// The setup wizard (feature 106; spec docs/superpowers/specs/2026-10-09-onboarding-and-public-release-design.md,
// section 6). Opened by the installer at the end of its console part, and from Settings at any time.
// Five steps, each skippable: what happens, your first client, remote access, your AI tools, done.
// Routing the AI tools is last but one (feature 110): nothing is switched over before a client exists.
// Local only - the server refuses /api/setup through the tunnel - because it runs this machine's
// scripts. The Cloudflare and DNS guidance is text the owner checks (TESTING marks it so).

import { getJSON, postJSON, sayError } from '../api.js';
import { h, setChildren } from '../dom.js';
import { aiToolsTable } from '../aitools.js';
import { casualFolderField } from '../chatnow.js';

const STEPS = ['What happens', 'Your first client', 'Remote access', 'Your AI tools', 'Casual chats', 'Done'];
const view = { step: 0, main: null, ctx: null, dns: null, remote: null };

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
  const body = [welcome, firstClient, remote, tools, casualChats, done][view.step](status);
  const back = h('button', { type: 'button', class: 'setup-back', disabled: view.step === 0 }, 'Back');
  const next = h('button', { type: 'button', class: 'primary setup-next' }, view.step === STEPS.length - 1 ? 'Finish' : 'Next');
  back.addEventListener('click', () => { view.step -= 1; paint(); });
  next.addEventListener('click', () => {
    if (view.step === STEPS.length - 1) { location.hash = '#/overview'; return; }
    view.step += 1;
    paint();
  });
  const card = h('section', { class: 'card setup-card', dataset: { step: String(view.step) } }, ...body);
  setChildren(view.main, h('h1', {}, 'Set up aicleaner'), tabs, card,
    h('div', { class: 'form-row setup-nav' }, back, next));
  if (status.remote) lockLocalActions(card);
}

// Through remote access the wizard is shown whole (E4, Brad 2026-10-19), but what runs this machine's
// scripts is disabled and says so - as Settings' local-only cards do. A step that draws more buttons
// later (remote access's stages) is watched, so a button added after the first paint is locked too.
// Routing the AI tools (step 4) reaches since J2 (Brad, 2026-10-20); setting up remote access installs
// cloudflared and a service, which prompt on the machine, so those stay here.
const LOCAL_ACTIONS = '.setup-remote-go, .setup-remote-replace';
const LOCAL_ONLY = 'Can only be changed where aicleaner is deployed';

function lockLocalActions(card) {
  const lock = () => {
    let any = false;
    for (const button of card.querySelectorAll(LOCAL_ACTIONS)) {
      if (!button.dataset.localOnly) {        // each button once; the observer watches children only
        button.disabled = true;
        button.title = LOCAL_ONLY;
        button.dataset.localOnly = '1';
      }
      any = true;
    }
    if (any && !card.querySelector('.local-only-note')) {
      const heading = card.querySelector('h2');
      const note = h('p', { class: 'chip local-only-note' }, `${LOCAL_ONLY}: this step's action runs on that machine.`);
      if (heading) heading.after(note); else card.prepend(note);
    }
  };
  lock();
  new MutationObserver(lock).observe(card, { childList: true, subtree: true });
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

function tools() {
  // Every AI tool aicleaner knows, found or not, filtered or not, signed in or not (item H).
  const table = aiToolsTable();
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
      + 'setting, after checking the filter is ready - a running tool picks it up without a restart. '
      + 'Each tool is also signed in to from here.'),
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

// ---- Remote access (feature 110; feedback items 7-11) --------------------------------------------
//
// In stages, so each screen asks one thing: should I (or tell me more), do you have the Cloudflare
// account it needs, API or by hand, then the details. What is already set up here is shown first,
// with Keep or Replace. Everything runs on this machine: POST /api/setup/remote starts
// Enable-RemoteControl.ps1 unattended, and its log is read until it says it is done.

const SIGN_IN = [
  { id: 'Cloudflare', label: 'Your Cloudflare account', note: 'Cloudflare\'s own sign-in and its MFA - nothing to register anywhere. Recommended.' },
  { id: 'OneTimePin', label: 'Email me a one-time PIN', note: 'Anyone who can read emails sent to the address below can sign in.' },
  { id: 'Google', label: 'A Google account', note: 'Google sign-in, with MFA on your phone. Needs an OAuth client made in Google Cloud first.' },
  { id: 'Microsoft', label: 'A Microsoft account or work ID', note: 'Microsoft sign-in and its MFA. Needs an app registration in Entra first.' },
  { id: 'GitHub', label: 'A GitHub account', note: 'GitHub sign-in and its MFA. Needs an OAuth app made in GitHub first.' },
];
const COMING_SOON = ['Facebook', 'LinkedIn', 'Okta', 'OneLogin', 'PingOne', 'JumpCloud', 'Any SAML provider', 'Any OpenID Connect provider'];
const NEEDS_CLIENT = new Set(['Google', 'Microsoft', 'GitHub']);
const CLIENT_STEPS = {
  Google: 'console.cloud.google.com > APIs & Services > Credentials > Create credentials > OAuth client ID > Web application.',
  Microsoft: 'entra.microsoft.com > Applications > App registrations > New registration (Web). For a personal account choose "Accounts in any organizational directory and personal Microsoft accounts".',
  GitHub: 'github.com/settings/developers > OAuth Apps > New OAuth App.',
};

function randomLabel() {
  const alphabet = 'abcdefghijkmnpqrstuvwxyz23456789';
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  return [...bytes].map((b) => alphabet[b % alphabet.length]).join('');
}

function remoteState() {
  if (!view.remote) view.remote = { stage: 'intro', state: null, account: '', zeroTrust: '', label: randomLabel(), identity: 'Cloudflare' };
  return view.remote;
}

function goRemote(stage) {
  remoteState().stage = stage;
  paint();
}

function remote() {
  const r = remoteState();
  if (!r.state) {
    getJSON('/api/setup/remote/state').then((state) => { r.state = state; paint(); })
      .catch(() => { r.state = { hostname: '', token_stored: false, feature_url: '' }; paint(); });
    return [h('h2', {}, 'Remote access (optional)'), h('p', { class: 'hint' }, 'Looking at what is set up here...')];
  }
  const body = { intro: remoteIntro, info: remoteInfo, account: remoteAccount, how: remoteHow, api: remoteApi }[r.stage] || remoteIntro;
  return [h('h2', {}, 'Remote access (optional)'), h('div', { class: 'setup-remote-stage', dataset: { stage: r.stage } }, ...body(r))];
}

function remoteIntro(r) {
  if (r.state.hostname) {
    const keep = h('button', { type: 'button', class: 'primary setup-remote-keep' }, 'Keep it');
    const replace = h('button', { type: 'button', class: 'setup-remote-replace' }, 'Replace it');
    const said = h('p', { class: 'setup-remote-said', role: 'status' });
    keep.addEventListener('click', () => { said.textContent = 'Kept as it is. Next goes on to your AI tools.'; });
    replace.addEventListener('click', async () => {
      replace.disabled = true;
      said.textContent = 'Taking it down - the tunnel, its DNS record and the sign-in rule...';
      try {
        const answer = await postJSON('/api/setup/remote/replace', {});
        if (!answer.ok) { said.textContent = 'It could not be taken down - see the output below.'; replace.disabled = false; return; }
        r.state = null;
        r.stage = 'intro';
        paint();
      } catch (error) { replace.disabled = false; sayError(error); }
    });
    return [
      h('p', { class: 'callout ok' }, `Remote access is already set up: https://${r.state.hostname}`
        + (r.state.sign_in ? ` (sign in with ${r.state.sign_in})` : '') + '.'),
      h('p', {}, 'Keep it as it is, or take it down and set it up again - a new tunnel, DNS record and sign-in rule.'),
      h('div', { class: 'form-row' }, keep, replace), said];
  }
  const setUp = h('button', { type: 'button', class: 'primary setup-remote-start' }, 'Set it up');
  const more = h('button', { type: 'button', class: 'setup-remote-more' }, 'More info, decide later');
  setUp.addEventListener('click', () => goRemote('account'));
  more.addEventListener('click', () => goRemote('info'));
  return [
    h('p', {}, 'If you own a domain and you can change where its DNS is run, you can create a private access connection through '
      + 'Cloudflare\'s free service and securely use aicleaner from any browser - even on your phone - signing in with your '
      + 'Cloudflare account, any email address, or your Google, Microsoft or GitHub login. ',
    h('a', { href: '#/wiki/Cloudflare-Tunnel' }, 'How it works, and why it is safe'), '.'),
    h('h3', {}, 'You will need'),
    h('ul', {},
      h('li', {}, 'A domain you own.'),
      h('li', {}, 'The ability to point its DNS at Cloudflare (', h('a', { href: '#/wiki/Repoint-DNS-cPanel' }, 'instructions for cPanel'),
        ') - completely reversible, and it does not disrupt email or your website.')),
    h('h3', {}, 'You will get'),
    h('ul', {},
      h('li', {}, 'A private address at your domain (https://anynameyouwant.yourdomain.com) with MFA sign-in in front of it.'),
      h('li', {}, 'Your AI sessions, reachable from anywhere you can sign in - and with the encrypted tunnel on, unreadable to anyone else, Cloudflare included.')),
    h('p', {}, 'No domain? No problem: choose More info and you will find how to get one, and how to come back to this later.'),
    h('div', { class: 'form-row' }, setUp, more)];
}

function remoteInfo(r) {
  const box = h('input', { type: 'text', class: 'setup-domain', placeholder: 'example.com', 'aria-label': 'Your domain',
    value: view.dns ? view.dns.domain : '', 'data-unsaved-ignore': '' });
  const look = h('button', { type: 'button', class: 'setup-lookup' }, 'Look it up');
  const report = h('div', { class: 'setup-dns' });
  const show = () => setChildren(report, ...dnsAdvice(view.dns));
  look.addEventListener('click', async () => {
    look.disabled = true;
    look.textContent = 'Looking...';
    try { view.dns = await getJSON(`/api/setup/dns?domain=${encodeURIComponent(box.value)}`); show(); } catch (error) { sayError(error); }
    look.disabled = false;
    look.textContent = 'Look it up';
  });
  box.addEventListener('keydown', (event) => { if (event.key === 'Enter') look.click(); });
  if (view.dns) show();
  const now = h('button', { type: 'button', class: 'primary setup-remote-start' }, 'Set it up now');
  now.addEventListener('click', () => goRemote('account'));
  return [
    h('p', {}, 'Remote access needs a domain whose DNS Cloudflare runs. No domain yet? Cloudflare sells them at cost '
      + '(Domain Registration > Register Domains, about US$10 a year), and one bought there is ready at once. '
      + 'Already have one elsewhere? Type it below: aicleaner looks at its DNS (read-only) and says what moving it involves.'),
    h('div', { class: 'form-row' }, box, look), report,
    h('p', { class: 'hint' }, 'Whenever you are ready: this step is in Settings too (Set up).'),
    h('div', { class: 'form-row' }, now)];
}

function yesNo(name, value, onChange) {
  const pick = (answer) => h('label', { class: 'check' },
    h('input', { type: 'radio', name, value: answer, checked: value === answer }), ` ${answer === 'yes' ? 'Yes' : 'No'}`);
  const group = h('div', { class: 'form-row setup-yesno', dataset: { question: name } }, pick('yes'), pick('no'));
  group.addEventListener('change', (event) => onChange(event.target.value));
  return group;
}

function remoteAccount(r) {
  const again = h('button', { type: 'button', class: 'setup-remote-recheck' }, 'Done - check again');
  again.addEventListener('click', () => { r.account = ''; r.zeroTrust = ''; paint(); });
  const next = h('button', { type: 'button', class: 'primary setup-remote-continue', disabled: !(r.account === 'yes' && r.zeroTrust === 'yes') }, 'Continue');
  next.addEventListener('click', () => goRemote('how'));
  const missing = r.account === 'no' || r.zeroTrust === 'no';
  return [
    h('h3', {}, 'A Cloudflare account with Zero Trust on?'),
    h('p', {}, 'How to check: open https://dash.cloudflare.com and click Zero Trust in the left-hand menu. If you are asked to '
      + 'choose a team name or a plan, Zero Trust is off. If a dashboard opens and Settings shows a team domain like '
      + '<team>.cloudflareaccess.com, it is on.'),
    h('p', {}, 'Do you have a Cloudflare account?'), yesNo('cf-account', r.account, (v) => { r.account = v; paint(); }),
    h('p', {}, 'Is Zero Trust on?'), yesNo('cf-zerotrust', r.zeroTrust, (v) => { r.zeroTrust = v; paint(); }),
    missing ? h('div', { class: 'callout warn setup-remote-guide' },
      h('ol', {},
        r.account === 'no' ? h('li', {}, 'Sign up for a free account at https://dash.cloudflare.com/sign-up.') : null,
        h('li', {}, 'In the dashboard click Zero Trust, choose a team name (it becomes <team>.cloudflareaccess.com), and pick the Free plan - it asks for a payment method but charges nothing.'),
        h('li', {}, 'Come back here and check again.')),
      h('div', { class: 'form-row' }, again)) : null,
    h('div', { class: 'form-row' }, next)];
}

function remoteHow(r) {
  const api = h('button', { type: 'button', class: 'primary setup-remote-api' }, 'Set it up for me (API token)');
  const manual = h('button', { type: 'button', class: 'setup-remote-manual' }, 'I will do it by hand');
  const steps = h('div', { class: 'setup-remote-manual-steps', hidden: r.how !== 'manual' },
    h('ol', {},
      h('li', {}, 'Zero Trust > Networks > Tunnels > Create a tunnel (Cloudflared), named for this machine.'),
      h('li', {}, 'Choose Windows, copy the token from the install command, and keep it for step 5.'),
      h('li', {}, 'Public hostname: a new subdomain of your domain, service HTTP, URL 127.0.0.1:8787.'),
      h('li', {}, 'Zero Trust > Access > Applications > Add (Self-hosted) for that hostname, with a policy allowing your email address.'),
      h('li', {}, 'On this machine run installer\\Enable-RemoteControl.ps1 -Setup manual, which asks for the tunnel token and starts it at every sign-in.')),
    h('p', {}, h('a', { href: '#/wiki/Remote-access' }, 'Remote access, in full'), '.'));
  api.addEventListener('click', () => goRemote('api'));
  manual.addEventListener('click', () => { r.how = 'manual'; paint(); });
  return [
    h('h3', {}, 'There are two ways to set up the tunnel'),
    h('ul', {},
      h('li', {}, 'With an API token: you create a Cloudflare API token, and aicleaner uses it to set up the tunnel on your behalf. '
        + 'The token can only touch what this needs; other Cloudflare settings are not affected.'),
      h('li', {}, 'By hand: you follow the steps on the Cloudflare website.')),
    h('div', { class: 'form-row' }, api, manual), steps];
}

function remoteApi(r) {
  const box = h('input', { type: 'text', class: 'setup-domain', placeholder: 'example.com', 'aria-label': 'Your domain',
    value: view.dns ? view.dns.domain : '', 'data-unsaved-ignore': '' });
  const look = h('button', { type: 'button', class: 'setup-lookup' }, 'Look it up');
  const report = h('div', { class: 'setup-dns' });
  const show = () => setChildren(report, ...dnsAdvice(view.dns), ...(view.dns && view.dns.on_cloudflare && !view.dns.error ? [remoteForm(r, view.dns.domain)] : []));
  look.addEventListener('click', async () => {
    look.disabled = true;
    look.textContent = 'Looking...';
    try { view.dns = await getJSON(`/api/setup/dns?domain=${encodeURIComponent(box.value)}`); show(); } catch (error) { sayError(error); }
    look.disabled = false;
    look.textContent = 'Look it up';
  });
  box.addEventListener('keydown', (event) => { if (event.key === 'Enter') look.click(); });
  if (view.dns) show();
  return [h('p', {}, 'Which domain? aicleaner looks at its DNS (read-only) first.'), h('div', { class: 'form-row' }, box, look), report];
}

// What the lookup found, and - when the domain is not on Cloudflare yet - what to do about it.
export function dnsAdvice(dns) {
  if (!dns) return [];
  if (dns.error) return [h('p', { class: 'callout bad' }, dns.error)];
  const facts = h('dl', { class: 'setup-facts' },
    h('dt', {}, 'DNS hosted by'), h('dd', {}, dns.host || `not recognised (${dns.nameservers.join(', ')})`),
    h('dt', {}, 'Email'), h('dd', {}, dns.mail.length ? dns.mail.join(', ') : 'no mail records'),
    h('dt', {}, 'Mail protection'), h('dd', {}, [dns.spf ? 'SPF' : '', dns.dmarc ? 'DMARC' : ''].filter(Boolean).join(' and ') || 'none found'),
    h('dt', {}, 'Website'), h('dd', {}, dns.apex.length || dns.www.length ? [...new Set([...dns.apex, ...dns.www])].join(', ') : 'none found'));
  if (dns.on_cloudflare) {
    return [facts, h('p', { class: 'callout ok' }, `${dns.domain} is already on Cloudflare - nothing to move.`)];
  }
  const keep = dns.mail.length ? h('p', { class: 'callout warn' },
    `${dns.domain} carries email (${dns.mail[0]}${dns.mail.length > 1 ? ' and more' : ''}). Moving its DNS must keep every mail record - `
    + 'a missed one stops your email. If you are not sure, use a separate domain (option B).') : null;
  const cpanel = dns.kind === 'cpanel' ? h('li', {}, 'Your host uses cPanel: sign in, open Zone Editor, choose the domain, and Export the zone file '
    + '(or copy every record down). Cloudflare can import that file under DNS > Records > Import and Export. ',
  h('a', { href: '#/wiki/Repoint-DNS-cPanel' }, 'Step by step'), '.') : null;
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
      h('li', {}, 'It is on Cloudflare from the start - nothing about your main domain changes. Look the new domain up here.'))];
}

// The details, then Enable-RemoteControl.ps1 runs on this machine. It restarts the proxy at the end,
// so its log is read until it says it is done, through the restart.
function remoteForm(r, domain) {
  const featureUrl = (r.state && r.state.feature_url) || 'https://github.com/specul8/aicleaner/issues/new';
  const methods = h('fieldset', { class: 'setup-sign-in' }, h('legend', {}, 'How would you like to sign in to the remote page?'),
    ...SIGN_IN.map((m) => h('label', { class: 'check setup-sign-in-option' },
      h('input', { type: 'radio', name: 'sign-in', value: m.id, checked: r.identity === m.id }),
      ` ${m.label}`, h('span', { class: 'hint' }, ` - ${m.note}`))),
    ...COMING_SOON.map((name) => h('label', { class: 'check setup-sign-in-soon' },
      h('input', { type: 'radio', name: 'sign-in', disabled: true }), ` ${name} (coming soon) `,
      h('a', { href: `${featureUrl}?title=${encodeURIComponent(`Remote sign-in with ${name}`)}`, target: '_blank', rel: 'noopener' }, 'ask for it'))));
  methods.addEventListener('change', (event) => { if (event.target.name === 'sign-in') { r.identity = event.target.value; paint(); } });
  const pin = r.identity === 'OneTimePin';
  const who = h('input', { type: 'email', class: 'setup-remote-email', placeholder: 'you@example.com', 'aria-label': 'Who may sign in', 'data-unsaved-ignore': '' });
  const whoLabel = pin ? 'Which email address may sign in (the one-time PIN goes there)'
    : `Which account may sign in (the email address of your ${SIGN_IN.find((m) => m.id === r.identity).label.replace(/^(A|An|Your) /, '')})`;
  const clientId = h('input', { type: 'text', class: 'setup-idp-client', 'aria-label': 'Client ID', 'data-unsaved-ignore': '' });
  const clientSecret = h('input', { type: 'password', class: 'setup-idp-secret', autocomplete: 'off', 'aria-label': 'Client secret', 'data-unsaved-ignore': '' });
  const kind = h('select', { class: 'setup-idp-kind', 'aria-label': 'Which Microsoft accounts' },
    h('option', { value: 'personal' }, 'a personal account (outlook.com, hotmail.com) - or any'),
    h('option', { value: 'work' }, 'work accounts in one directory only'));
  const directory = h('input', { type: 'text', class: 'setup-idp-directory', placeholder: 'Entra directory (tenant) ID', 'aria-label': 'Directory ID', 'data-unsaved-ignore': '' });
  const team = 'https://<team>.cloudflareaccess.com/cdn-cgi/access/callback';
  const client = NEEDS_CLIENT.has(r.identity) ? h('div', { class: 'setup-idp' },
    h('p', {}, `First make the ${r.identity} side: ${CLIENT_STEPS[r.identity]} Its redirect (callback) URL is ${team}, with your team name.`),
    h('label', { class: 'field' }, 'Client ID', clientId),
    h('label', { class: 'field' }, 'Client secret', clientSecret),
    r.identity === 'Microsoft' ? h('label', { class: 'field' }, 'Who signs in', kind) : null,
    r.identity === 'Microsoft' ? h('label', { class: 'field' }, 'Directory ID (work accounts only)', directory) : null) : null;
  const label = h('input', { type: 'text', class: 'setup-remote-label', value: r.label, 'aria-label': 'Subdomain', 'data-unsaved-ignore': '' });
  label.addEventListener('input', () => { r.label = label.value.trim().toLowerCase(); });
  const stored = r.state && r.state.token_stored;
  const useStored = h('input', { type: 'checkbox', class: 'setup-remote-use-stored', checked: Boolean(stored) });
  const secret = h('input', { type: 'password', class: 'setup-remote-token', autocomplete: 'off', 'aria-label': 'Cloudflare API token', 'data-unsaved-ignore': '' });
  const idpAccess = pin ? 'Read' : 'Edit';
  const tokenSteps = h('ol', { class: 'setup-token-steps' },
    h('li', {}, 'In the Cloudflare dashboard, open My Profile > API Tokens > Create Token > Create Custom Token.'),
    h('li', {}, `Permissions: Account - Cloudflare Tunnel - Edit; Account - Access: Apps and Policies - Edit; Account - Access: Organizations, Identity Providers and Groups - ${idpAccess}; Zone - DNS - Edit. Zone resources: ${domain}.`),
    h('li', {}, 'Create it and copy the token (Cloudflare shows it once).'));
  const go = h('button', { type: 'button', class: 'primary setup-remote-go' }, 'Set up remote access');
  const out = h('pre', { class: 'setup-output setup-remote-log', hidden: true });
  const said = h('p', { class: 'setup-remote-said', role: 'status' });
  go.addEventListener('click', async () => {
    const host = `${(r.label || '').replace(/\.+$/, '')}.${domain}`;
    const body = { hostname: host, email: who.value, identity: r.identity };
    if (stored && useStored.checked) body.use_stored = true; else body.token = secret.value;
    if (NEEDS_CLIENT.has(r.identity)) {
      Object.assign(body, { client_id: clientId.value, client_secret: clientSecret.value });
      if (r.identity === 'Microsoft') Object.assign(body, { account_kind: kind.value, directory: directory.value });
    }
    go.disabled = true;
    try {
      await postJSON('/api/setup/remote', body);
    } catch (error) { go.disabled = false; sayError(error); return; }
    secret.value = '';                                  // neither is kept in the page once sent
    clientSecret.value = '';
    out.hidden = false;
    said.textContent = 'Setting up - the tunnel, its DNS record and the sign-in rule. The proxy restarts at the end.';
    for (let i = 0; i < 150; i += 1) {
      await new Promise((resolve) => { setTimeout(resolve, 2000); });
      let log;
      try { log = await getJSON('/api/setup/remote/log'); } catch (error) { continue; }   // restarting
      out.textContent = log.text;
      out.scrollTop = out.scrollHeight;
      if (log.done) {
        said.textContent = log.ok ? `Remote access is on: https://${host} - sign in as ${who.value}.`
          : 'It stopped with an error - the log above says where. Fix that and try again.';
        go.disabled = false;
        return;
      }
    }
    said.textContent = 'Still running after five minutes - see logs\\remote-setup.log.';
    go.disabled = false;
  });
  return h('div', { class: 'setup-remote' },
    methods, client,
    h('label', { class: 'field' }, whoLabel, who),
    h('p', { class: 'callout warn' }, 'Give the subdomain a name you will recognise, but others will not: it is published in certificate '
      + 'transparency logs. "cld-bsdq8" tells people nothing; "ACME-Claude" tells them you are associated with ACME and use Claude.'),
    h('label', { class: 'field' }, 'Subdomain', h('span', { class: 'setup-host' }, label, h('span', { class: 'setup-zone' }, `.${domain}`))),
    h('h3', {}, 'A Cloudflare API token'),
    stored ? h('label', { class: 'check' }, useStored, ' Use the token stored on this machine (from an earlier setup)') : null,
    tokenSteps,
    h('label', { class: 'field' }, stored ? 'Or a new token (replaces the stored one)' : 'Cloudflare API token', secret),
    h('div', { class: 'form-row' }, go), said, out);
}

// Casual chats (item J): a folder for chats that belong to no project - OneDrive or Google Drive, so what
// they make is synced - and the Casual chats project session in it, which Chat now in the menu opens.
// Reaches remotely as well: choosing a folder prompts nothing on the machine.
function casualChats() {
  const field = casualFolderField(null);
  return [
    h('h2', {}, 'Casual chats'),
    h('p', {}, 'Not every chat belongs to a client or a project. Choose a folder for the ones that do not: Chat now in the '
      + 'menu starts a new conversation there, and anything it makes is kept in that folder. A OneDrive or Google Drive '
      + 'folder is synced to your other devices.'),
    field.element,
  ];
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
