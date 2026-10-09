// Clients and Global. All clients as collapsed rows (Global first); rows open independently. A
// client chosen in the sidebar (#/clients/<id>) is shown alone, already open. Inside a client:
// filter, entries with inline edit (auto-save on leaving the row), add, delete with an in-page
// confirmation, the client's ignore list, export and archive, and the error views: a rejected
// row highlighted where it is, or the raw file around a syntax error with Restore.

import { getJSON, postJSON, say, sayError } from '../api.js';
import { TOKEN_SHAPES, bulkGroup, focusFirst, h, icon, iconButton, kindLabel, pickBox, pickCell, setChildren, shortTime } from '../dom.js';
import { displayName } from '../app.js';
import { scanBlock } from './clientscan.js';
import { heldCallout, keySection } from './clientkeys.js';
import { track } from '../unsaved.js';
import { folderPicker } from '../picker.js';

// Quotes and backslashes are fine (escaped where JSON needs it); control characters never are.
const FORBIDDEN = /[\u0000-\u001f]/;
const NOT_YET = 'Control characters are not allowed.';
const LOCKED = 'Also in Global; remove it from Global to change it here.';
const GLOBAL_DESC = 'yours alone: your employer and vendors you use; applies to every client; tokens Org00; stored with your install, never shared';

const expanded = new Set();
const view = { main: null, ctx: null };

// ---- status helpers -------------------------------------------------------------------------

function allRows(status) {
  const global = status.global || { id: 'global', label: 'Global', state: ['ok'], entries: 0, conflicts: [] };
  return [global, ...status.clients];
}

function rowById(id) {
  return allRows(view.ctx.status).find((r) => r.id === id) || null;
}

function stateChips(row) {
  const chips = [];
  if (row.state.includes('newer')) {
    // F14: a colleague's newer aicleaner wrote it. Last-good is in use where there is one.
    chips.push(h('span', { class: 'chip warn', dataset: { state: 'newer' } },
      row.state.includes('broken') ? 'written by a newer aicleaner - update to use it'
        : `written by a newer aicleaner - update; using version from ${row.fallback_at ? shortTime(row.fallback_at) : 'earlier'}`));
    return chips;
  }
  if (row.state.includes('broken')) chips.push(h('span', { class: 'chip bad', dataset: { state: 'broken' } }, 'file invalid'));
  // Its file has gone; last-good keeps it protected (DECISIONS 67).
  if (row.state.includes('missing')) {
    chips.push(h('span', { class: 'chip bad', dataset: { state: 'missing' } },
      `file missing - using version from ${row.fallback_at ? shortTime(row.fallback_at) : 'earlier'}`));
  } else if (row.state.includes('fallback')) {
    chips.push(h('span', { class: 'chip warn', dataset: { state: 'fallback' } },
      `edit rejected, using version from ${row.fallback_at ? shortTime(row.fallback_at) : 'earlier'}`));
  }
  if (row.state.includes('conflict-copy')) chips.push(h('span', { class: 'chip bad', dataset: { state: 'conflict-copy' } }, 'conflict copy'));
  if (row.state.includes('held')) chips.push(h('span', { class: 'chip warn', dataset: { state: 'held' } }, 'change waiting for review'));
  if (row.state.includes('hand-edited')) chips.push(h('span', { class: 'chip', dataset: { state: 'hand-edited' } }, 'edited outside aicleaner'));
  return chips;
}

function levelClass(row) {
  if (row.state.includes('broken') || row.state.includes('conflict-copy') || row.state.includes('missing')) return 'has-bad';
  if (row.state.includes('fallback') || row.state.includes('held')) return 'has-warn';
  return '';
}

// ---- sections -------------------------------------------------------------------------------

function paintHeader(section, row) {
  const toggle = section.querySelector('.client-toggle');
  toggle.replaceChildren(...[icon('chevron'), h('span', { class: 'name' }, displayName(row)),
    row.id === 'global' ? h('span', { class: 'desc' }, GLOBAL_DESC) : null,
    ...stateChips(row),
    h('span', { class: 'count' }, `${row.entries} ${row.entries === 1 ? 'entry' : 'entries'}`)].filter(Boolean));
  toggle.querySelector('svg').classList.add('chev');
  section.className = `client ${levelClass(row)}`.trim();
}

function section(row, open) {
  const toggle = h('button', { type: 'button', class: 'client-toggle', 'aria-expanded': String(open), 'aria-controls': `body-${row.id}` });
  const body = h('div', { class: 'client-body', id: `body-${row.id}`, hidden: !open });
  const sec = h('section', { dataset: { clientId: row.id } }, toggle, body);
  paintHeader(sec, row);
  // The chevron shows which way it is; the title says what a click does (the user, 2026-10-01).
  const sayAction = (isOpen) => { toggle.title = `${isOpen ? 'Hide' : 'Show'} ${displayName(row)}`; };
  sayAction(open);
  toggle.addEventListener('click', () => {
    const nowOpen = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(nowOpen));
    sayAction(nowOpen);
    body.hidden = !nowOpen;
    if (nowOpen) {
      expanded.add(row.id);
      loadBody(row.id, body);
    } else {
      expanded.delete(row.id);
    }
  });
  if (open) loadBody(row.id, body);
  return sec;
}

// ---- inside a client ---------------------------------------------------------------------------

function problemCallout(row, clientView) {
  const err = row.error;
  const file = clientView.file;
  const since = row.fallback_at ? shortTime(row.fallback_at) : 'earlier';
  const parts = [];
  if (row.state.includes('broken')) {
    parts.push(h('div', { class: 'callout bad' }, h('b', {}, `${file} is invalid and there is no good copy on this machine. `),
      'AI communication is stopped until it is fixed.',
      err && err.kind === 'rule' && err.index !== undefined ? ` Row ${err.index + 1}: ${err.message}. Edit or delete the highlighted row.` : ''));
  } else if (row.state.includes('missing') && err) {
    parts.push(h('div', { class: 'callout bad' }, h('b', {}, 'This client\'s file is missing. '),
      `${err.message} Its names are still replaced, from the version of ${since}.`));
  } else if (row.state.includes('fallback') && err) {
    parts.push(h('div', { class: 'callout' }, h('b', {}, `An edit to ${file} was rejected. `),
      err.kind === 'rule' && err.index !== undefined
        ? `Row ${err.index + 1}: ${err.message}. Using the version from ${since} until it is fixed: edit the highlighted row, or add a valid entry and delete it.`
        : `${err.message}. Using the version from ${since} until it is fixed.`));
  }
  if (row.state.includes('conflict-copy')) {
    parts.push(h('div', { class: 'callout bad' }, h('b', {}, 'A conflicting copy of this file was found: '),
      `${(row.conflicts || []).join(', ')}. With the client key on this machine, a copy signed with it is merged in on its own; `
      + 'this one is not signed with it (or there is no key here), so it is ignored. Compare it with the file, then delete it.'));
  }
  if (row.state.includes('hand-edited')) {
    parts.push(h('p', { class: 'hint' },
      `This file was edited outside aicleaner${row.stamp && row.stamp.by ? ` (last saved by aicleaner on ${row.stamp.by})` : ''}. It is valid and in use.`));
  }
  return parts;
}

function rawView(raw, row, cid) {
  const lines = raw.text.split(/\r?\n/);
  const fileError = raw.file_error;
  const errLine = fileError && fileError.line;
  const pre = h('pre', { class: 'raw' }, lines.map((line, i) => [
    i + 1 === errLine ? h('mark', {}, line || ' ') : line, i < lines.length - 1 ? '\n' : '']));
  const canRestore = row && row.state.includes('fallback');
  const action = canRestore
    ? h('button', { type: 'button', class: 'primary restore' }, icon('rewind'), 'Restore last good version')
    : null;
  if (canRestore) {
    action.addEventListener('click', async () => {
      try {
        await postJSON(`/api/clients/${encodeURIComponent(cid)}/restore`, {});
        say(`Restored the last good version of ${raw.file}.`, 'ok');
        await reloadClient(cid);
      } catch (error) {
        sayError(error, refusalHandlers(cid));
      }
    });
  }
  // Restore is the right answer only when there is a good copy AND nothing has been added since
  // it was taken. Salvage is the other half: it reads the entries either side of the damage out
  // of the broken file itself, so it keeps this afternoon's work that the last-good copy predates
  // - and it is the only answer at all on a machine with no good copy (feature 13).
  const salvage = h('button', { type: 'button', class: canRestore ? 'salvage' : 'primary salvage' },
    icon('patch'), 'Keep the valid entries');
  salvage.addEventListener('click', async () => {
    try {
      const answer = await postJSON(`/api/clients/${encodeURIComponent(cid)}/salvage`, {});
      const lost = answer.dropped.length
        ? ` ${answer.dropped.length} could be read but not used: ${answer.dropped.join(', ')}.`
        : '';
      say(`Rebuilt ${raw.file} from ${answer.forms} entries.${lost} The broken file is kept at ${answer.kept}.`,
          answer.dropped.length ? 'warn' : 'ok');
      await reloadClient(cid);
    } catch (error) {
      sayError(error, refusalHandlers(cid));
    }
  });
  const hint = h('p', { class: 'hint' }, canRestore
    ? 'Restore goes back to the last good copy, losing anything added since. Keep the valid entries rebuilds from this file, losing only what cannot be read.'
    : 'There is no good copy of this file on this machine. Keep the valid entries rebuilds from what is still readable; anything else needs a text editor.');
  const where = errLine ? ` (line ${errLine}, column ${fileError.column})` : '';
  return h('div', { class: 'callout bad' }, h('b', {}, `${raw.file} is not valid JSON${where}. `),
    fileError ? fileError.message : '', pre,
    h('div', { class: 'form-row' }, action, salvage), hint);
}

