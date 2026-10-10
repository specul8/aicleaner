// The Add Client wizard (feature 111; spec docs/superpowers/specs/2026-10-10-new-user-install-design.md,
// part E). Opened by Client Filters' Add client, the menu's Add new client, the setup wizard and
// Review - until "Skip the wizard next time" is ticked, when they open the New Client form instead.
//
// What is typed is saved to a draft on this machine as it is typed (POST /api/client-drafts) and
// becomes a client only at Save on the last page; an unfinished draft can be resumed or deleted from
// the first. The arrows at the top left go back and forward between pages.

import { getJSON, postJSON, say, sayError } from '../api.js';
import { refreshStatus } from '../app.js';
import { h, kindLabel, setChildren } from '../dom.js';
import { folderPicker } from '../picker.js';

export const SKIP_KEY = 'aicleaner.clientWizard';
const PAGES = ['Intro', 'Client name', 'Alternate names', 'Domains', 'Email domains', 'Other identifiers', 'Scan a repository', 'Finish'];
const ID_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9_]|-){0,63}$/;

export function wizardSkipped() {
  try { return localStorage.getItem(SKIP_KEY) === 'off'; } catch (error) { return false; }
}

export function setWizardSkipped(skip) {
  try { if (skip) localStorage.setItem(SKIP_KEY, 'off'); else localStorage.removeItem(SKIP_KEY); } catch (error) { /* not kept */ }
}

// The same rule the server applies (clientdrafts.strip_scheme): acme.com from https://www.acme.com/x.
export function stripScheme(value) {
  return String(value || '').trim().replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').split(/[/?#]/)[0]
    .replace(/:\d+$/, '').replace(/\.+$/, '').toLowerCase();
}

const blank = () => ({ id: '', mode: 'client', client_id: '', label: '', page: 1, names: [], domains: [], emails: [], urls: [], found: [] });

export function openClientWizard(onCreated) {
  document.getElementById('client-wizard')?.remove();
  const wiz = { page: 0, draft: blank(), lookups: {}, finds: null, saving: null, onCreated };
  const body = h('div', { class: 'wizard-body' });
  const back = h('button', { type: 'button', class: 'wizard-back', 'aria-label': 'Back', title: 'Back' }, '<');
  const forward = h('button', { type: 'button', class: 'wizard-forward', 'aria-label': 'Forward', title: 'Forward' }, '>');
  const where = h('span', { class: 'wizard-where hint' });
  const close = h('button', { type: 'button', class: 'wizard-close', 'aria-label': 'Close' }, 'Close');
  const dialog = h('dialog', { id: 'client-wizard', class: 'sheet wizard' },
    h('div', { class: 'wizard-head' }, back, forward, where, close), body);
  document.body.append(dialog);
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => dialog.remove());
  back.addEventListener('click', () => go(-1));
  forward.addEventListener('click', () => go(1));

  // Saved as typed, a moment after the typing stops: a draft is never more than half a second behind.
  function changed() {
    clearTimeout(wiz.saving);
    wiz.saving = setTimeout(save, 400);
  }
  async function save() {
    clearTimeout(wiz.saving);
    if (!wiz.draft.client_id && !wiz.draft.names.length && wiz.draft.mode !== 'global') return;
    try {
      const answer = await postJSON('/api/client-drafts', { ...wiz.draft, page: wiz.page });
      wiz.draft.id = answer.draft.id;
    } catch (error) { sayError(error); }
  }
  const order = () => (wiz.draft.mode === 'global' ? [0, 2, 3, 4, 5, 6, 7] : [0, 1, 2, 3, 4, 5, 6, 7]);
  function go(step) {
    const pages = order();
    const at = pages.indexOf(wiz.page);
    const next = pages[Math.max(0, Math.min(pages.length - 1, at + step))];
    if (wiz.page === 1 && step > 0 && !ID_PATTERN.test(wiz.draft.client_id)) {
      say('Give the client an id first: letters, digits, hyphens and underscores.', 'warn');
      return;
    }
    wiz.page = next;
    save();
    paint();
  }
  function paint() {
    const pages = order();
    back.disabled = wiz.page === 0;
    forward.disabled = wiz.page === 0 || wiz.page === 7;
    where.textContent = `${pages.indexOf(wiz.page) + 1} of ${pages.length}: ${PAGES[wiz.page]}`;
    const page = [intro, clientName, altNames, domains, emails, others, scan, finish][wiz.page];
    setChildren(body, ...page(wiz, { changed, go, paint, dialog }));
    dialog.dataset.page = String(wiz.page);
  }
  paint();
  dialog.showModal();
  return dialog;
}

