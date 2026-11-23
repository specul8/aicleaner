// Git kept out of OneDrive (feature 186; Brad, 2026-11-18). A start whose project assets folder holds Git is refused
// with an offer {action: 'git_onedrive', ...}; this dialog asks what to do about it:
//   Yes & Migrate Repo - choose a folder outside OneDrive (AI Cleaner's own folder picker, so it works remotely), see
//     the plan (what moves, what stays, any clash), move; then start
//   No, but remind me next time - start this time
//   No, Don't remind me again (this project) - kept with the project session, then start
//   Cancel - nothing starts
// openGitOneDrive(offer) resolves 'once' (start, skipping the check this once), 'checked' (start - the check runs
// again, after a move) or 'cancel'.
//
// Also the editor's non-blocking warning (gitWarning): the same finding, shown in the Project assets folder block.

import { getJSON, postJSON, say, sayError } from './api.js';
import { h } from './dom.js';
import { folderPicker } from './picker.js';
import { arrangeSheet } from './sheet.js';

const IN_ONEDRIVE = /onedrive/i;
const POLL_MS = 700;

function sizeText(bytes) {
  let value = Number(bytes) || 0;
  for (const unit of ['bytes', 'KB', 'MB', 'GB']) {
    if (value < 1024 || unit === 'GB') return unit === 'bytes' ? `${value} bytes` : `${value.toFixed(1)} ${unit}`;
    value /= 1024;
  }
  return `${bytes} bytes`;
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

function repoList(repos) {
  return h('ul', { class: 'git-od-repos mono' }, ...repos.map((repo) => h('li', {},
    repo.rel || '(the project assets folder itself)',
    repo.kind === 'file' ? h('span', { class: 'muted' }, ' - a worktree or submodule (.git file)') : null)));
}

// D259: empty .git folders OneDrive kept after repositories were moved out (no HEAD - Git cannot open them). Said,
// never a reason to hold Start.
function skeletonNote(skeletons, cls) {
  if (!(skeletons || []).length) return null;
  return h('div', { class: `hint git-od-skeletons ${cls || ''}`.trim() },
    h('p', {}, `Empty Git folders left behind - safe to delete (${plural(skeletons.length, 'folder', 'folders')}; `
      + 'they hold no Git data, so they do not hold Start). The .git folder inside each of:'),
    h('ul', { class: 'mono' }, ...skeletons.map((dot) => h('li', {}, dot.path))));
}

// What was found, in words: the repos in each of the session's OneDrive folders (f187: every folder is checked, not only
// the client assets folder), or that the check was cut off.
function findings(offer) {
  const repos = offer.repos || [];
  const parts = [];
  const found = (offer.found || []).filter((f) => (f.repos || []).length);
  if (found.length > 1) {
    for (const folder of found) parts.push(h('p', { class: 'git-od-folder' }, `Found in ${folder.folder}:`), repoList(folder.repos));
    parts.push(h('p', { class: 'hint git-od-one-at-a-time' }, `Yes & Migrate Repo moves the repositories in ${offer.share} `
      + 'first; Start then checks again and asks about the next folder.'));
  } else if (repos.length) {
    parts.push(h('p', {}, `Found in ${offer.share}:`), repoList(repos));
  }
  const skeletons = skeletonNote(offer.skeletons);
  if (skeletons) parts.push(skeletons);
  if (!offer.complete) {
    parts.push(h('p', { class: 'hint warn git-od-cutoff' },
      `AI Cleaner could not finish checking ${offer.share} in ${Math.round(offer.budget || 2)} seconds `
      + `(${plural(offer.folders || 0, 'folder', 'folders')} checked), so there may be Git files it did not see.`));
  }
  return parts;
}

async function remember(name, choice) {
  await postJSON('/api/session/git-onedrive/choice', { name, choice });
}

export function openGitOneDrive(offer) {
  return new Promise((resolve) => {
    const name = offer.name;
    const body = h('div', { class: 'git-od-body' });
    const actions = h('span', { class: 'git-od-actions' });
    const cancel = h('button', { type: 'button', class: 'git-od-cancel' }, 'Cancel');
    const dialog = h('dialog', { id: 'git-onedrive', class: 'sheet git-od-dialog', 'aria-labelledby': 'git-od-title',
      'aria-describedby': 'git-od-warning' },
    h('h2', { id: 'git-od-title' }, 'Git files in OneDrive'),
    body,
    h('div', { class: 'fb-footer' }, cancel, actions));
    let busy = false;
    let answered = false;
    const finish = (answer) => {
      if (answered) return;
      answered = true;
      dialog.close();
      dialog.remove();
      resolve(answer);
    };
    cancel.addEventListener('click', () => { if (!busy) finish('cancel'); });
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); if (!busy) finish('cancel'); });

    const button = (label, cls, run) => {
      const b = h('button', { type: 'button', class: cls }, label);
      b.addEventListener('click', async () => {
        if (busy) return;
        try { await run(b); } catch (error) { sayError(error); }
      });
      return b;
    };
    const show = (parts, buttons, focus) => {
      body.replaceChildren(...parts.filter(Boolean));
      actions.replaceChildren(...buttons.filter(Boolean));
      const first = focus || buttons.find(Boolean);
      if (first) first.focus();
    };

    // ---- the question -------------------------------------------------------------------------------------------
    const ask = () => {
      const repos = offer.repos || [];
      show([
        h('p', { id: 'git-od-warning', class: 'git-od-warning' },
          h('b', {}, 'WARNING'), ' - You have Git files in OneDrive, which can cause problems when OneDrive is syncing '
          + '(Git creates a large number of small files, and OneDrive can lock them or sync half a change). Would you '
          + 'like to create a folder outside OneDrive to store your repo files in?'),
        ...findings(offer),
      ], [
        repos.length ? button('Yes & Migrate Repo', 'primary git-od-migrate', () => (offer.migrate_possible ? choose() : nowhere())) : null,
        button('No, but remind me next time', 'git-od-remind', () => finish('once')),
        button('No, Don\'t remind me again (this project)', 'git-od-ignore', async () => {
          await remember(name, 'ignore');
          say(`${name}: AI Cleaner will not check its OneDrive folders for Git again. Turn it back on in Edit `
            + 'project sessions.', 'ok');
          finish('once');
        }),
      ]);
    };

    // ---- nowhere outside OneDrive to move to ------------------------------------------------------------------------
    const nowhere = () => {
      const tick = h('input', { type: 'checkbox', class: 'git-od-noremind' });
      const keep = async () => { if (tick.checked) await remember(name, 'ignore'); };
      const stop = button('Cancel', 'git-od-nowhere-cancel', async () => { await keep(); finish('cancel'); });
      show([
        h('p', { class: 'hint warn git-od-nowhere' }, 'Every folder AI Cleaner may use is in OneDrive, so there is '
          + 'nowhere outside OneDrive to move the repositories to. Allow a folder outside OneDrive under Settings > '
          + 'Folders the pages can open (on this machine), then start again to move them.'),
        h('label', { class: 'check' }, tick, ' Don\'t remind me again for this project'),
      ], [
        button('Ignore this time', 'primary git-od-ignore-once', async () => { await keep(); finish('once'); }),
        stop,
      ]);
      cancel.hidden = true;
    };

    // ---- choosing the folder, and the plan -------------------------------------------------------------------------
    const target = h('input', { type: 'text', class: 'git-od-target mono', readonly: true, 'aria-label': 'Folder to move the repositories to',
      placeholder: 'No folder chosen yet' });
    const planSlot = h('div', { class: 'git-od-plan', role: 'status', 'aria-live': 'polite' });
    let plan = null;
    const pick = folderPicker(target, {
      label: 'Choose a folder', title: 'Choose a folder outside OneDrive for the repositories',
      chooseLabel: 'Use this folder',
      start: () => (offer.roots || [])[0] || '',
      refuse: (path) => (IN_ONEDRIVE.test(path) ? 'That folder is in OneDrive - choose one outside OneDrive.' : ''),
    });
    pick.classList.add('git-od-pick');
    const choose = () => {
      cancel.hidden = false;
      show([
        h('p', {}, 'Choose a folder outside OneDrive for the repositories. Each moves into it with its .git folder, '
          + 'every file Git tracks and every file .gitignore covers; untracked files stay in OneDrive. Nothing is '
          + 'moved until you have seen the plan.'),
        h('div', { class: 'git-od-choose' }, target, pick),
        planSlot,
      ], [], pick);
      if (target.value) check(target.value);
    };
    target.addEventListener('input', () => check(target.value));

    const check = async (folder) => {
      plan = null;
      actions.replaceChildren();
      planSlot.replaceChildren(h('p', { class: 'hint' }, 'Working out the move...'));
      try {
        plan = await postJSON('/api/session/git-onedrive/plan', { name, target: folder, folder: offer.share });
      } catch (error) {
        planSlot.replaceChildren(h('p', { class: 'hint bad git-od-refused', role: 'alert' }, error.message || String(error)));
        return;
      }
      drawPlan(plan);
    };

    const drawPlan = (p) => {
      const rows = p.repos.map((r) => h('li', {},
        h('span', { class: 'mono' }, r.rel || '(top)'), ' -> ', h('span', { class: 'mono' }, r.dest),
        ` - ${plural(r.files, 'file', 'files')} (${sizeText(r.bytes)})`,
        r.untracked_left ? `; ${plural(r.untracked_left, 'untracked file stays', 'untracked files stay')} in OneDrive` : '',
        r.dirty ? h('span', { class: 'warn' }, `; ${plural(r.dirty, 'uncommitted change', 'uncommitted changes')} (they move with it)`) : '',
        r.problem ? h('div', { class: 'bad' }, r.problem) : null));
      const parts = [h('ul', { class: 'git-od-moves' }, ...rows),
        h('p', {}, `In all: ${plural(p.files, 'file', 'files')}, ${sizeText(p.bytes)}. Copied first, checked, and only `
          + 'then removed from OneDrive; a note, "where did my files go.md", is left in each repository\'s old folder.')];
      for (const problem of p.problems || []) parts.push(h('p', { class: 'bad' }, problem));
      if (p.clash_count) {
        parts.push(h('div', { class: 'git-od-clashes', role: 'alert' },
          h('p', { class: 'warn' }, `${plural(p.clash_count, 'thing is', 'things are')} already in ${p.target} where the `
            + 'move would write - nothing is ever overwritten or merged:'),
          h('ul', { class: 'mono' }, ...p.clashes.map((c) => h('li', {}, `${c.path} - ${c.why}`))),
          p.clash_count > p.clashes.length ? h('p', { class: 'hint' }, `...and ${p.clash_count - p.clashes.length} more.`) : null));
        planSlot.replaceChildren(...parts);
        actions.replaceChildren(
          button('Choose another folder', 'git-od-another', () => pick.click()),
          p.suggest ? button(`Move into a new folder (${p.suggest.split(/[\\/]/).pop()})`, 'primary git-od-subfolder', async () => {
            target.value = p.suggest;
            await check(p.suggest);
          }) : null);
        const first = actions.querySelector('button');
        if (first) first.focus();
        return;
      }
      planSlot.replaceChildren(...parts);
      if (p.ok) {
        const go = button('Move the repositories', 'primary git-od-go', () => move(p.target));
        actions.replaceChildren(go);
        go.focus();
      }
    };

    // ---- the move ---------------------------------------------------------------------------------------------------
    const move = async (folder) => {
      busy = true;
      cancel.disabled = true;
      actions.replaceChildren();
      const step = h('p', { class: 'git-od-step', role: 'status', 'aria-live': 'polite' }, 'Starting the move...');
      body.replaceChildren(h('p', {}, `Moving the repositories to ${folder}. Leave this open until it finishes.`), step);
      let job;
      try {
        job = (await postJSON('/api/session/git-onedrive/migrate', { name, target: folder, folder: offer.share })).job;
      } catch (error) {
        busy = false;
        cancel.disabled = false;
        failed(error.message || String(error));
        return;
      }
      for (;;) {
        await new Promise((done) => { setTimeout(done, POLL_MS); });
        let state;
        try { state = await getJSON(`/api/session/git-onedrive/job?id=${encodeURIComponent(job)}`); } catch (error) { continue; }
        step.textContent = `${state.step}...`;
        if (state.state === 'running') continue;
        busy = false;
        cancel.disabled = false;
        if (state.state === 'done') done(state.result); else failed(state.error);
        return;
      }
    };
    const done = (result) => {
      const notes = [...(result.profile_notes || [])];
      show([
        h('p', { class: 'ok git-od-done' }, `Moved ${plural(result.repos.length, 'repository', 'repositories')} `
          + `(${plural(result.files, 'file', 'files')}) to ${result.target}.`),
        h('ul', { class: 'mono' }, ...result.repos.map((r) => h('li', {}, `${r.repo} -> ${r.dest}`))),
        ...notes.map((n) => h('p', { class: 'hint' }, n)),
        h('p', { class: 'hint' }, `A note saying what happened and how to undo it is in each old folder. The log: ${result.log || '(none)'}`),
      ], [button('Start the session', 'primary git-od-start', () => finish('checked'))]);
      cancel.textContent = 'Close';
    };
    const failed = (why) => {
      show([
        h('p', { class: 'bad git-od-failed', role: 'alert' }, `The repositories were not moved: ${why}`),
        h('p', { class: 'hint' }, 'Everything is where it was. Choose another folder, or cancel.'),
      ], [button('Choose another folder', 'git-od-another', () => { choose(); pick.click(); })]);
    };

    document.body.append(dialog);
    arrangeSheet(dialog, { dirty: () => busy, cancel });
    dialog.showModal();
    ask();
  });
}