// A stored `match` value as the three things the row's controls need. The store keeps the rule in
// the shape the file holds it ("cased", or an object), so the page reads it rather than being
// given a second representation to drift from.
function matchMode(rule) {
  if (!rule) return { mode: '', side: 'followed_by', words: '' };
  if (rule === 'cased' || (rule.cased && !rule.followed_by && !rule.preceded_by)) {
    return { mode: 'cased', side: 'followed_by', words: '' };
  }
  const side = (rule.preceded_by && rule.preceded_by.length) ? 'preceded_by' : 'followed_by';
  return { mode: 'near', side, words: (rule[side] || []).join(', ') };
}

// How an entry got here (feature 30). Two levels, deliberately: the label is what the CLIENT FILE
// says and therefore what a colleague receiving it sees; the detail is local evidence and never
// travels. Both are on screen, because triaging an entry needs the second and reading the file
// needs the first.
const SOURCES = {
  'repo-scan': 'Repo scan',
  manual: 'Manual entry',
  'auto-add': 'Auto-add',
  derived: 'Derived',
  review: 'Review',
};

function sourceLabel(source) {
  return SOURCES[source] || source || 'Unrecorded';
}

// The evidence, as a person reads it. A tooltip alone would be unreachable on a touch screen -
// which is the device the tunnel exists for - so this text is also what the chip shows on a tap.
function seenDetail(form) {
  const seen = form.seen || {};
  const lines = [`Source: ${sourceLabel(form.source || seen.source)}`];
  if (seen.added) lines.push(`Added: ${seen.added}${seen.by ? ` on ${seen.by}` : ''}`);
  if (seen.confidence) lines.push(`Confidence: ${seen.confidence}`);
  if (seen.count) lines.push(`Seen ${seen.count} time${seen.count === 1 ? '' : 's'}`);
  if (seen.first_seen || seen.last_seen) lines.push(`Between ${seen.first_seen || '?'} and ${seen.last_seen || '?'}`);
  if (seen.near) lines.push(`One letter from: ${seen.near}`);
  if ((seen.paths || []).length) lines.push(`Found in: ${seen.paths.join(', ')}`);
  if ((seen.projects || []).length) lines.push(`Projects: ${seen.projects.join(', ')}`);
  if ((seen.sessions || []).length) lines.push(`Sessions: ${seen.sessions.join(', ')}`);
  const with_ = Object.entries(seen.seen_with || {});
  if (with_.length) lines.push(`Alongside: ${with_.map(([c, n]) => `${c} (${n})`).join(', ')}`);
  if ((seen.notes || []).length) lines.push(...seen.notes);
  if (lines.length === 1 && !seen.added) lines.push('No detail was recorded for this entry.');
  return lines.join('\n');
}

function sourceChip(form) {
  const detail = seenDetail(form);
  const chip = h('button', { type: 'button', class: 'chip source', title: detail,
                             'aria-label': `Where ${form.real} came from` },
    sourceLabel(form.source || (form.seen || {}).source));
  // On a tap there is no hover, so the same text goes to a toast - which lingers until it is
  // dismissed, so it can be read properly rather than chased.
  chip.addEventListener('click', (event) => {
    event.stopPropagation();
    say(`${form.real}`, 'warn', { why: detail });
  });
  return chip;
}

function matchChip(rule) {
  const m = matchMode(rule);
  if (m.mode === 'cased') return 'cased only';
  if (m.mode === 'near') return m.side === 'preceded_by' ? 'after a word' : 'before a word';
  return 'rule';
}

function matchWhy(rule) {
  const m = matchMode(rule);
  if (m.mode === 'cased') return 'Replaced only where it is spelled exactly like this. Still detected everywhere.';
  if (m.mode === 'near') {
    const side = m.side === 'preceded_by' ? 'after' : 'before';
    return `Replaced only where it comes ${side} one of: ${m.words}. Still detected everywhere.`;
  }
  return 'This entry has a matching rule.';
}

// The row's controls back into what the API takes: undefined for "leave it alone" is the caller's
// job, so this only ever returns a rule or null (remove it).
function matchValue(mode, side, words) {
  if (mode === 'cased') return 'cased';
  if (mode !== 'near') return null;
  const list = words.split(',').map((w) => w.trim()).filter(Boolean);
  return list.length ? { [side]: list } : null;
}

function validity(inputs, message, button) {
  const bad = inputs.some((input) => FORBIDDEN.test(input.value));
  for (const input of inputs) input.classList.toggle('invalid', FORBIDDEN.test(input.value));
  message.textContent = bad ? NOT_YET : '';
  button.disabled = bad || !inputs[0].value.trim();
  return !button.disabled;
}

// Where an ignore can be moved to: every other client, and Global - which is not a move target
// for a token (Copy to Global is the way there) but is an ordinary list for an ignore.
function moveIgnoreTargets(cid) {
  const scope = cid === 'global' ? 'global' : cid;
  const list = view.ctx.status.clients.filter((r) => r.id !== scope).map((r) => ({ id: r.id, label: displayName(r) }));
  if (scope !== 'global') list.unshift({ id: 'global', label: 'Global (everywhere)' });
  return list;
}

function moveTargets(cid) {
  return view.ctx.status.clients.filter((r) => r.id !== cid).map((r) => ({ id: r.id, label: displayName(r) }));
}