const nextButton = (go, label = 'Next') => {
  const button = h('button', { type: 'button', class: 'primary wizard-next' }, label);
  button.addEventListener('click', () => go(1));
  return h('div', { class: 'form-row wizard-nav' }, button);
};

// A list of values, one per row: typed into, removed, added to. `fixed` rows lead and are not edited.
function listEditor(values, { placeholder = '', normalise = (v) => v.trim(), fixed = [], onChange, onAdd } = {}) {
  const rows = h('div', { class: 'wizard-list' });
  const paintRows = () => {
    setChildren(rows,
      ...fixed.filter(Boolean).map((value) => h('div', { class: 'wizard-row fixed' }, h('span', { class: 'mono' }, value), h('span', { class: 'hint' }, ' (from the client name)'))),
      ...values.map((value, i) => {
        const input = h('input', { type: 'text', value, 'aria-label': placeholder || 'value', 'data-unsaved-ignore': '' });
        input.addEventListener('change', () => {
          const clean = normalise(input.value);
          values[i] = clean;
          input.value = clean;
          onChange();
          if (onAdd && clean) onAdd(clean);
        });
        const remove = h('button', { type: 'button', class: 'link wizard-remove', 'aria-label': `Remove ${value}` }, 'Remove');
        remove.addEventListener('click', () => { values.splice(i, 1); onChange(); paintRows(); });
        return h('div', { class: 'wizard-row' }, input, remove);
      }));
  };
  const box = h('input', { type: 'text', class: 'wizard-add-box', placeholder, 'aria-label': placeholder || 'Add', 'data-unsaved-ignore': '' });
  const add = h('button', { type: 'button', class: 'wizard-add' }, 'Add');
  const doAdd = () => {
    const clean = normalise(box.value);
    if (!clean) return;
    if (!values.includes(clean) && !fixed.includes(clean)) values.push(clean);
    box.value = '';
    onChange();
    paintRows();
    if (onAdd) onAdd(clean);
    box.focus();
  };
  add.addEventListener('click', doAdd);
  box.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); doAdd(); } });
  paintRows();
  return h('div', { class: 'wizard-list-editor' }, rows, h('div', { class: 'form-row' }, box, add));
}

function intro(wiz, { go, paint }) {
  const start = h('button', { type: 'button', class: 'primary wizard-start-client' }, 'Enter Client Name');
  const global = h('button', { type: 'button', class: 'wizard-start-global' }, 'Add Global Info');
  start.addEventListener('click', () => { wiz.draft = blank(); go(1); });
  global.addEventListener('click', () => { wiz.draft = { ...blank(), mode: 'global' }; wiz.page = 2; paint(); });
  const list = h('div', { class: 'wizard-drafts' });
  getJSON('/api/client-drafts').then(({ drafts }) => {
    if (!drafts.length) return;
    setChildren(list, h('h3', {}, 'Unfinished'),
      ...drafts.map((d) => {
        const resume = h('button', { type: 'button', class: 'wizard-resume' }, 'Resume');
        const remove = h('button', { type: 'button', class: 'link wizard-delete' }, 'Delete');
        resume.addEventListener('click', async () => {
          try {
            wiz.draft = { ...blank(), ...(await getJSON(`/api/client-drafts/${d.id}`)) };
            wiz.page = Math.min(7, Math.max(wiz.draft.mode === 'global' ? 2 : 1, Number(wiz.draft.page) || 1));
            paint();
          } catch (error) { sayError(error); }
        });
        remove.addEventListener('click', async () => {
          try { await postJSON(`/api/client-drafts/${d.id}/delete`, {}); paint(); } catch (error) { sayError(error); }
        });
        return h('div', { class: 'wizard-draft' },
          h('span', {}, d.mode === 'global' ? 'Global info' : (d.label || d.client_id || 'unnamed client')), resume, remove);
      }));
  }).catch(() => {});
  return [
    h('h2', {}, 'Add a client'),
    h('p', {}, 'A client is one engagement. Over the next few pages you tell aicleaner the names, domains and other '
      + 'identifiers that would show who the client is; from then on they are replaced with stand-ins before anything '
      + 'reaches the AI service. What you type is kept as a draft as you go, and nothing becomes a client until you '
      + 'Save on the last page.'),
    h('p', { class: 'hint' }, 'Global info is the same pages for things to hide whoever you work for - your own company and domains.'),
    h('div', { class: 'form-row' }, start, global), list];
}

