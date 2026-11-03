// A second chat session on a project, in a git worktree of its own (DECISIONS 66, feature 86).
//
// On a project's session card: "Create a worktree" - pick one of the profile's git repos (or turn a
// plain folder into one first), keep or change the default name - and the page switches to the new
// session, `<profile> @ <name>`. On a stopped worktree session's card: Merge (its branch into the
// repo) and Remove (the worktree, and the branch when merged). Claude can do either too.

import { getJSON, postJSON, say, sayError } from './api.js';
import { busyButton, h } from './dom.js';

// What the slot shows now, so the five-second poll leaves an open form alone.
const shown = { key: '', form: false, slot: null };

function createForm(slot, profile, offer, selectSession) {
  const choices = [
    ...offer.repos.map((repo) => h('option', { value: repo, dataset: { init: '' } }, repo)),
    ...offer.folders.map((folder) => h('option', { value: folder, dataset: { init: '1' } },
      `${folder} (make it a git repo first)`)),
  ];
  const where = h('select', { class: 'worktree-repo', 'aria-label': 'Repo to branch from' }, ...choices);
  const name = h('input', { type: 'text', class: 'worktree-name', value: offer.default,
    'aria-label': 'Worktree name', maxlength: '40', 'data-unsaved-ignore': '' });
  const create = h('button', { type: 'button', class: 'primary worktree-go' }, 'Create');
  const cancel = h('button', { type: 'button', class: 'worktree-cancel' }, 'Cancel');
  const close = () => { shown.form = false; shown.key = ''; slot.replaceChildren(); };
  cancel.addEventListener('click', close);
  busyButton(create, async () => {
    const chosen = where.selectedOptions[0];
    try {
      const made = await postJSON('/api/session/worktree', { name: profile.name, repo: where.value,
        branch: name.value.trim(), init: Boolean(chosen && chosen.dataset.init) });
      close();
      say(`Created ${made.path}. This session works there, on its own branch.`, 'ok');
      await selectSession(made.session);
    } catch (error) { sayError(error); }
  }, { busyLabel: 'Creating...', timeout: 120000 });
  return h('div', { class: 'worktree-form form-row' },
    h('label', { class: 'field' }, 'Branch from', where),
    h('label', { class: 'field' }, 'Name', name), create, cancel,
    h('p', { class: 'hint' }, 'A second checkout beside the repo, on a branch of this name, so two '
      + 'sessions can work on one project without committing over each other.'));
}

async function openCreate(slot, profile, selectSession) {
  let offer;
  try { offer = await getJSON(`/api/session/worktree?name=${encodeURIComponent(profile.name)}`); }
  catch (error) { sayError(error); return; }
  if (!offer.repos.length && !offer.folders.length) {
    say(`${profile.name} has no git repo, and no folder outside OneDrive that could be made one.`, 'warn');
    return;
  }
  shown.form = true;
  slot.replaceChildren(createForm(slot, profile, offer, selectSession));
}

function finishButtons(profile, selectSession, refresh) {
  const merge = h('button', { type: 'button', class: 'worktree-merge' }, 'Merge');
  const remove = h('button', { type: 'button', class: 'worktree-remove danger' }, 'Remove');
  busyButton(merge, async () => {
    try {
      const done = await postJSON('/api/session/worktree/merge', { name: profile.name });
      say(`${done.message}.`, 'ok');
      await refresh();
    } catch (error) { sayError(error); }
  }, { busyLabel: 'Merging...', timeout: 120000 });
  busyButton(remove, async () => {
    try {
      const done = await postJSON('/api/session/worktree/remove', { name: profile.name });
      say(`${done.message}.`, 'ok');
      shown.key = '';
      await selectSession(done.session);
    } catch (error) { sayError(error); }
  }, { busyLabel: 'Removing...', timeout: 120000 });
  const tree = profile.worktree || {};
  return h('div', { class: 'worktree-finish form-row' },
    h('span', { class: 'muted' }, `Branch ${tree.name} of ${tree.repo}.`), merge, remove);
}

// Called on every paint; rebuilds only when the session or its running state changes.
export function paintWorktree(slot, profile, running, selectSession, refresh) {
  if (shown.slot !== slot) Object.assign(shown, { key: '', form: false, slot });
  const key = `${profile.name || ''}|${running}|${profile.worktree_of || ''}`;
  if (shown.key === key || (shown.form && shown.key.startsWith(`${profile.name}|`))) return;
  shown.key = key;
  shown.form = false;
  if (!profile.name) { slot.replaceChildren(); return; }
  if (profile.worktree_of) {
    // Merging or removing under a running Claude Code would change files it is working on.
    slot.replaceChildren(running ? h('p', { class: 'hint' }, 'Stop this session to merge or remove its worktree.')
      : finishButtons(profile, selectSession, refresh));
    return;
  }
  const open = h('button', { type: 'button', class: 'link worktree-create' }, 'Create a worktree');
  open.addEventListener('click', () => openCreate(slot, profile, selectSession));
  // What a worktree is for, for someone new to Git (the user, 2026-10-08: "people not used to Git
  // may not understand the benefit").
  const why = h('span', { class: 'hint worktree-why' },
    ' - a second working copy of the same repository, with its own session. Two pieces of work can go '
    + 'on side by side without touching each other\'s files - say, a large feature alongside everyday '
    + 'fixes - and when one is finished, Merge brings its changes back into the main copy.');
  slot.replaceChildren(open, why);
}