function entryRow(cid, form, reason, readOnly) {
  if (form.locked) readOnly = true;
  const realTd = h('td', { class: 'real' }, form.real);
  const tokenTd = reason ? h('td', { class: 'reason' }, reason) : h('td', { class: 'mono token' }, form.token);
  // A form with a matching rule does not replace everywhere its text appears, which is a
  // surprising thing for a row to be doing silently. The chip says so at a glance, beside the
  // shape, rather than only inside the edit form (feature 23).
  const kindTd = h('td', {}, reason ? '' : kindLabel(form.kind),
    reason || !form.match ? null : h('span', { class: 'chip match', title: matchWhy(form.match) },
                                     matchChip(form.match)),
    reason ? null : sourceChip(form));
  const actions = h('td', { class: 'actions' });
  // No tick on a row nothing can be done to - locked entries set readOnly above, and a read-only
  // view has no actions at all. Offering a tick and then refusing the action is worse than not
  // offering one.
  const pick = readOnly ? h('td', { class: 'pick' }) : pickCell(`Select ${form.real}`);
  const tr = h('tr', { class: `entry${reason ? ' invalid' : ''}${form.locked ? ' locked' : ''}`,
    dataset: { real: form.real, kind: form.kind || '' } },
    pick, realTd, tokenTd, kindTd, actions);

  const normal = () => {
    if (form.locked) {
      return actions.replaceChildren(h('span', { class: 'lock', title: LOCKED }, icon('lock', LOCKED)));
    }
    if (readOnly) return actions.replaceChildren();
    const clientOnly = cid !== 'global' && !reason;
    actions.replaceChildren(h('div', { class: 'actions-wrap' },
      iconButton('edit', `Edit ${form.real}`, { class: 'icon edit' }),
      clientOnly ? iconButton('global', `Copy ${form.real} to Global`, { class: 'icon copy-global' }) : null,
      clientOnly && moveTargets(cid).length ? iconButton('move', `Move ${form.real} to another client`, { class: 'icon move' }) : null,
      // The reverse of Copy to Global, offered only on Global's own rows: give it to one client.
      cid === 'global' && !reason && moveTargets(cid).length
        ? iconButton('give', `Give ${form.real} to one client`, { class: 'icon to-client' }) : null,
      reason ? null : iconButton('ignore', `Ignore ${form.real} instead of tokenising it`, { class: 'icon to-ignore' }),
      iconButton('trash', `Delete ${form.real}`, { class: 'icon danger delete' })));
    return null;
  };
  normal();

  let editing = false;
  let saving = false;
  const enterEdit = () => {
    if (editing || readOnly) return;
    editing = true;
    tr.classList.add('editing');
    const realIn = h('input', { type: 'text', class: 'real', value: form.real, 'aria-label': 'Real identifier' });
    const tokenIn = h('input', { type: 'text', class: 'token', value: reason ? '' : form.token, placeholder: 'automatic', 'aria-label': 'Token' });
    const message = h('div', { class: 'error-text' });
    realTd.replaceChildren(realIn, message);
    tokenTd.replaceChildren(tokenIn);
    // The shape is what the token is built from, and it is guessed from the text. The guess is
    // right nearly always and useless when it is wrong: linkedin.com/in/janedoe reads as a name,
    // becomes "Org00", and "read my bio at Org00" means nothing to anybody. Correcting it here
    // re-derives the token, so it is offered where the real is edited and nowhere else.
    const kindIn = h('select', { class: 'kind', 'aria-label': `Shape of ${form.real}` },
      ...TOKEN_SHAPES.map((k) => h('option', { value: k, selected: k === form.kind ? true : null },
        kindLabel(k))));
    // When this form counts as the client rather than as the ordinary word it looks like
    // (feature 23, defect D2). A client called BIOS matched inside NetBIOS and SystemBIOS and
    // rewrote ordinary Windows vocabulary in every conversation, and there was no lever for it.
    //
    // A rule narrows only what is REPLACED. Detection and the canary keep using the unrestricted
    // form, so setting one here can never reduce what the filter finds - which is why it is safe
    // to offer on a row at all.
    const matchNow = matchMode(form.match);
    const matchIn = h('select', { class: 'match', 'aria-label': `When ${form.real} counts as the client` },
      h('option', { value: '', selected: matchNow.mode === '' ? true : null }, 'Matches anywhere'),
      h('option', { value: 'cased', selected: matchNow.mode === 'cased' ? true : null }, 'Only this capitalisation'),
      h('option', { value: 'near', selected: matchNow.mode === 'near' ? true : null }, 'Only beside a word'));
    const matchSide = h('select', { class: 'match-side', 'aria-label': 'Which side the word is on' },
      h('option', { value: 'followed_by', selected: matchNow.side === 'followed_by' ? true : null }, 'before'),
      h('option', { value: 'preceded_by', selected: matchNow.side === 'preceded_by' ? true : null }, 'after'));
    const matchWords = h('input', { type: 'text', class: 'match-words', value: matchNow.words,
                                    placeholder: 'Pty, Ltd, Group', 'aria-label': 'Corroborating words' });
    const matchNear = h('div', { class: 'match-near form-row' }, matchWords, matchSide, h('span', { class: 'hint' }, 'it'));
    const showNear = () => { matchNear.hidden = matchIn.value !== 'near'; };
    matchIn.addEventListener('change', showNear);
    showNear();
    kindTd.replaceChildren(kindIn, matchIn, matchNear);
    const save = iconButton('save', 'Save', { class: 'icon save' });
    const cancel = iconButton('cross', 'Cancel', { class: 'icon cancel' });
    // A stored token is kept verbatim for ever, which is what makes tokens stable across versions
    // and also what leaves a file that has been through an older version holding whatever it was
    // given then. This is the one entry, under today's rules (D6). Only where a token is already
    // stored: there is nothing to re-derive otherwise.
    const rederive = form.token
      ? iconButton('rederive', `Derive ${form.real}'s token again, under today's rules`,
                   { class: 'icon rederive' })
      : null;
    if (rederive) {
      rederive.addEventListener('click', async () => {
        if (saving) return;
        saving = true;
        try {
          const result = await postJSON('/api/mapping/form/rederive', { client: cid, real: form.real });
          say(result.token === form.token
            ? `${form.real} already has the token today's rules give.`
            : `${form.real} is now ${result.token}.`, 'ok');
          await reloadClient(cid, { saved: form.real });
        } catch (error) {
          saving = false;
          sayError(error, refusalHandlers(cid));
        }
      });
    }
    actions.replaceChildren(h('div', { class: 'actions-wrap' }, save, rederive, cancel));
    const check = () => validity([realIn, tokenIn], message, save);
    realIn.addEventListener('input', check);
    tokenIn.addEventListener('input', check);
    check();
    const commit = async () => {
      if (saving || !check()) return;
      const newReal = realIn.value.trim();
      const newToken = tokenIn.value.trim();
      const tokenChanged = Boolean(newToken) && newToken !== form.token;
      const kindChanged = kindIn.value !== form.kind;
      const newMatch = matchValue(matchIn.value, matchSide.value, matchWords.value);
      // Compared as JSON because a rule is a value, not a string, and "unchanged" has to survive
      // "cased" vs {"cased": true} meaning the same thing.
      const matchChanged = JSON.stringify(newMatch) !== JSON.stringify(form.match || null);
      if (newReal === form.real && !tokenChanged && !kindChanged && !matchChanged && !reason) {
        tr.replaceWith(entryRow(cid, form, reason, readOnly));
        return;
      }
      saving = true;
      try {
        const body = { client: cid, real: form.real, new_real: newReal,
          new_token: tokenChanged ? newToken : null, new_kind: kindChanged ? kindIn.value : null };
        // Absent means "leave the rule alone"; present-and-null means "take it off". Sending it
        // only when it changed is what stops an ordinary rename from dropping somebody's rule.
        if (matchChanged) body.match = newMatch;
        await postJSON('/api/mapping/form/update', body);
        await reloadClient(cid, { saved: newReal });
      } catch (error) {
        saving = false;
        sayError(error, refusalHandlers(cid));
      }
    };
    save.addEventListener('click', commit);
    cancel.addEventListener('click', () => tr.replaceWith(entryRow(cid, form, reason, readOnly)));
    tr.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); commit(); }
      if (event.key === 'Escape') tr.replaceWith(entryRow(cid, form, reason, readOnly));
    });
    tr.addEventListener('focusout', (event) => {
      if (!tr.isConnected || tr.contains(event.relatedTarget)) return;
      commit();
    });
    realIn.focus();
  };

  if (!form.locked) realTd.addEventListener('click', enterEdit);
  actions.addEventListener('click', async (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.classList.contains('edit')) enterEdit();
    else if (button.classList.contains('delete')) {
      actions.replaceChildren(h('span', { class: 'confirm' }, 'Delete this entry?',
        h('button', { type: 'button', class: 'danger confirm-delete' }, 'Delete'),
        h('button', { type: 'button', class: 'cancel-delete' }, 'Keep')));
    } else if (button.classList.contains('cancel-delete') || button.classList.contains('cancel-move')
               || button.classList.contains('cancel-ignore')) normal();
    else if (button.classList.contains('to-ignore')) {
      // A declared entry is tokenised whatever any ignore list says, so this takes it off the token
      // list and puts it on an ignore list in one step: this client's, or Global's for everywhere.
      actions.replaceChildren(h('span', { class: 'confirm' }, 'Ignore instead of tokenising:',
        cid !== 'global' ? h('button', { type: 'button', class: 'confirm-ignore', dataset: { scope: cid } }, 'For this client') : null,
        h('button', { type: 'button', class: 'confirm-ignore', dataset: { scope: 'global' } }, 'Everywhere'),
        h('button', { type: 'button', class: 'cancel-ignore' }, 'Cancel')));
    } else if (button.classList.contains('confirm-ignore')) {
      try {
        await postJSON('/api/mapping/form/ignore', { client: cid, real: form.real, scope: button.dataset.scope });
        say(`${form.real} is no longer tokenised and is ignored ${button.dataset.scope === 'global' ? 'everywhere' : `for ${displayName(rowById(cid) || { id: cid })}`}.`, 'ok');
        await reloadClient(cid);
      } catch (error) {
        sayError(error, refusalHandlers(cid));
      }
    }
    else if (button.classList.contains('copy-global')) {
      try {
        await postJSON('/api/mapping/form/copy-to-global', { client: cid, real: form.real });
        say(`Copied ${form.real} to Global. The ${displayName(rowById(cid) || { id: cid })} entry stays, locked, for anyone sharing this client.`, 'ok');
        await reloadClient(cid);
      } catch (error) {
        sayError(error, refusalHandlers(cid));
      }
    } else if (button.classList.contains('to-client')) {
      const select = h('select', { class: 'move-to', 'aria-label': `Give ${form.real} to` },
        moveTargets(cid).map((t) => h('option', { value: t.id }, t.label)));
      actions.replaceChildren(h('span', { class: 'confirm' }, 'Give to', select,
        h('button', { type: 'button', class: 'primary confirm-to-client' }, 'Give'),
        h('button', { type: 'button', class: 'cancel-move' }, 'Cancel'),
        h('span', { class: 'hint' }, 'If that client already has it, this only unlocks their entry.')));
    } else if (button.classList.contains('confirm-to-client')) {
      const to = actions.querySelector('select.move-to').value;
      try {
        const answer = await postJSON('/api/mapping/form/from-global', { real: form.real, to });
        say(`${form.real} now belongs to ${to} as ${answer.token}.`, 'ok');
        await reloadClient(cid);
      } catch (error) {
        sayError(error, refusalHandlers(cid));
      }
    } else if (button.classList.contains('move')) {
      const select = h('select', { class: 'move-to', 'aria-label': `Move ${form.real} to` },
        moveTargets(cid).map((t) => h('option', { value: t.id }, t.label)));
      actions.replaceChildren(h('span', { class: 'confirm' }, 'Move to', select,
        h('button', { type: 'button', class: 'primary confirm-move' }, 'Move'),
        h('button', { type: 'button', class: 'cancel-move' }, 'Cancel'),
        h('span', { class: 'hint' }, 'This changes the client for everyone who shares it.')));
    } else if (button.classList.contains('confirm-move')) {
      const to = actions.querySelector('select.move-to').value;
      try {
        await postJSON('/api/mapping/form/move', { client: cid, real: form.real, to });
        say(`Moved ${form.real} to ${to}.`, 'ok');
        await reloadClient(cid);
      } catch (error) {
        sayError(error, refusalHandlers(cid));
      }
    } else if (button.classList.contains('confirm-delete')) {
      try {
        await postJSON('/api/mapping/form/delete', { client: cid, real: form.real });
        say(`Deleted ${form.real}.`, 'ok');
        await reloadClient(cid);
      } catch (error) {
        sayError(error, refusalHandlers(cid));
      }
    }
  });
  return tr;
}