function clientName(wiz, { changed, go }) {
  const d = wiz.draft;
  const id = h('input', { type: 'text', class: 'wizard-client-id', value: d.client_id, maxlength: '64', placeholder: 'e.g. acme', 'data-unsaved-ignore': '' });
  const label = h('input', { type: 'text', class: 'wizard-label', value: d.label, maxlength: '80', 'data-unsaved-ignore': '' });
  const hintFor = () => (d.client_id ? `leave blank to use ${d.client_id} as the Display name` : 'e.g. ACME Pty Ltd');
  label.placeholder = hintFor();
  id.addEventListener('input', () => { d.client_id = id.value.trim(); label.placeholder = hintFor(); changed(); });
  label.addEventListener('input', () => { d.label = label.value.trim(); changed(); });
  return [
    h('h2', {}, 'Client name'),
    h('label', { class: 'field wide' }, 'Client ID', id,
      h('span', { class: 'hint' }, 'Names the client\'s file and keys the mapping. Letters, digits, hyphens and underscores.')),
    h('label', { class: 'field wide' }, 'Display name', label,
      h('span', { class: 'hint' }, 'What you read, everywhere in here.')),
    h('p', { class: 'hint' }, 'The number inside the stand-ins (Client07, client07.invalid) is worked out from the id; it is shown on the last page.'),
    nextButton(go)];
}

function altNames(wiz, { changed, go }) {
  const d = wiz.draft;
  const display = d.label || d.client_id;
  const fixed = d.mode === 'global' ? [] : [d.client_id, d.label].filter((v, i, all) => v && all.indexOf(v) === i);
  return [
    h('h2', {}, d.mode === 'global' ? 'Your own names' : 'Alternate names'),
    h('p', {}, d.mode === 'global'
      ? 'Names to hide whoever the work is for: your company, your brands.'
      : 'Any other name that makes it obvious which company it is. Just the common ones: what you call this client, '
        + 'brands the company owns or trades under, acronyms and what they stand for.'),
    h('p', { class: 'hint' }, `For example: ${display ? `${display} Pty Ltd, ` : ''}ACME, ACME Inc, A Company Making Everything.`),
    listEditor(d.names, { placeholder: 'Another name', fixed, onChange: changed }),
    h('details', { class: 'wizard-why' }, h('summary', {}, 'Names inside other words'),
      h('p', {}, 'A name is replaced where it stands on its own, and where it is joined to other words at a capital letter '
        + '(newAcme, AcmeApps, ACMEApproved) - whatever its case. It is not replaced inside a lower-case run of letters '
        + '(acmeish, pacmen). Longer names that contain it, such as acmevault01, are offered to you in Review rather than replaced.')),
    nextButton(go)];
}

function domains(wiz, { changed, go, paint }) {
  const d = wiz.draft;
  const found = h('div', { class: 'wizard-lookups hint' });
  const showLookups = () => setChildren(found, ...Object.values(wiz.lookups).map((l) => h('div', {},
    l.pending ? `Looking ${l.domain} up at Microsoft...` : l.error ? `${l.domain}: ${l.error}.`
      : `${l.domain}: a Microsoft tenant - ${l.suggestions.length} suggestion${l.suggestions.length === 1 ? '' : 's'} on the Other identifiers page.`)));
  const lookUp = async (domain) => {
    if (!domain || wiz.lookups[domain]) return;
    wiz.lookups[domain] = { domain, pending: true, suggestions: [] };
    showLookups();
    try { wiz.lookups[domain] = await postJSON('/api/client-wizard/lookup', { domain }); } catch (error) {
      wiz.lookups[domain] = { domain, error: 'could not be looked up', suggestions: [] };
    }
    showLookups();
  };
  showLookups();
  return [
    h('h2', {}, 'Domains'),
    h('p', {}, 'The domains the company uses, for example acme.com or acme.com.au. Typing https:// or a path is fine: '
      + 'it is taken off, so every form of the address is caught - http://, https://, smtp:// and the rest.'),
    listEditor(d.domains, { placeholder: 'acme.com', normalise: stripScheme, onChange: changed, onAdd: lookUp }),
    h('p', { class: 'hint' }, 'Each domain is looked up at Microsoft as you add it - only the domain is sent, to '
      + 'login.microsoftonline.com and autodiscover-s.outlook.com - to find the tenant, its SharePoint and its other domains.'),
    found,
    h('p', { class: 'hint' }, 'Coming later: uploading a text file of the company\'s domains.'),
    nextButton(go)];
}