// The editor's warning (non-blocking): Git in the project assets folder, or the check cut off; and, when this project
// session said "don't remind me again", that the check is off - with a way to turn it back on.
export async function gitWarning(slot, name) {
  if (!slot || !name) return;
  let found;
  try {
    found = await getJSON(`/api/session/git-onedrive?name=${encodeURIComponent(name)}`);
  } catch (error) { return; }
  const parts = [];
  const holding = (found.found || []).filter((f) => (f.repos || []).length);
  if (found.onedrive && holding.length) {
    parts.push(h('p', { class: 'hint warn git-od-editor-warn' },
      `Git files are in OneDrive: ${holding.map((f) => `${f.folder} (${f.repos.map((r) => r.rel || 'its top').join(', ')})`).join('; ')}. `
      + 'Git in OneDrive causes sync problems; Start will ask whether to move the repositories out of OneDrive.'));
  } else if (found.onedrive && !found.complete) {
    parts.push(h('p', { class: 'hint warn git-od-editor-warn' },
      'AI Cleaner could not finish checking these OneDrive folders for Git files; Start will check again.'));
  }
  if (found.onedrive) parts.push(skeletonNote(found.skeletons, 'git-od-editor-skeletons'));
  if (found.choice === 'ignore') {
    const on = h('button', { type: 'button', class: 'link git-od-turn-on' }, 'Turn it back on');
    on.addEventListener('click', async () => {
      try {
        await remember(name, 'remind');
        say(`${name}: Start will check its OneDrive folders for Git again.`, 'ok');
        gitWarning(slot, name);
      } catch (error) { sayError(error); }
    });
    parts.push(h('p', { class: 'hint git-od-off' }, 'Git check: off for this project session. ', on));
  }
  slot.replaceChildren(...parts.filter(Boolean));
}