function addEntryForm(cid) {
  const real = h('input', { type: 'text', name: 'real', placeholder: 'real identifier', 'aria-label': 'Real identifier' });
  const token = h('input', { type: 'text', name: 'token', placeholder: 'token (blank = automatic)', 'aria-label': 'Token' });
  const submit = h('button', { type: 'submit', class: 'primary' }, icon('plus'), 'Add entry');
  const message = h('span', { class: 'error-text' });
  const form = h('form', { class: 'add-entry form-row' }, real, token, submit, message);
  const check = () => validity([real, token], message, submit);
  real.addEventListener('input', check);
  token.addEventListener('input', check);
  check();
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!check()) return;
    try {
      await postJSON('/api/mapping/form', { client: cid, real: real.value.trim(), token: token.value.trim() || null });
      const added = real.value.trim();
      await reloadClient(cid, { saved: added });
      // Back to the left-hand box, ready for the next one: entering a list of identifiers should
      // feel like filling in a spreadsheet, not like clicking into the same field a hundred times.
      focusFirst('form.add-entry');
    } catch (error) {
      sayError(error, refusalHandlers(cid));
    }
  });
  track(form);                          // an entry typed and not added is unsaved (D98)
  return form;
}

// Ignoring one string is a judgement about a string. Ignoring eleven hosts under one domain is a
// judgement about a pattern, and nothing read it: the list grew a row at a time and the detector
// that proposed them all was unchanged (feature 31).
//
// This is the ignore list learning to compress itself, not a new detector. It only ever suggests
// something the person has already said eleven times in longhand.
const COMPRESS_AT = 4;   // below this it is a coincidence, not a pattern
const STEM_MIN = 4;      // DECISIONS 25: a shared prefix or suffix of at least four characters
const HOST = /^[a-z0-9-]+(\.[a-z0-9-]+)+$/;

// Four or more hosts under one domain: ignore the domain. The server treats an ignored domain as
// covering the hosts under it (D67 - it did not, so accepting this used to undo all four).
function parentPatterns(texts) {
  const byParent = new Map();
  for (const text of texts) {
    // Hosts only. A parent of an email or a free string is not a thing you can ignore.
    if (!HOST.test(text)) continue;
    const parts = text.split('.');
    // Two labels is already the parent; three or more have one worth suggesting. Stop at the
    // last two labels rather than guessing at a public-suffix list: "co.uk" would be wrong, and
    // suggesting it is harmless because the person still has to agree.
    if (parts.length < 3) continue;
    const parent = parts.slice(-2).join('.');
    if (!byParent.has(parent)) byParent.set(parent, []);
    byParent.get(parent).push(text);
  }
  return [...byParent.entries()]
    .filter(([, hosts]) => hosts.length >= COMPRESS_AT)
    .map(([parent, hosts]) => ({ entry: parent, covered: hosts, says: `ignored under ${parent}` }));
}

// Four or more ignored strings that start (or end) with the same four characters or more: ignore
// the stem as a pattern - `rg-aue-*`, `*-validation` (feature 31's other two, DECISIONS 25). Hosts
// are left to the domain rule above; a suffix of hosts is just their domain said worse.
function stemPatterns(texts, fromEnd) {
  const flip = (s) => (fromEnd ? [...s].reverse().join('') : s);
  const words = texts.filter((t) => !HOST.test(t) && !t.includes('*')).map(flip).sort();
  const found = [];
  // In sorted order, any four sharing a stem sit together, so each window of four is checked for
  // the stem they share.
  for (let i = 0; i + COMPRESS_AT <= words.length; i++) {
    const a = words[i];
    const b = words[i + COMPRESS_AT - 1];
    let n = 0;
    while (n < a.length && n < b.length && a[n] === b[n]) n++;
    if (n < STEM_MIN) continue;
    const stem = a.slice(0, n);
    const covered = words.filter((w) => w.startsWith(stem) && w.length > stem.length);
    if (covered.length >= COMPRESS_AT) {
      const shown = flip(stem);
      found.push({ entry: fromEnd ? `*${shown}` : `${shown}*`, covered: covered.map(flip),
        says: fromEnd ? `ignored ending "${shown}"` : `ignored starting "${shown}"` });
    }
  }
  return found;
}

function ignorePatterns(values) {
  // Matched in lower case, removed as stored: an entry saved as "Help.Vendor.org" is deleted by
  // that spelling, and a lower-cased one would miss it and leave the list holding both.
  const stored = new Map(values.map((value) => [String(value || '').trim().toLowerCase(), String(value)]));
  const texts = [...stored.keys()].filter(Boolean);
  const offers = [...parentPatterns(texts), ...stemPatterns(texts, false), ...stemPatterns(texts, true)]
    .filter((offer) => !texts.includes(offer.entry))
    .sort((a, b) => b.covered.length - a.covered.length || a.entry.length - b.entry.length);
  // One offer per set of rows: a longer stem over the same rows says nothing the shorter one did
  // not, and two offers that would each remove the same entries are one decision asked twice.
  const kept = [];
  for (const offer of offers) {
    if (!kept.some((k) => offer.covered.every((c) => k.covered.includes(c)))) kept.push(offer);
  }
  return kept.map((offer) => ({ ...offer, covered: offer.covered.map((c) => stored.get(c) || c) }));
}

function ignoresBlock(cid, values) {
  const scope = cid === 'global' ? 'global' : cid;
  const input = h('input', { type: 'text', placeholder: 'add a string to ignore', 'aria-label': 'String to ignore' });
  const form = h('form', { class: 'add-ignore form-row' }, input, h('button', { type: 'submit' }, icon('plus'), 'Add'));
  track(form);                          // a value typed and not added is unsaved (D98)
  // Two buttons, both always there. A checkbox that turns the x into something else would mean a
  // control that has learned to do one thing quietly does another, and the second action would be
  // hidden behind a switch nobody knows to look for. The destination needs no asking: an ignore
  // already lives in a scope, and it goes onto the token list of that same scope.
  const where = cid === 'global' ? 'everywhere' : displayName(rowById(cid) || { id: cid });
  const chips = h('div', { class: 'ignores' }, values.length
    ? values.map((value) => h('span', { class: 'chip' }, value,
      iconButton('tokenise', `Tokenise ${value} for ${where} instead of ignoring it`,
        { class: 'icon tokenise-ignore', dataset: { value } }),
      // An ignore can be in the wrong place the same way a token can. /api/ignore/move existed
      // from the start and nothing ever called it, so the only way to correct one was to remove it
      // and retype it on the right page. No lock to worry about: an ignore is not a token.
      moveIgnoreTargets(cid).length
        ? iconButton('shuffle', `Move ignoring ${value} to another list`, { class: 'icon move-ignore', dataset: { value } })
        : null,
      h('button', { type: 'button', class: 'remove-ignore', 'aria-label': `Stop ignoring ${value}`, dataset: { value } }, 'x')))
    : h('span', { class: 'hint' }, 'Nothing ignored yet.'));
  chips.addEventListener('click', async (event) => {
    const picker = event.target.closest('button.move-ignore');
    if (picker) {
      const select = h('select', { class: 'ignore-to', 'aria-label': `Move ignoring ${picker.dataset.value} to` },
        moveIgnoreTargets(cid).map((t) => h('option', { value: t.id }, t.label)));
      const go = h('button', { type: 'button', class: 'primary confirm-move-ignore', dataset: { value: picker.dataset.value } }, 'Move');
      picker.replaceWith(h('span', { class: 'confirm' }, select, go));
      return;
    }
    const confirmMove = event.target.closest('button.confirm-move-ignore');
    if (confirmMove) {
      const to = chips.querySelector('select.ignore-to').value;
      try {
        await postJSON('/api/ignore/move', { value: confirmMove.dataset.value, scope, to });
        say(`Ignoring ${confirmMove.dataset.value} moved to ${to}.`, 'ok');
        await reloadClient(cid);
      } catch (error) {
        sayError(error, refusalHandlers(cid));
      }
      return;
    }
    const button = event.target.closest('button.remove-ignore, button.tokenise-ignore');
    if (!button) return;
    const moving = button.classList.contains('tokenise-ignore');
    try {
      const answer = await postJSON(moving ? '/api/ignore/tokenise' : '/api/ignore/delete',
        { value: button.dataset.value, scope });
      if (moving) say(`${button.dataset.value} is now tokenised as ${answer.token}.`, 'ok');
      await reloadClient(cid);
    } catch (error) {
      sayError(error, refusalHandlers(cid));
    }
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) return;
    try {
      await postJSON('/api/ignore', { value, scope });
      await reloadClient(cid);
      focusFirst('form.add-ignore');
    } catch (error) {
      sayError(error, refusalHandlers(cid));
    }
  });
  // "You have ignored eleven things under salesforce.com; ignore the domain?" (31). Offered,
  // never applied on its own: it is a summary of a judgement already made by hand, and the
  // person still has to agree to the shorter form of it.
  const found = ignorePatterns(values);
  const suggestions = h('div', { class: 'ignore-patterns' }, ...found.map((offer) =>
    h('p', { class: 'hint ignore-pattern' },
      `${offer.covered.length} ${offer.says}: `,
      h('button', {
        type: 'button', class: 'link ignore-parent', dataset: { parent: offer.entry },
        title: `Ignore ${offer.entry} instead of these ${offer.covered.length} entries: ${offer.covered.join(', ')}`,
        // Said, because it is what the entry does (feature 76): a value carrying a client's name is
        // never covered by a wildcard or a parent, so it still reaches Review.
      }, `ignore ${offer.entry} instead, unless it contains a client's name`))));
  suggestions.addEventListener('click', async (event) => {
    const button = event.target.closest('button.ignore-parent');
    if (!button) return;
    const parent = button.dataset.parent;
    const covered = (found.find((offer) => offer.entry === parent) || { covered: [] }).covered;
    button.disabled = true;
    try {
      await postJSON('/api/ignore', { value: parent, scope });
      // The rows it replaces go with it, or the list has both and has compressed nothing.
      for (const host of covered) {
        try {
          await postJSON('/api/ignore/delete', { value: host, scope });
        } catch (error) { /* one that will not go is not worth losing the parent over */ }
      }
      say(`Ignoring ${parent}, and removed ${covered.length} entries it covers.`, 'ok');
      await reloadClient(cid);
    } catch (error) {
      button.disabled = false;
      sayError(error, refusalHandlers(cid));
    }
  });
  return h('div', { class: 'card' },
    h('h3', {}, cid === 'global' ? 'Ignored everywhere' : `Ignored for ${displayName(rowById(cid) || { id: cid })}`),
    h('p', { class: 'hint' }, cid === 'global'
      ? 'Strings that look like identifiers but are not client-specific, such as vendors and public services.'
      : 'Never proposed for review again for this client.'),
    chips, suggestions, form);
}