function emails(wiz, { changed, go }) {
  const d = wiz.draft;
  for (const domain of d.domains) {
    const rule = `*@${domain}`;
    if (!d.emails.includes(rule) && !(wiz.emailsSeen || []).includes(rule)) d.emails.push(rule);
  }
  wiz.emailsSeen = [...new Set([...(wiz.emailsSeen || []), ...d.domains.map((x) => `*@${x}`)])];
  changed();
  return [
    h('h2', {}, 'Email domains'),
    h('p', {}, 'Every address at these domains is the client\'s: it is replaced whoever it belongs to. The domains '
      + 'from the last page are here already; add any email domain that is not among them.'),
    listEditor(d.emails, { placeholder: '*@acme-mail.com', normalise: (v) => { const x = stripScheme(String(v).replace(/^\*?@/, '')); return x ? `*@${x}` : ''; }, onChange: changed }),
    nextButton(go)];
}

function others(wiz, { changed, go, paint }) {
  const d = wiz.draft;
  const suggestions = Object.values(wiz.lookups).flatMap((l) => (l.suggestions || []).map((s) => ({ ...s, from: l.domain })));
  const target = (s) => (s.kind === 'domain' ? d.domains : s.kind === 'name' ? d.names : d.urls);
  const ticks = suggestions.length ? h('fieldset', { class: 'wizard-suggestions' }, h('legend', {}, 'Found at Microsoft - tick what is theirs'),
    ...suggestions.map((s) => {
      const box = h('input', { type: 'checkbox', checked: target(s).includes(s.value) });
      box.addEventListener('change', () => {
        const list = target(s);
        if (box.checked && !list.includes(s.value)) list.push(s.value);
        if (!box.checked && list.includes(s.value)) list.splice(list.indexOf(s.value), 1);
        changed();
      });
      return h('label', { class: 'check' }, box, ` ${s.value} `, h('span', { class: 'hint' }, `(${kindLabel(s.kind)}: ${s.why}, from ${s.from})`));
    })) : null;
  return [
    h('h2', {}, 'Other identifiers'),
    h('p', {}, 'Anything else that points at this client alone: the Azure DevOps organisation (dev.azure.com/<org>), '
      + 'the SharePoint address (its -my and -admin twins are added for you), other Microsoft endpoints, GitHub '
      + 'organisations or repositories, AWS account aliases or Google Cloud projects.'),
    ticks,
    listEditor(d.urls, { placeholder: 'acme.sharepoint.com', onChange: changed }),
    nextButton(go)];
}

