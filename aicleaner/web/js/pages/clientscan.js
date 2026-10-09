// Scanning a folder for a client, from that client's own page.
//
// The commands this runs are not hard to run; they are easy to forget. The moment one is worth
// running is the moment a client is created, or the moment a second repo turns up six months in
// - and at that moment you are here, not in a terminal thinking about maintenance.
//
// The client is never chosen or typed: it is whose page this is.

import { getJSON, postJSON, say, sayError } from '../api.js';
import { h } from '../dom.js';
import { folderPicker, folderRoots, noFoldersYet } from '../picker.js';

const POLL_MS = 900;

// The machine the proxy runs on, which is the only machine a scan can read.
function host() {
  return (window.aicleanerHost || '').trim() || 'the machine the proxy runs on';
}

const ACTIONS = [
  ['scan', 'add', 'Scan and map',
   'Reads the folder and maps every high-confidence find to this client. Writes to the mapping.'],
  ['preflight', 'add', 'Preflight and map',
   'The same, over the folder AND the Claude Code history for it - so picking up existing work '
   + 'does not send a first request full of identifiers the mapping has never seen.'],
  ['scan', 'queue', 'Scan for review',
   'Reads the folder and puts the finds in Review, already marked as this client\'s, to decide '
   + 'by hand. Writes nothing to the mapping.'],
];

function mb(bytes) {
  if (!bytes) return '0 MB';
  return bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// What the scan is doing, in the one place somebody looks to find out whether it is doing anything.
// Counting is shown as its own step: on a large repository it takes long enough that a bar sitting
// at zero reads as a scan that never started, which is exactly how this was first reported.
function progressBlock(job) {
  if (job.phase === 'counting') {
    return h('p', { class: 'chip' }, `Measuring ${job.folder}: ${job.total_files} files so far...`);
  }
  // Writing is a phase of its own, and on a large repository the slower one. The bar used to reach
  // 100% at the end of reading and sit there while the mapping was still being built, which reads
  // as a finished scan that has hung.
  if (job.phase === 'writing') {
    const total = job.total_writes || 0;
    const done = Math.min(job.done_writes || 0, total);
    return h('div', { class: 'scan-progress' },
      h('progress', { max: String(total || 1), value: String(done) }),
      h('p', { class: 'hint' },
        `Writing the mapping: ${done} of ${total} entries`
        + (job.now ? ` for ${job.now}` : '') + ' - the reading is done.'));
  }
  const total = job.total_bytes || 0;
  const done = Math.min(job.done_bytes || 0, total);
  const pct = total ? Math.round((done / total) * 100) : 0;
  return h('div', { class: 'scan-progress' },
    h('progress', { max: String(total || 1), value: String(done) }),
    h('p', { class: 'hint' },
      `${pct}% - ${job.done_files} of ${job.total_files} files, ${mb(done)} of ${mb(total)}`
      + (job.now ? ` - reading ${job.now}` : '')));
}

function report(job) {
  if (!job) return h('p', { class: 'hint' }, 'Nothing scanned yet.');
  if (job.state === 'running') {
    return h('div', {},
      h('p', { class: 'chip' }, `${job.kind} of ${job.folder} is running...`),
      progressBlock(job));
  }
  // Only finished jobs reach here: the states that are still going returned above.
  const head = h('p', { class: `chip ${job.state === 'failed' ? 'bad' : 'ok'}` },
    `${job.kind} of ${job.folder}: ${job.state}`);
  return h('div', {},
    head,
    job.error ? h('p', { class: 'error-text' }, job.error) : null,
    job.lines && job.lines.length ? h('pre', { class: 'mono scan-report' }, job.lines.join('\n')) : null,
    job.truncated ? h('p', { class: 'hint' }, 'Report shortened; the full one is in the terminal.') : null);
}

export function scanBlock(cid) {
  const folder = h('input', {
    type: 'text', name: 'folder', class: 'wide',
    placeholder: 'C:\\Repos\\their-project   (quotes are fine)',
  });
  const out = h('div', { class: 'scan-out' }, report(null));
  const buttons = ACTIONS.map(([kind, mode, label, why]) =>
    h('button', { type: 'button', class: 'scan-go', dataset: { kind, mode }, title: why }, label));
  let polling = null;

  // Buttons are only ever off while a job this page can SEE is going. A disabled control with no
  // visible reason is indistinguishable from a dead page: that is how three buttons ended up doing
  // nothing, silently, after the proxy was restarted and took the job with it.
  const show = (job) => {
    out.replaceChildren(report(job));
    const busy = Boolean(job && job.state === 'running');
    for (const button of buttons) button.disabled = busy;
  };

  const release = (why) => {
    for (const button of buttons) button.disabled = false;
    out.replaceChildren(h('p', { class: 'hint' }, why));
  };

  const watch = () => {
    clearInterval(polling);
    polling = setInterval(async () => {
      try {
        const state = await getJSON('/api/scan');
        show(state.job);
        // The page can be navigated away from mid-scan; the interval must not outlive it.
        if (!state.running || !out.isConnected) clearInterval(polling);
      } catch (error) {
        // Whatever went wrong, this page can no longer tell whether a scan is running - so it says
        // so and hands the buttons back, rather than freezing in the last state it happened to see.
        clearInterval(polling);
        release('Lost track of the scan (the proxy may have restarted). Start it again if you need to.');
      }
    }, POLL_MS);
  };

  const section = h('section', { class: 'card', dataset: { card: 'scan' } },
    h('h2', {}, 'Scan a folder for this client'),
    h('p', { class: 'hint' },
      'Runs the same scan the command line runs, for '
      + `${cid}. The folder is on ${host()}, where the proxy runs, whether you are sitting at it `
      + 'or reading this through the tunnel. A scan reads every text file in it, so a large '
      + 'repository takes a while.'),
    // Named, not "this machine": through the tunnel you are looking at a page served BY that
    // machine from somewhere else, and "this" is exactly the word that cannot be trusted to mean
    // the same thing at both ends.
    h('div', { class: 'form-row' },
      h('label', { class: 'field wide' }, `Folder on ${host()}`, folder),
      folderPicker(folder)),
    h('div', { class: 'form-row' }, ...buttons),
    out);

  // When nothing may be reached yet, say so here rather than letting the buttons fail: the answer
  // belongs on the page that needs it, not in a refusal after the fact.
  folderRoots().then((info) => {
    if (info.roots && info.roots.length) return;
    section.insertBefore(noFoldersYet(info.editable), section.querySelector('.form-row'));
    for (const button of buttons) button.disabled = true;
  }).catch(() => {});

  section.addEventListener('click', async (event) => {
    const button = event.target.closest('button.scan-go');
    if (!button) return;
    const where = folder.value.trim();
    if (!where) {
      say('Name a folder to scan.', 'error');
      folder.focus();
      return;
    }
    try {
      const job = await postJSON('/api/scan', {
        kind: button.dataset.kind, mode: button.dataset.mode, folder: where, client: cid,
      });
      show(job);
      watch();
    } catch (error) {
      sayError(error);
    }
  });

  // Show a scan that is already running, so leaving the page and coming back is not a dead end.
  getJSON('/api/scan').then((state) => {
    if (!state.job) return;
    show(state.job);
    if (state.running) watch();
  }).catch(() => {
    // Could not ask: leave every button usable. Guessing "probably busy" is the failure that
    // produced a page where nothing worked and nothing said why.
    for (const button of buttons) button.disabled = false;
  });

  return section;
}