// Acting on many entries at once. The same endpoints the row buttons use, one call each: a
// thousand-row client is where this matters, and it is exactly where doing it a row at a time is
// unbearable.
// A row put aside by the filter box or by a kind toggle is out of view, and an action never
// touches a row that is out of view - the count follows the actions, not the other way round.
const IN_VIEW = 'tr.entry:not(.hidden-by-filter):not(.hidden-by-kind)';

function selectedReals(table) {
  return [...table.querySelectorAll(IN_VIEW)]
    .filter((tr) => { const box = tr.querySelector('input.pick'); return box && box.checked; })
    .map((tr) => tr.dataset.real);
}

// One request for the whole selection, and a report of what would not go.
//
// This used to send one request per row inside a single try, so the first refusal abandoned every
// row after it, left the rows before it already changed on the server, and skipped the reload -
// so the table went on showing state that no longer existed, which then produced more refusals
// (D23). It was also one whole-mapping rebuild per row (D36).
// The entries about to be acted on, as {real, token}, read off the table before anything changes.
//
// The token is the part that matters (D42). Putting an entry back with a freshly derived token
// would leave whatever already carries the old one unrehydratable - a document, a colleague's
// copy of the file - so the undo has to carry the pairs, and the only moment they are all still
// on screen is before the action runs.
function snapshot(cid, values) {
  const sec = view.main.querySelector(`section.client[data-client-id="${CSS.escape(cid)}"]`);
  const wanted = new Set(values);
  const rows = sec ? [...sec.querySelectorAll('tr.entry')] : [];
  return rows
    .filter((tr) => wanted.has(tr.dataset.real))
    .map((tr) => ({ real: tr.dataset.real, token: (tr.querySelector('td.token') || {}).textContent || '' }));
}

// `undo` is {label, ...body} naming what the inverse needs: `unignore` a scope to come off, and
// `from_global` when the action put things there. The entries are added here from the snapshot.
async function bulkReal(cid, values, post, done, undo) {
  const before = undo ? snapshot(cid, values) : [];
  try {
    const result = await post(values);
    const went = (result.done || []).length;
    const refused = result.refused || [];
    // Only what actually happened can be put back, so the undo is built from `done`.
    const wentSet = new Set(result.done || []);
    const back = before.filter((e) => wentSet.has(e.real));
    const action = (undo && back.length) ? {
      label: undo.label || 'Undo',
      run: async () => {
        const { label: _label, ...rest } = undo;
        const answer = await postJSON('/api/mapping/forms/restore',
          { client: cid, entries: back, ...rest });
        const put = (answer.done || []).length;
        const stuck = answer.refused || [];
        say(stuck.length
          ? `Put ${put} back. ${stuck.length} refused - ${stuck[0].real}: ${stuck[0].why}`
          : `Put ${put} back.`, stuck.length ? 'warn' : 'ok');
        await reloadClient(cid);
      },
    } : null;
    if (!refused.length) {
      say(done(went), 'ok', { action });
    } else {
      const first = refused[0];
      say(`${done(went)} ${refused.length} refused - ${first.real}: ${first.why}`
        + (refused.length > 1 ? ` (and ${refused.length - 1} more)` : ''),
        went ? 'info' : 'error', { action });
    }
  } catch (error) {
    sayError(error, refusalHandlers(cid));
  } finally {
    await reloadClient(cid);          // whatever happened, the table must show what is true now
  }
}

// For the actions that have no bulk endpoint yet: try them all, keep what holds, always reload.
// The row-at-a-time bulk path, for actions with no single-write endpoint. `undoFrom` is the
// client the entries end up in, so the inverse is a restore there back to here (D42).
async function eachReal(cid, values, send, done, undoFrom) {
  const before = undoFrom ? snapshot(cid, values) : [];
  const refused = [];
  const moved = [];
  for (const real of values) {
    try {
      await send(real);
      moved.push(real);
    } catch (error) {
      refused.push({ real, why: error && error.message ? error.message : String(error) });
    }
  }
  const went = moved.length;
  const back = before.filter((e) => moved.includes(e.real));
  // Moving back is the inverse: take them out of where they went, put them where they were.
  const action = (undoFrom && back.length) ? {
    label: 'Undo the move',
    run: async () => {
      for (const entry of back) {
        try {
          await postJSON('/api/mapping/form/delete', { client: undoFrom, real: entry.real });
        } catch (error) { /* it may already be gone; the restore below is what matters */ }
      }
      const answer = await postJSON('/api/mapping/forms/restore', { client: cid, entries: back });
      const stuck = answer.refused || [];
      say(stuck.length
        ? `Put ${(answer.done || []).length} back. ${stuck.length} refused - ${stuck[0].real}: ${stuck[0].why}`
        : `Put ${(answer.done || []).length} back.`, stuck.length ? 'warn' : 'ok');
      await reloadClient(cid);
    },
  } : null;
  if (!refused.length) {
    say(done(went), 'ok', { action });
  } else {
    const first = refused[0];
    say(`${done(went)} ${refused.length} refused - ${first.real}: ${first.why}`
      + (refused.length > 1 ? ` (and ${refused.length - 1} more)` : ''), went ? 'info' : 'error',
      { action });
  }
  await reloadClient(cid);
}

// The bulk bar, worded by the INTENT of the outcome rather than by the mechanic (feature 38,
// option C, chosen from a mockup 2026-09-27).
//
// The old bar argued with itself: "Move selected" and "Copy to Global" are what the system does,
// and the asymmetry between them - an ignore moves, a token copies - had to be explained every
// time. Grouped under headings that say the outcome, the mechanic never has to surface, and
// "who they should belong to" and "stop watching for them" are never adjacent, which is how a
// mis-click between two unrelated outcomes happened. `bulkGroup` lives in dom.js: Review uses it
// too (D45).
function bulkEntries(cid, table) {
  const here = displayName(rowById(cid) || { id: cid });
  const count = h('span', { class: 'muted bulk-count' }, '0 selected items');
  const targets = moveTargets(cid);
  const moveTo = targets.length
    ? h('select', { class: 'bulk-move-to', 'aria-label': 'Which client they should belong to' },
      targets.map((t) => h('option', { value: t.id }, t.label)))
    : null;
  const remove = h('button', { type: 'button', class: 'danger bulk-delete' }, icon('trash'), 'Delete them');
  const move = moveTo ? h('button', { type: 'button', class: 'bulk-move' }, 'Make them theirs') : null;
  // Global's own rows are already everyone's, so there is nothing to promote them to.
  const global = cid === 'global' ? null
    : h('button', { type: 'button', class: 'bulk-global' }, 'Everyone');
  const ignore = h('button', { type: 'button', class: 'bulk-to-ignore' },
    cid === 'global' ? 'Everywhere' : `In ${here}`);
  const ignoreAll = cid === 'global' ? null
    : h('button', { type: 'button', class: 'bulk-to-ignore-global' }, 'Everywhere');

  const bar = h('div', { class: 'bulk-entries' },
    h('span', { class: 'bulk-count-wrap' }, count),
    (global || move) ? bulkGroup('Who they should belong to', global, moveTo, move) : null,
    bulkGroup('Stop watching for them', ignore, ignoreAll),
    bulkGroup('Remove', remove));

  const need = () => {
    const values = selectedReals(table);
    if (!values.length) say('Tick at least one entry first.');
    return values;
  };
  const recount = () => {
    const picked = selectedReals(table).length;
    count.textContent = `${picked} selected item${picked === 1 ? '' : 's'}`;
    for (const button of [remove, move, ignore, global, ignoreAll]) if (button) button.disabled = !picked;
  };
  table.addEventListener('change', (event) => {
    if (event.target.classList.contains('pick-all')) {
      for (const tr of table.querySelectorAll(IN_VIEW)) {
        const box = tr.querySelector('input.pick');
        if (box) box.checked = event.target.checked;
      }
    }
    recount();
  });
  recount();

  remove.addEventListener('click', () => {
    const values = need();
    if (!values.length) return;
    // Deleting is the one action here that cannot be undone from the page, so it asks first.
    const confirmBox = h('span', { class: 'confirm' }, `Delete ${values.length} ${values.length === 1 ? 'entry' : 'entries'}?`,
      h('button', { type: 'button', class: 'danger go' }, 'Delete'),
      h('button', { type: 'button', class: 'cancel' }, 'Keep'));
    remove.replaceWith(confirmBox);
    confirmBox.querySelector('.cancel').addEventListener('click', () => confirmBox.replaceWith(remove));
    confirmBox.querySelector('.go').addEventListener('click', () => bulkReal(cid, values,
      (reals) => postJSON('/api/mapping/forms/delete', { client: cid, reals }),
      (n) => `Deleted ${n} ${n === 1 ? 'entry' : 'entries'}.`,
      { label: 'Undo the delete' }));
  });

  if (move) {
    move.addEventListener('click', () => {
      const values = need();
      if (!values.length) return;
      const to = moveTo.value;
      eachReal(cid, values, (real) => postJSON('/api/mapping/form/move', { client: cid, real, to }),
        (n) => `Moved ${n} ${n === 1 ? 'entry' : 'entries'} to ${to}.`, to);
    });
  }

  if (global) {
    global.addEventListener('click', () => {
      const values = need();
      if (!values.length) return;
      // "Everyone" is a copy, not a move: the client's own entry stays and is locked, so anyone
      // sharing that file keeps working. The bar never says "copy" - that is the mechanic.
      eachReal(cid, values,
        (real) => postJSON('/api/mapping/form/copy-to-global', { client: cid, real }),
        (n) => `${n} ${n === 1 ? 'item belongs' : 'items belong'} to everyone now.`);
    });
  }

  const ignoreIn = (scope, label) => () => {
    const values = need();
    if (!values.length) return;
    bulkReal(cid, values,
      (reals) => postJSON('/api/mapping/forms/ignore', { client: cid, reals, scope }),
      (n) => `${n} ${n === 1 ? 'item is' : 'items are'} no longer tokenised, and ignored ${label}.`,
      // Two halves to reverse: off the ignore list, and back onto the token list.
      { label: 'Undo the ignore', unignore: scope });
  };
  // Scope is this page: a client's entries are ignored for that client, Global's everywhere.
  ignore.addEventListener('click', ignoreIn(cid === 'global' ? 'global' : cid,
                                            cid === 'global' ? 'everywhere' : `in ${here}`));
  if (ignoreAll) ignoreAll.addEventListener('click', ignoreIn('global', 'everywhere'));
  return bar;
}

