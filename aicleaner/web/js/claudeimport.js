// Importing a project from claude.ai (2026-10-16, item H; spec 2026-10-16-h-claude-ai-import-design.md).
//
// One dialog, its steps along the top: ask claude.ai for the export, choose the manifest, download
// each file (picked up from Downloads on this machine by itself, or chosen and uploaded through
// remote access), choose the project and its chats, say where it goes, and import. The server does
// the reading and writing; nothing from the export leaves this machine.
import { getJSON, postJSON, say, sayError } from './api.js';
import { h } from './dom.js';
import { sendInPieces } from './pieces.js';

const STEPS = ['Ask for the export', 'The manifest', 'Download the files', 'The project', 'Its chats', 'Where it goes', 'Import'];
const EXPORT_PAGE = 'https://claude.ai/settings/data-privacy-controls';
const POLL_MS = 2000;

function dateOf(value) {
  const at = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(at.getTime()) ? '' : at.toLocaleDateString();
}

// The page's choice of a downloaded file, sent in order, piece by piece.
function uploadFile(job, name, file, progress) {
  return sendInPieces('/api/claude-import/upload', { job, file: name }, file, progress);
}

// Opens the wizard. `name` is the profile shown ('' imports as a new client); `opened(name)` is
// called with the profile to show when the person chooses Open the session.
export function openClaudeImport({ name = '', opened = () => {} } = {}) {
  if (document.getElementById('claude-import')) return;
  const state = {
    step: 0, job: '', files: [], watching: [], folder: '', projects: [], project: null, chats: [], ticked: new Set(),
    filter: '', client: '', place: '', share: 'create', sharePath: '', pairs: [['', '']], memories: true, keepZips: false,
    report: null,
  };
  let timer = 0;
  const steps = h('ol', { class: 'ci-steps' });
  const body = h('div', { class: 'ci-body' });
  const back = h('button', { type: 'button', class: 'ci-back' }, 'Back');
  const next = h('button', { type: 'button', class: 'primary ci-next' }, 'Next');
  const cancel = h('button', { type: 'button', class: 'ci-cancel' }, 'Cancel');
  const dialog = h('dialog', { id: 'claude-import', class: 'sheet ci-dialog', 'aria-label': 'Import from claude.ai' },
    h('h2', {}, name ? `Import from claude.ai into ${name}` : 'Import from claude.ai as a new client'),
    steps, body, h('div', { class: 'fb-footer' }, cancel, h('span', {}, back, ' ', next)));

  const stopPolling = () => { if (timer) { clearInterval(timer); timer = 0; } };
  const close = async (ended = false) => {
    stopPolling();
    if (state.job && !ended) postJSON('/api/claude-import/cancel', { job: state.job }).catch(() => {});
    dialog.close();
    dialog.remove();
  };
  const ticked = () => state.files.filter((f) => f.ticked);
  const ready = () => {
    switch (state.step) {
      case 1: return Boolean(state.job);
      case 2: return ticked().every((f) => f.state === 'received');
      case 3: return Boolean(state.project);
      case 4: return state.ticked.size > 0;
      case 5: return Boolean(name || state.client.trim());
      default: return true;
    }
  };
  const go = (step) => { state.step = step; render(); };

  // ---- the steps --------------------------------------------------------------------------------

  const askStep = () => h('div', {},
    h('p', {}, 'claude.ai sends your data as an export: open its settings, choose ',
      h('strong', {}, 'Privacy > Export data'), ', and it e-mails you a link.'),
    h('p', {}, h('a', { href: EXPORT_PAGE, target: '_blank', rel: 'noopener', class: 'ci-export-link' }, 'Open claude.ai\'s settings')),
    h('p', { class: 'hint' }, 'The e-mail can take minutes or hours to arrive, and its links last 24 hours. '
      + 'The export holds every chat the account has; only the project you choose is imported, and the copies '
      + 'aicleaner takes are deleted afterwards.'));

  const manifestStep = () => {
    const input = h('input', { type: 'file', accept: '.json,application/json', class: 'ci-manifest-file' });
    const drop = h('div', { class: 'ci-drop' }, 'Drop the manifest here, or ', input);
    const take = async (file) => {
      if (!file) return;
      try {
        const doc = JSON.parse(await file.text());
        const answer = await postJSON('/api/claude-import/manifest', { manifest: doc });
        if (state.job) postJSON('/api/claude-import/cancel', { job: state.job }).catch(() => {});
        Object.assign(state, { job: answer.job, files: answer.files, watching: answer.watching || [] });
        render();
      } catch (error) { sayError(error instanceof SyntaxError ? new Error('That file is not JSON: choose the manifest-....json file') : error); }
    };
    input.addEventListener('change', () => take(input.files[0]));
    drop.addEventListener('dragover', (event) => { event.preventDefault(); drop.classList.add('over'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('over'));
    drop.addEventListener('drop', (event) => { event.preventDefault(); drop.classList.remove('over'); take(event.dataTransfer.files[0]); });
    const rows = state.files.map((file) => {
      const box = h('input', { type: 'checkbox', checked: file.ticked ? true : null, disabled: file.required ? true : null });
      box.addEventListener('change', () => { file.ticked = box.checked; });
      return h('li', { class: 'ci-file' }, h('label', {}, box, ' ', h('span', { class: 'mono' }, file.filename),
        h('span', { class: 'hint' }, file.required ? ' - needed' : (file.category === 'memories' ? ' - claude.ai\'s memory of your projects'
          : ' - not needed for the import'))));
    });
    return h('div', {},
      h('p', {}, 'The e-mail\'s link downloads a manifest - ', h('span', { class: 'mono' }, 'manifest-....json'), '. Choose it here.'),
      drop,
      rows.length ? h('ul', { class: 'ci-files' }, ...rows) : null);
  };

  const downloadStep = () => {
    const rows = ticked().map((file, index) => {
      const status = h('span', { class: `ci-state ci-${file.state}` },
        file.state === 'received' ? 'received' : file.state === 'bad' ? file.problem : 'waiting');
      const link = h('a', { href: file.url, target: '_blank', rel: 'noopener', class: 'button ci-download' }, 'Download');
      const chooser = h('input', { type: 'file', accept: '.zip', class: 'ci-upload', 'aria-label': `Choose ${file.filename}` });
      chooser.addEventListener('change', async () => {
        const chosen = chooser.files[0];
        if (!chosen) return;
        try {
          status.textContent = 'sending...';
          await uploadFile(state.job, file.filename, chosen, (sent, total) => {
            status.textContent = `sending ${Math.round((100 * sent) / total)}%`;
          });
          file.state = 'received';
          render();
        } catch (error) { status.textContent = ''; sayError(error); }
      });
      const first = ticked().findIndex((f) => f.state !== 'received');
      return h('li', { class: `ci-file${index === first ? ' ci-next-file' : ''}`, 'data-file': file.filename },
        h('span', { class: 'mono' }, file.filename), ' ', status, ' ',
        file.state === 'received' ? null : h('span', { class: 'ci-actions' }, link, ' ',
          h('label', { class: 'button ci-choose' }, 'Choose the downloaded file', chooser)));
    });
    const other = h('input', { type: 'text', class: 'ci-folder', placeholder: 'Another folder to look in', value: state.folder });
    other.addEventListener('change', () => { state.folder = other.value; });
    return h('div', {},
      h('p', {}, 'Download each file in turn. The first may stop at claude.ai\'s sign-in: sign in on the tab that '
        + 'opened, then come back. The next ones download straight away.'),
      state.watching.length
        ? h('p', { class: 'hint' }, `On this machine each file is picked up from ${state.watching.join(', ')} by itself. `
          + 'Through remote access, choose each downloaded file instead.')
        : h('p', { class: 'hint' }, 'Through remote access the files land on this device: choose each one after it downloads.'),
      h('ul', { class: 'ci-files' }, ...rows),
      state.watching.length ? h('label', { class: 'field' }, 'Saved somewhere else?', other) : null,
      ticked().some((f) => f.state === 'bad')
        ? h('p', { class: 'callout' }, 'A file that is not a zip usually means the links have expired: ask claude.ai for a new export.')
        : null);
  };

  const projectStep = () => h('div', {},
    h('p', {}, 'Which project?'),
    h('ul', { class: 'ci-projects' }, ...state.projects.map((project) => {
      const radio = h('input', { type: 'radio', name: 'ci-project', value: project.uuid,
        checked: state.project && state.project.uuid === project.uuid ? true : null });
      radio.addEventListener('change', () => {
        if (!state.project || state.project.uuid !== project.uuid) { state.chats = []; state.ticked = new Set(); }
        state.project = project;
        if (!state.client) state.client = project.name;
        render();
      });
      return h('li', { class: 'ci-project' }, h('label', {}, radio, ' ', h('strong', {}, project.name),
        h('span', { class: 'hint' }, ` - ${project.docs} document${project.docs === 1 ? '' : 's'}, updated ${dateOf(project.updated)}`)),
      project.description ? h('p', { class: 'hint' }, project.description) : null,
      project.instructions ? h('pre', { class: 'ci-instructions' }, project.instructions) : null);
    })));

  const chatsStep = () => {
    const search = h('input', { type: 'search', class: 'ci-filter', placeholder: 'Narrow the list', value: state.filter });
    const shown = () => state.chats.filter((chat) => !state.filter
      || `${chat.title} ${(chat.passage && chat.passage.text) || ''}`.toLowerCase().includes(state.filter.toLowerCase()));
    const count = h('span', { class: 'ci-count' }, `${state.ticked.size} chosen of ${state.chats.length}`);
    const table = h('tbody', {});
    const fill = () => {
      table.replaceChildren(...shown().map((chat) => {
        const box = h('input', { type: 'checkbox', class: 'ci-chat', value: chat.id, checked: state.ticked.has(chat.id) ? true : null });
        box.addEventListener('change', () => {
          if (box.checked) state.ticked.add(chat.id); else state.ticked.delete(chat.id);
          count.textContent = `${state.ticked.size} chosen of ${state.chats.length}`;
          next.disabled = !ready();
        });
        return h('tr', { class: chat.imported ? 'ci-imported' : '' }, h('td', {}, box),
          h('td', {}, h('strong', {}, chat.title), chat.imported ? h('span', { class: 'hint' }, ' (already imported)') : null,
            chat.passage ? h('div', { class: 'hint ci-passage' }, chat.passage.text) : null),
          h('td', { class: 'ci-when' }, dateOf(chat.updated)), h('td', { class: 'ci-n' }, String(chat.messages)));
      }));
    };
    search.addEventListener('input', () => { state.filter = search.value; fill(); });
    const all = h('button', { type: 'button', class: 'link ci-all' }, 'Select all');
    const none = h('button', { type: 'button', class: 'link ci-none' }, 'Select none');
    const pick = h('button', { type: 'button', class: 'ci-pick', title: 'One request to Claude, through aicleaner: the project and every chat\'s title and summary' },
      'Ask Claude to choose');
    all.addEventListener('click', () => { for (const chat of shown()) state.ticked.add(chat.id); render(); });
    none.addEventListener('click', () => { for (const chat of shown()) state.ticked.delete(chat.id); render(); });
    pick.addEventListener('click', async () => {
      pick.disabled = true;
      pick.textContent = 'Claude is choosing...';
      try {
        const answer = await postJSON('/api/claude-import/pick', { job: state.job, project: state.project.uuid });
        const imported = new Set(state.chats.filter((c) => c.imported).map((c) => c.id));
        state.ticked = new Set((answer.chats || []).filter((id) => !imported.has(id)));
        say(`Claude chose ${state.ticked.size} chat${state.ticked.size === 1 ? '' : 's'}.`, 'ok');
        render();
      } catch (error) { sayError(error); pick.disabled = false; pick.textContent = 'Ask Claude to choose'; }
    });
    fill();
    return h('div', {},
      h('p', {}, 'claude.ai does not record which chats were the project\'s. They are listed most-related first, '
        + 'the clear ones ticked; ask Claude to choose for a closer reading.'),
      h('div', { class: 'form-row ci-chat-tools' }, search, all, none, pick, count),
      h('div', { class: 'ci-chat-scroll' }, h('table', { class: 'ci-chats' },
        h('thead', {}, h('tr', {}, h('th', {}, ''), h('th', {}, 'Chat'), h('th', {}, 'Updated'), h('th', {}, 'Messages'))), table)));
  };

  const whereStep = () => {
    const parts = [];
    if (name) {
      parts.push(h('p', {}, 'Into ', h('strong', {}, name), ': its home folder, its client and its share.'));
    } else {
      const client = h('input', { type: 'text', class: 'ci-client', value: state.client, maxlength: '64' });
      const place = h('input', { type: 'text', class: 'ci-place', value: state.place, placeholder: `C:\\Repos\\${state.client || 'Name'}` });
      const shareChoice = h('select', { class: 'ci-share' },
        h('option', { value: 'create', selected: state.share === 'create' ? true : null }, 'Create one in OneDrive'),
        h('option', { value: 'folder', selected: state.share === 'folder' ? true : null }, 'Use a folder'),
        h('option', { value: 'none', selected: state.share === 'none' ? true : null }, 'No share'));
      const sharePath = h('input', { type: 'text', class: 'ci-share-path', value: state.sharePath, hidden: state.share === 'folder' ? null : true });
      client.addEventListener('input', () => {
        state.client = client.value;
        place.placeholder = `C:\\Repos\\${client.value || 'Name'}`;
        next.disabled = !ready();
      });
      place.addEventListener('input', () => { state.place = place.value; });
      shareChoice.addEventListener('change', () => { state.share = shareChoice.value; sharePath.hidden = state.share !== 'folder'; });
      sharePath.addEventListener('input', () => { state.sharePath = sharePath.value; });
      parts.push(
        h('label', { class: 'field' }, 'The client\'s name (used exactly as typed)', client),
        h('label', { class: 'field' }, 'The project folder: it gets a local-only _<name>-tooling repository as the session\'s home', place),
        h('label', { class: 'field' }, 'The OneDrive share', shareChoice, sharePath));
    }
    const pairRows = state.pairs.map((pair, index) => {
      const old = h('input', { type: 'text', class: 'ci-pair-old', value: pair[0], placeholder: 'C:\\Users\\me\\OneDrive\\Old folder' });
      const now = h('input', { type: 'text', class: 'ci-pair-new', value: pair[1], placeholder: 'the folder it is now' });
      old.addEventListener('input', () => { state.pairs[index][0] = old.value; });
      now.addEventListener('input', () => { state.pairs[index][1] = now.value; });
      return h('div', { class: 'form-row ci-pair' }, old, h('span', {}, ' becomes '), now);
    });
    const addPair = h('button', { type: 'button', class: 'link ci-add-pair' }, 'Add another');
    addPair.addEventListener('click', () => { state.pairs.push(['', '']); render(); });
    const memories = h('input', { type: 'checkbox', checked: state.memories ? true : null });
    memories.addEventListener('change', () => { state.memories = memories.checked; });
    const keep = h('input', { type: 'checkbox', checked: state.keepZips ? true : null });
    keep.addEventListener('change', () => { state.keepZips = keep.checked; });
    return h('div', {}, ...parts,
      h('p', {}, 'My old folders were (optional): folder names in the imported text are rewritten to the new ones.'),
      ...pairRows, addPair,
      h('p', {}, h('label', {}, memories, ' Import claude.ai\'s memory of the project')),
      h('p', {}, h('label', {}, keep, ' Keep the downloaded export afterwards (it holds every chat the account has)')));
  };

  const reviewStep = () => {
    if (state.report) {
      const r = state.report;
      const open = h('button', { type: 'button', class: 'primary ci-open' }, 'Open the session');
      open.addEventListener('click', () => { close(true); opened(r.name); });
      return h('div', { class: 'ci-report' },
        h('p', {}, h('strong', {}, `Imported into ${r.name}.`)),
        h('ul', {},
          h('li', {}, `${r.written.length} file${r.written.length === 1 ? '' : 's'} written in ${r.home}`),
          h('li', {}, `${r.chats_imported.length} chat${r.chats_imported.length === 1 ? '' : 's'} added to Continue`),
          r.chats_left_alone.length ? h('li', {}, `${r.chats_left_alone.length} already there, left alone`) : null,
          r.zips_kept ? h('li', {}, `The export is kept in ${r.zips_kept}`) : h('li', {}, 'The downloaded export was deleted')),
        open);
    }
    const where = name ? name : `a new client, ${state.client.trim()}`;
    return h('div', {},
      h('p', {}, 'Ready to import ', h('strong', {}, state.project.name), ` into ${where}:`),
      h('ul', { class: 'ci-review' },
        h('li', {}, 'claude-project\\claude-ai-instructions.md - the project\'s instructions'),
        h('li', {}, `claude-project\\docs\\ - its ${state.project.docs} document${state.project.docs === 1 ? '' : 's'}`),
        state.memories ? h('li', {}, 'claude-project\\memories.md - claude.ai\'s memory of it') : null,
        h('li', {}, 'CLAUDE.md - a section pointing at them (added, never replacing what is there)'),
        h('li', {}, `${state.ticked.size} chat${state.ticked.size === 1 ? '' : 's'}, as conversations Continue lists`)),
      name ? null : h('p', { class: 'hint' }, 'The client, its project profile, the tooling repository and the share are created first.'));
  };

  // ---- moving between them ---------------------------------------------------------------------

  const enter = async (step) => {
    try {
      if (step === 3 && !state.projects.length) {
        state.projects = (await getJSON(`/api/claude-import/projects?job=${state.job}`)).projects || [];
      }
      if (step === 4 && !state.chats.length) {
        body.replaceChildren(h('p', { class: 'hint' }, 'Reading the chats...'));
        const query = `job=${state.job}&project=${encodeURIComponent(state.project.uuid)}${name ? `&name=${encodeURIComponent(name)}` : ''}`;
        state.chats = (await getJSON(`/api/claude-import/chats?${query}`)).chats || [];
        state.ticked = new Set(state.chats.filter((c) => c.ticked).map((c) => c.id));
      }
      go(step);
    } catch (error) { sayError(error); }
  };

  const runImport = async () => {
    next.disabled = true;
    next.textContent = 'Importing...';
    const payload = { job: state.job, project: state.project.uuid, chats: [...state.ticked], memories: state.memories,
      keep_zips: state.keepZips, path_pairs: state.pairs.filter((p) => p[0].trim() && p[1].trim()) };
    if (name) payload.name = name;
    else payload.new = { client: state.client.trim(), folder: state.place.trim(),
      share: state.share === 'create' ? 'create' : state.share === 'folder' ? state.sharePath.trim() : '' };
    try {
      state.report = await postJSON('/api/claude-import/run', payload);
      state.job = '';
      render();
    } catch (error) { sayError(error); next.disabled = false; next.textContent = 'Import'; }
  };

  const poll = async () => {
    if (state.step !== 2 || !state.job) return;
    try {
      const folder = state.folder.trim() ? `&folder=${encodeURIComponent(state.folder.trim())}` : '';
      const answer = await getJSON(`/api/claude-import/received?job=${state.job}${folder}`);
      let changed = false;
      for (const file of answer.files || []) {
        const mine = state.files.find((f) => f.filename === file.filename);
        if (mine && (mine.state !== file.state || mine.problem !== file.problem)) {
          Object.assign(mine, { state: file.state, problem: file.problem });
          changed = true;
        }
      }
      if (changed && !dialog.querySelector('.ci-upload:focus')) render();
    } catch (error) { /* the next look tries again */ }
  };

  function render() {
    steps.replaceChildren(...STEPS.map((label, index) => h('li', {
      class: index === state.step ? 'current' : index < state.step ? 'done' : '' }, label)));
    const views = [askStep, manifestStep, downloadStep, projectStep, chatsStep, whereStep, reviewStep];
    body.replaceChildren(views[state.step]());
    back.hidden = state.step === 0 || Boolean(state.report);
    next.hidden = Boolean(state.report);
    next.textContent = state.step === 0 ? 'I have the e-mail' : state.step === STEPS.length - 1 ? 'Import' : 'Next';
    next.disabled = !ready();
    cancel.textContent = state.report ? 'Close' : 'Cancel';
    stopPolling();
    if (state.step === 2) timer = setInterval(poll, POLL_MS);
  }

  back.addEventListener('click', () => go(Math.max(0, state.step - 1)));
  next.addEventListener('click', () => {
    if (state.step === STEPS.length - 1) runImport();
    else enter(state.step + 1);
  });
  cancel.addEventListener('click', () => close(Boolean(state.report)));
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(Boolean(state.report)); });
  document.body.append(dialog);
  dialog.showModal();
  render();
}