function scan(wiz, { changed, go, paint }) {
  const d = wiz.draft;
  const folder = h('input', { type: 'text', class: 'wizard-scan-folder', placeholder: 'A folder with a git repository of theirs', 'data-unsaved-ignore': '' });
  const run = h('button', { type: 'button', class: 'wizard-scan-run' }, 'Scan');
  const results = h('div', { class: 'wizard-finds' });
  const show = () => {
    if (!wiz.finds) return;
    if (!wiz.finds.length) { setChildren(results, h('p', { class: 'hint' }, 'Nothing new turned up.')); return; }
    const kinds = [...new Set(wiz.finds.map((f) => f.kind))];
    setChildren(results, ...kinds.map((kind) => h('fieldset', {}, h('legend', {}, kindLabel(kind)),
      ...wiz.finds.filter((f) => f.kind === kind).map((f) => {
        const box = h('input', { type: 'checkbox', checked: d.found.includes(f.value) });
        box.addEventListener('change', () => {
          if (box.checked && !d.found.includes(f.value)) d.found.push(f.value);
          if (!box.checked) d.found = d.found.filter((v) => v !== f.value);
          changed();
        });
        return h('label', { class: 'check wizard-find' }, box, ` ${f.value} `, h('span', { class: 'hint' }, `(${f.count}x${f.theirs ? ', carries their name' : ''})`));
      }))));
  };
  run.addEventListener('click', async () => {
    run.disabled = true;
    run.textContent = 'Scanning...';
    try {
      await new Promise((resolve) => { setTimeout(resolve, 450); });   // the draft is saved first
      const answer = await postJSON('/api/client-wizard/scan', { folder: folder.value, draft: d.id });
      wiz.finds = answer.finds;
      show();
    } catch (error) { sayError(error); }
    run.disabled = false;
    run.textContent = 'Scan';
  });
  show();
  return [
    h('h2', {}, 'Scan a repository (optional)'),
    h('p', {}, 'If you have a local copy of a git repository with this client\'s work in it, aicleaner can read it for '
      + 'more of their identifiers. Tick the ones that are theirs; nothing goes to Review. Skip this if you have none.'),
    h('div', { class: 'form-row' }, folder, folderPicker(folder, { title: 'A repository to scan' }), run),
    results,
    h('div', { class: 'form-row wizard-nav' },
      (() => { const b = h('button', { type: 'button', class: 'wizard-skip' }, 'Skip'); b.addEventListener('click', () => go(1)); return b; })(),
      (() => { const b = h('button', { type: 'button', class: 'primary wizard-next' }, 'Next'); b.addEventListener('click', () => go(1)); return b; })())];
}

function finish(wiz, { dialog }) {
  const d = wiz.draft;
  const summary = h('div', { class: 'wizard-summary' }, h('p', { class: 'hint' }, 'Counting...'));
  const skip = h('input', { type: 'checkbox', class: 'wizard-skip-next', checked: wizardSkipped() });
  skip.addEventListener('change', () => setWizardSkipped(skip.checked));
  const saveButton = h('button', { type: 'button', class: 'primary wizard-save' }, 'Save');
  const cancel = h('button', { type: 'button', class: 'wizard-cancel' }, 'Cancel');
  cancel.addEventListener('click', () => { if (d.id) say('Kept as a draft: open Add client to resume it.', 'info'); dialog.close(); });
  const load = async () => {
    await new Promise((resolve) => { setTimeout(resolve, 450); });
    if (!d.id) { setChildren(summary, h('p', { class: 'hint' }, 'Nothing entered yet.')); return; }
    try {
      const s = await getJSON(`/api/client-drafts/${d.id}/summary`);
      setChildren(summary,
        h('dl', { class: 'setup-facts' },
          h('dt', {}, d.mode === 'global' ? 'Adding to' : 'Client'), h('dd', {}, d.mode === 'global' ? 'Global' : `${d.label || d.client_id} (${d.client_id})`),
          d.mode === 'global' ? null : h('dt', {}, 'Index'), d.mode === 'global' ? null : h('dd', {}, String(s.index)),
          h('dt', {}, 'Entries'), h('dd', {}, String(s.entries))),
        h('ul', { class: 'wizard-counts' }, ...Object.entries(s.kinds).map(([kind, n]) => h('li', {}, `${kindLabel(kind)}: ${n}`))),
        s.email_rules.length ? h('p', {}, `Every address at ${s.email_rules.join(', ')} counts as theirs.`) : null);
    } catch (error) { sayError(error); }
  };
  load();
  saveButton.addEventListener('click', async () => {
    saveButton.disabled = true;
    try {
      const done = await postJSON(`/api/client-drafts/${d.id}/finish`, {});
      await refreshStatus();
      say(`Saved ${d.mode === 'global' ? 'to Global' : (d.label || d.client_id)}: ${done.added} entr${done.added === 1 ? 'y' : 'ies'}.`, 'ok');
      dialog.close();
      if (wiz.onCreated) wiz.onCreated(done.client);
      else window.location.hash = `#/clients/${encodeURIComponent(done.client)}`;
    } catch (error) { saveButton.disabled = false; sayError(error); }
  });
  return [
    h('h2', {}, 'Finish'),
    h('p', {}, 'This is what will be added. Save makes it a client; Cancel keeps it as a draft to come back to.'),
    summary,
    h('label', { class: 'check' }, skip, ' Skip the wizard next time (Add client opens the short form instead)'),
    h('div', { class: 'form-row wizard-nav' }, saveButton, cancel)];
}