function tools(cid, clientView, table, readOnly) {
  const filter = h('input', { type: 'search', class: 'entry-filter', placeholder: 'Filter entries (real value, token, kind, ignored)', 'aria-label': 'Filter entries' });
  filter.addEventListener('input', () => {
    const q = filter.value.trim().toLowerCase();
    for (const tr of table.querySelectorAll('tr.entry')) {
      tr.classList.toggle('hidden-by-filter', Boolean(q) && !tr.textContent.toLowerCase().includes(q)
        && !(tr.dataset.real || '').toLowerCase().includes(q));
    }
    // The ignored values too (the user, 2026-10-06): they are this client's entries as much as its
    // tokens are, and a long ignore list is where something is hardest to find by eye.
    const sec = table.closest('section');
    for (const chip of sec ? sec.querySelectorAll('.ignores > .chip') : []) {
      const value = (chip.querySelector('[data-value]') || {}).dataset;
      chip.classList.toggle('hidden-by-filter', Boolean(q) && !String((value && value.value) || chip.textContent).toLowerCase().includes(q));
    }
    // The count is of SELECTED AND VISIBLE rows, which is what the actions operate on - so
    // changing what is visible changes it. Only a checkbox used to trigger the recount, which
    // left the number showing whatever the visible selection was when one was last touched:
    // tick five, filter two away, touch anything and it drops to three, clear the filter and it
    // stayed at three (D40). The count follows the actions; it must not lead them.
    table.dispatchEvent(new Event('change', { bubbles: true }));
  });
  // Which kinds to show. The filter box is a substring match, so it can only ever say "show me
  // things containing X"; what a large mapping needs is subtractive - everything EXCEPT the
  // emails and the GUIDs, which are the bulk of it and the least interesting to look at (33).
  // On by default, click to put one aside.
  const kindsPresent = [...new Set(clientView.forms.map((f) => f.kind).filter(Boolean))].sort();
  const hiddenKinds = new Set();
  const applyKinds = () => {
    for (const row of table.querySelectorAll('tr.entry')) {
      row.classList.toggle('hidden-by-kind', hiddenKinds.has(row.dataset.kind || ''));
    }
    // The count is of selected AND VISIBLE rows, and hiding a kind changes what is visible.
    table.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const kindToggles = kindsPresent.length > 1
    ? h('div', { class: 'kind-toggles' }, ...kindsPresent.map((kind) => {
      // A filter chip: lit while its rows show. The title says what a click does (2026-10-01).
      const button = h('button', { type: 'button', class: 'chip kind-toggle on', 'aria-pressed': 'true',
        dataset: { kind }, title: `Hide ${kindLabel(kind)} rows` }, kindLabel(kind));
      button.addEventListener('click', () => {
        const off = hiddenKinds.has(kind);
        if (off) hiddenKinds.delete(kind); else hiddenKinds.add(kind);
        button.classList.toggle('on', off);
        button.setAttribute('aria-pressed', String(off));
        button.title = `${off ? 'Hide' : 'Show'} ${kindLabel(kind)} rows`;
        applyKinds();
      });
      return button;
    }))
    : null;

  const bar = h('div', { class: 'tools' }, filter, kindToggles);
  // The headings stick BELOW the bar, so they need its height. Measured rather than assumed,
  // because the bar wraps to two lines on a narrow window and to one on a wide one, and a wrong
  // offset either overlaps the first row or leaves a gap the rows scroll through (32).
  //
  // Set through the CSSOM, which the CSP allows - what it forbids is a style attribute in markup,
  // which is why generated rows carry their indent as a data attribute. coltable.js sets column
  // widths the same way.
  const measureTools = () => {
    const body = bar.closest('.client-body');
    if (body) body.style.setProperty('--tools-h', `${Math.round(bar.getBoundingClientRect().height)}px`);
  };
  if (typeof ResizeObserver === 'function') new ResizeObserver(measureTools).observe(bar);
  requestAnimationFrame(measureTools);
  if (!readOnly) bar.append(bulkEntries(cid, table));
  if (clientView.source !== 'none') {
    bar.append(h('a', { class: 'btn export', href: `/api/export/client/${encodeURIComponent(cid)}`,
                        download: `${cid}.json`,
                        title: `Everything in ${cid}'s file: every entry and its token, and the ignore list. Not the selection.` },
      // "Export" beside a bar that can say "133 selected" read as though it might take the
      // selection. It never has: the endpoint takes a client id and nothing else.
      //
      // Decided 2026-09-27: name what it takes rather than react to the selection (feature 40).
      // A label that says "mapping and ignore policies" is not about the selection at all, so
      // the count beside it stops being a question. Exporting a selection would need a new
      // endpoint and is not wanted for now.
      icon('download'), 'Export mapping and ignore policies'));
  }
  if (cid !== 'global') {
    const archive = h('button', { type: 'button', class: 'archive' }, icon('archive'), 'Archive');
    archive.addEventListener('click', () => {
      const confirmBox = h('span', { class: 'confirm' }, `Archive ${cid}? Its file moves to clients\\_archive and its tokens stay reserved.`,
        h('button', { type: 'button', class: 'danger confirm-archive' }, 'Archive'),
        h('button', { type: 'button', class: 'cancel-archive' }, 'Keep'));
      archive.replaceWith(confirmBox);
      confirmBox.querySelector('.cancel-archive').addEventListener('click', () => confirmBox.replaceWith(archive));
      confirmBox.querySelector('.confirm-archive').addEventListener('click', async () => {
        try {
          await postJSON('/api/mapping/client/archive', { id: cid });
          say(`Archived ${cid}.`, 'ok');
          expanded.delete(cid);
          view.ctx.status = await view.ctx.refresh();
          location.hash = '#/clients';
        } catch (error) {
          sayError(error);
        }
      });
    });
    bar.append(archive);
  }
  return bar;
}

// The OneDrive share and the primary repo used to be set here. Both are about this machine's disk,
// not about tokenising, so they moved to the Session page's profiles editor (D99).

// What this client is called, in the three places it has a name. The id is the one that leaves
// this window - it names the file, keys the mapping, and is what the command line wants - so it
// is here rather than only in the address bar.
function identityCard(cid, clientView, row) {
  const index = clientView.index ?? row.index;
  const command = cid === 'global'
    ? 'aicleaner scan <folder> --add global'
    : `aicleaner scan <folder> --add ${cid}`;
  const lines = [
    ['Client id', cid, 'Names the file and keys the mapping. What every command line wants.'],
    ['Display name', row.label || clientView.label || '(none: the id is shown)',
     'What you read, everywhere in here.'],
    ['Index', index === undefined || index === null ? '(derived from the id)' : String(index),
     'The number inside the tokens the AI sees.'],
  ];
  // Three facets of one fact - what this client is called - so they read side by side rather than
  // as three stacked rows costing a card's height at the top of the page, above the thing people
  // came for (34). A grid that wraps, so it is one column on a phone with no media query.
  //
  // The hints are the constraint: three sentences across three narrow columns is worse than three
  // rows, not better. So each is a title on the facet rather than always-on text, and the label
  // carries it too - a tooltip nobody can reach on a touch screen would be no explanation at all,
  // which is why the wording of each label says most of it.
  return h('section', { class: 'card', dataset: { card: 'identity' } },
    h('h2', {}, 'Names'),
    h('div', { class: 'identity-row' }, ...lines.map(([label, value, why]) =>
      h('div', { class: 'identity-facet', title: why },
        h('div', { class: 'identity-label' }, label),
        h('div', { class: 'mono identity-value' }, value),
        h('div', { class: 'hint identity-why' }, why)))),
    h('p', { class: 'hint' }, 'From a terminal: ', h('code', { class: 'mono' }, command)),
    cid === 'global' ? null : renumberRow(cid, index),
    cid === 'global' ? null : shareInOneDrive(cid));
}

// A different index for this client (DECISIONS 67): before sharing it, when a colleague's client
// has the same number - the two would produce the same tokens. Tokens already written stay.
function renumberRow(cid, index) {
  const box = h('input', { type: 'number', min: '1', class: 'renumber-index', 'aria-label': 'New index',
    value: index === undefined || index === null ? '' : String(index), 'data-unsaved-ignore': '' });
  const go = h('button', { type: 'button', class: 'renumber' }, 'Change index');
  go.addEventListener('click', async () => {
    try {
      await postJSON(`/api/clients/${encodeURIComponent(cid)}/index`, { index: Number(box.value) });
      say(`${cid} now derives new tokens from index ${box.value}. Tokens already written stay as they are.`, 'ok');
    } catch (error) { sayError(error); }
  });
  return h('details', { class: 'hint renumber-row' },
    h('summary', {}, 'Change the index (before sharing, if a colleague’s client has the same one)'),
    h('div', { class: 'form-row' }, box, go));
}

// Sharing this client with the engagement's people is done in OneDrive (DECISIONS 51, 67): this opens
// the client's folder there, where Share does people, groups and expiry properly. A work account
// opens the folder itself; a personal one opens OneDrive, with the folder's path shown to find.
function shareInOneDrive(cid) {
  const row = h('p', { class: 'hint share-onedrive' });
  getJSON(`/api/clients/${encodeURIComponent(cid)}/onedrive`).then((where) => {
    if (!where.url) {
      row.textContent = 'This client\'s folder is not in OneDrive, so it cannot be shared from there.';
      return;
    }
    const open = h('a', { class: 'button open-onedrive', href: where.url, target: '_blank', rel: 'noopener' },
      'Open in OneDrive');
    row.replaceChildren(open, ' ', where.exact
      ? 'and press Share there to give the engagement\'s people access.'
      : h('span', {}, 'then go to ', h('code', { class: 'mono' }, where.relative), ' and press Share there.'));
  }).catch(() => { row.hidden = true; });
  return row;
}

async function loadBody(cid, body) {
  let clientView;
  let ignores;
  try {
    [clientView, ignores] = await Promise.all([
      getJSON(`/api/clients/${encodeURIComponent(cid)}`), getJSON('/api/ignore')]);
  } catch (error) {
    body.replaceChildren(h('p', { class: 'error-text' }, error.message));
    return;
  }
  const row = rowById(cid) || { id: cid, state: ['ok'], entries: 0 };
  const err = row.error;
  const parts = [...problemCallout(row, clientView)];
  if (err && err.kind === 'syntax') {
    try {
      parts.push(rawView(await getJSON(`/api/clients/${encodeURIComponent(cid)}/raw`), row, cid));
    } catch (error) {
      sayError(error);
    }
  }
  const readOnly = clientView.source === 'lastgood';
  const badIndex = clientView.source === 'file' && err && err.kind === 'rule' ? err.index : undefined;
  // Addresses under an email rule are listed as the rule, not one by one (feature 96). Each row keeps
  // its index in the file, which is what a rejected row's error names.
  const allForms = clientView.forms;
  const listed = allForms.map((form, i) => ({ form, i })).filter(({ form, i }) => !form.rule || i === badIndex);
  clientView = { ...clientView, forms: listed.map(({ form }) => form) };
  const reasonFor = (form, i) => {
    if (i !== badIndex) return null;
    return `rejected: ${err.message}`;
  };
  const table = h('table', { class: 'entries', dataset: { resizable: 'entries' } },
    h('thead', {}, h('tr', {},
      readOnly ? h('th', { class: 'pick' }) : h('th', { class: 'pick', dataset: { col: 'pick' } }, pickBox('Select all', { class: 'pick-all' })),
      h('th', { dataset: { col: 'real' } }, 'Real'),
      h('th', { dataset: { col: 'token' } }, 'Token'),
      h('th', { dataset: { col: 'kind' } }, 'Kind'),
      h('th', { class: 'actions' }, 'Actions'))),
    h('tbody', {}, listed.map(({ form, i }) => entryRow(cid, form, reasonFor(form, i), readOnly))));
  const entriesCard = h('div', { class: 'card' }, tools(cid, clientView, table, readOnly),
    readOnly ? h('p', { class: 'hint' }, 'Showing the last good version, read-only until the file is restored or fixed.') : null,
    clientView.forms.length ? table : h('p', { class: 'empty' }, 'No entries yet.'),
    readOnly ? null : addEntryForm(cid));
  if (!clientView.forms.length) entriesCard.append(h('table', { class: 'entries', hidden: true }));
  const scopeValues = ((ignores.scopes || {})[cid === 'global' ? 'global' : cid]) || [];
  // Global is not an engagement, so there is no repo of theirs to scan for it.
  const scanCard = cid === 'global' ? null : scanBlock(cid);
  // setChildren, not replaceChildren: on Global scanCard and keyCard are null by design, and the
  // native method turns a non-Node into a text node, reading as 'nullnull' (D46). h() drops null,
  // and setChildren drops what h() drops.
  // A key per client and its held changes (DECISIONS 51): Global is never shared, so has neither.
  // The page is about tokenising only (D99): names, terms replaced, terms ignored, Scan, the key.
  const keyCard = cid === 'global' ? null : await keySection(cid, () => reloadClient(cid));
  setChildren(body, heldCallout(row, () => reloadClient(cid)), ...parts, identityCard(cid, clientView, row),
              emailRulesCard(cid, clientView.email_rules || [], allForms, readOnly),
              entriesCard, ignoresBlock(cid, scopeValues), scanCard, keyCard);
}

// One rule per email domain (feature 96): every address at one of the client's domains is the
// client's, added when met with a token every machine derives alike, and listed here as a count
// rather than row by row. Offered for each of the client's own domain and tenant entries.
function emailRulesCard(cid, rules, forms, readOnly) {
  if (cid === 'global') return null;
  const domains = forms.filter((f) => f.kind === 'domain' || f.kind === 'tenant').map((f) => String(f.real).toLowerCase());
  if (!domains.length && !rules.length) return null;
  const at = (domain) => forms.filter((f) => f.kind === 'email' && (String(f.real).toLowerCase().endsWith(`@${domain}`)
    || String(f.real).toLowerCase().endsWith(`.${domain}`))).length;
  const ruled = new Map(rules.map((r) => [r.domain, r.count]));
  const set = async (domain, on, button) => {
    button.disabled = true;
    try {
      await postJSON('/api/mapping/client/email-rule', { client: cid, domain, on });
      say(on ? `Every address at ${domain} is now ${cid}'s.` : `The rule for ${domain} is off; its addresses are listed again.`, 'ok');
      await reloadClient(cid);
    } catch (error) { button.disabled = false; sayError(error); }
  };
  const rows = [...new Set([...rules.map((r) => r.domain), ...domains])].sort().map((domain) => {
    const on = ruled.has(domain);
    const button = h('button', { type: 'button', class: on ? 'email-rule-off' : 'email-rule-on', disabled: readOnly },
      on ? 'Remove rule' : 'Make a rule');
    button.addEventListener('click', () => set(domain, !on, button));
    const count = on ? ruled.get(domain) : at(domain);
    return h('tr', { class: `email-rule${on ? ' on' : ''}`, dataset: { domain } },
      h('td', {}, h('code', {}, `*@${domain}`)),
      h('td', {}, on ? `${count} address${count === 1 ? '' : 'es'}, not listed one by one` : `no rule - ${count} address${count === 1 ? '' : 'es'} listed below`),
      h('td', { class: 'actions' }, button));
  });
  return h('div', { class: 'card email-rules' },
    h('h3', {}, 'Email rules'),
    h('p', { class: 'hint' }, 'A rule says every address at the domain is this client\'s: each is replaced the first time it is seen, whatever the email setting, with a token that is the same on every machine sharing this client. Addresses already here keep their tokens. Removing a rule lists them again; nothing is deleted.'),
    h('table', { class: 'entries email-rules-table' }, h('tbody', {}, rows)));
}

async function reloadClient(cid, opts = {}) {
  view.ctx.status = await view.ctx.refresh();
  const sec = view.main.querySelector(`section.client[data-client-id="${CSS.escape(cid)}"]`);
  if (!sec) return;
  const row = rowById(cid);
  if (row) paintHeader(sec, row);
  const body = sec.querySelector('.client-body');
  await loadBody(cid, body);
  if (opts.saved) {
    const tr = body.querySelector(`tr.entry[data-real="${CSS.escape(opts.saved)}"]`);
    const wrap = tr && tr.querySelector('td.actions .actions-wrap');
    if (wrap) {
      const tick = h('span', { class: 'saved' }, icon('check'), 'Saved');
      wrap.append(tick);
      setTimeout(() => tick.remove(), 2500);
    }
  }
}

// Feature 41: a refusal from the store can name an action as well as a reason. The store does not
// know what a route or a DOM node is, so it names the thing to do and this is where the page says
// what that means here. An action this page has no entry for simply gets no button - which is the
// right outcome, rather than a button that quietly does nothing.
function refusalHandlers(cid) {
  return {
    reload: () => reloadClient(cid),
    ignore_global: async (offer) => {
      await postJSON('/api/ignore', { value: offer.value, scope: 'global' });
      say(`${offer.value} is ignored everywhere.`, 'ok');
      await reloadClient(cid);
    },
    open_global: () => {
      const sec = view.main.querySelector('section.client[data-client-id="global"]');
      if (!sec) return;
      if (sec.hasAttribute('hidden')) sec.removeAttribute('hidden');
      sec.scrollIntoView({ block: 'start', behavior: 'smooth' });
    },
    find: (offer) => {
      const sec = view.main.querySelector(`section.client[data-client-id="${CSS.escape(cid)}"]`);
      const filter = sec && sec.querySelector('input.entry-filter');
      if (!filter) return;
      filter.value = offer.value;
      filter.dispatchEvent(new Event('input'));
      const tr = sec.querySelector(`tr.entry[data-real="${CSS.escape(offer.value)}"]`);
      if (tr) tr.scrollIntoView({ block: 'center', behavior: 'smooth' });
    },
  };
}

// ---- page ---------------------------------------------------------------------------------------

function addClientForm(list) {
  const form = h('form', { id: 'add-client-form', class: 'card form-row', hidden: true },
    h('label', { class: 'field' }, 'Client id', h('input', { type: 'text', name: 'id', required: true, placeholder: 'e.g. northwind' })),
    h('label', { class: 'field' }, 'Display name (optional)', h('input', { type: 'text', name: 'label', maxlength: '80', placeholder: 'e.g. Northwind Traders' })),
    h('label', { class: 'field' }, 'Index (optional)', h('input', { type: 'number', name: 'index', min: '1', placeholder: 'automatic' })),
    h('button', { type: 'submit', class: 'primary' }, icon('plus'), 'Add client'),
    h('span', { class: 'hint' }, 'Letters, digits, - and _. The index decides the tokens and is derived from the id unless you set one.'));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = form.elements.id.value.trim();
    const index = form.elements.index.value.trim();
    const label = form.elements.label.value.trim();
    try {
      await postJSON('/api/mapping/client', { id, index: index ? Number(index) : null, label: label || null });
      say(`Added client ${label || id}.`, 'ok');
      expanded.add(id);
      view.ctx.status = await view.ctx.refresh();
      form.reset();
      form.hidden = true;
      paintList(list);
    } catch (error) {
      sayError(error);
    }
  });
  return form;
}

function renameControl(row, head, title) {
  const button = iconButton('edit', 'Change the display name', { class: 'icon rename' });
  button.addEventListener('click', () => {
    const input = h('input', { type: 'text', name: 'label', maxlength: '80', value: row.label && row.label !== row.id ? row.label : '',
      placeholder: row.id, 'aria-label': 'Display name' });
    const form = h('form', { class: 'rename form-row' }, input,
      h('button', { type: 'submit', class: 'primary' }, icon('save'), 'Save'),
      h('button', { type: 'button', class: 'cancel' }, 'Cancel'),
      h('span', { class: 'hint' }, 'Leave empty to show the id.'));
    track(form);                        // a new name typed and not saved is unsaved (D98)
    form.querySelector('.cancel').addEventListener('click', () => form.replaceWith(button));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        await postJSON('/api/mapping/client/label', { id: row.id, label: input.value.trim() });
        view.ctx.status = await view.ctx.refresh();
        const fresh = rowById(row.id) || row;
        title.textContent = displayName(fresh);
        say('Display name saved.', 'ok');
        form.replaceWith(button);
      } catch (error) {
        sayError(error);
      }
    });
    button.replaceWith(form);
    input.focus();
  });
  return button;
}

// Find across every client (the user, 2026-10-06): a client stays in view when its name, or any of
// its real values, tokens or ignored entries, holds the text. One whose entries match opens, with
// its own entry filter set to the same text, so only the matching rows and ignored entries show.
// Clearing the box puts the list back: the clients this opened close again, their filters cleared.
async function findEverywhere(list, q) {
  const asked = (view.findAsked = (view.findAsked || 0) + 1);
  const sections = [...list.querySelectorAll('section.client')];
  if (!q) {
    for (const sec of sections) {
      sec.hidden = false;
      const filter = sec.querySelector('input.entry-filter');
      if (filter && filter.value) { filter.value = ''; filter.dispatchEvent(new Event('input')); }
      if (sec.dataset.openedByFind) {
        delete sec.dataset.openedByFind;
        const toggle = sec.querySelector('.client-toggle');
        if (toggle.getAttribute('aria-expanded') === 'true') toggle.click();
      }
    }
    view.findViews = null;
    return;
  }
  // What each client holds, read once per search (cleared when the box is emptied).
  if (!view.findViews) {
    const ignores = await getJSON('/api/ignore');
    const views = await Promise.all(sections.map((sec) => getJSON(`/api/clients/${encodeURIComponent(sec.dataset.clientId)}`)
      .catch(() => ({ forms: [] }))));
    view.findViews = new Map(sections.map((sec, i) => [sec.dataset.clientId, {
      texts: (views[i].forms || []).flatMap((f) => [f.real, f.token]).map((s) => String(s || '').toLowerCase()),
      ignored: ((ignores.scopes || {})[sec.dataset.clientId] || []).map((s) => String(s).toLowerCase()),
    }]));
  }
  if (asked !== view.findAsked) return;       // a newer search overtook this one
  for (const sec of sections) {
    const cid = sec.dataset.clientId;
    const held = view.findViews.get(cid) || { texts: [], ignored: [] };
    const named = sec.querySelector('.name').textContent.toLowerCase().includes(q) || cid.toLowerCase().includes(q);
    const inEntries = held.texts.some((s) => s.includes(q)) || held.ignored.some((s) => s.includes(q));
    sec.hidden = !named && !inEntries;
    if (!inEntries) continue;
    const toggle = sec.querySelector('.client-toggle');
    const body = sec.querySelector('.client-body');
    if (toggle.getAttribute('aria-expanded') !== 'true') {
      sec.dataset.openedByFind = '1';
      toggle.setAttribute('aria-expanded', 'true');
      body.hidden = false;
      expanded.add(cid);
      await loadBody(cid, body);
    }
    if (asked !== view.findAsked) return;
    const filter = body.querySelector('input.entry-filter');
    if (filter) { filter.value = q; filter.dispatchEvent(new Event('input')); }
  }
}

// A client a colleague shared (DECISIONS 67): after "Add shortcut to My files" in OneDrive, its
// folder is chosen here and joins this PC's clients.
function joinShared() {
  const chosen = h('input', { type: 'hidden', class: 'join-folder', 'data-unsaved-ignore': '' });
  chosen.addEventListener('input', async () => {
    const folder = chosen.value.trim();
    chosen.value = '';
    if (!folder) return;
    try {
      const answer = await postJSON('/api/clients/join', { folder });
      say(`${answer.client} joined from the shared folder.`, 'ok');
      location.hash = `#/clients/${encodeURIComponent(answer.client)}`;
    } catch (error) { sayError(error); }
  });
  return h('span', { class: 'join-shared', title: 'A colleague shared a client with you: add its folder to your OneDrive '
    + '("Add shortcut to My files"), then choose it here' }, chosen,
  folderPicker(chosen, { label: 'Joined a shared client?', title: 'Choose the client folder that was shared with you' }));
}

function paintList(list) {
  list.replaceChildren(...allRows(view.ctx.status).map((row) => section(row, expanded.has(row.id))));
}

export async function render(main, ctx) {
  view.main = main;
  view.ctx = ctx;
  const id = ctx.route.id;
  if (id) {
    const row = rowById(id);
    if (!row) {
      main.append(h('div', { class: 'page-head' }, h('h1', {}, id)), h('p', { class: 'error-text' }, `There is no client called ${id}. `),
        h('a', { id: 'show-all', href: '#/clients' }, 'Show all clients'));
      return;
    }
    const indexNote = row.id === 'global' ? GLOBAL_DESC : `id ${row.id}; tokens are derived from the client index`;
    const title = h('h1', {}, displayName(row));
    const head = h('div', { class: 'page-head' }, title);
    if (row.id !== 'global') head.append(renameControl(row, head, title));
    head.append(h('span', { class: 'muted' }, indexNote, ' - ', h('a', { id: 'show-all', href: '#/clients' }, 'show all clients')));
    main.append(head, h('div', { class: 'client-list' }, section(row, true)));
    return;
  }
  const find = h('input', { type: 'search', id: 'find-client', placeholder: 'Find a client, value, token or ignored entry',
    'aria-label': 'Find a client or an entry in any client' });
  const addButton = h('button', { type: 'button', id: 'add-client' }, icon('plus'), 'Add client');
  const list = h('div', { class: 'client-list', id: 'client-list' });
  const form = addClientForm(list);
  addButton.addEventListener('click', () => { form.hidden = !form.hidden; if (!form.hidden) form.elements.id.focus(); });
  find.addEventListener('input', () => {
    clearTimeout(view.findTimer);
    view.findTimer = setTimeout(() => findEverywhere(list, find.value.trim().toLowerCase()).catch(sayError), 250);
  });
  main.append(h('div', { class: 'page-head' }, h('h1', {}, 'Client Filters'),
    h('span', { class: 'muted' }, `${ctx.status.clients.length} ${ctx.status.clients.length === 1 ? 'client' : 'clients'} and Global`),
    h('div', { class: 'actions' }, find, addButton, joinShared())), form, list);
  paintList(list);
}

export function onStatus(status) {
  if (!view.main || !view.main.isConnected) return;
  view.ctx.status = status;
  for (const sec of view.main.querySelectorAll('section.client')) {
    const row = rowById(sec.dataset.clientId);
    if (row) paintHeader(sec, row);
  }
}
