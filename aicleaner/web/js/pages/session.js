// Session: drive the Claude Code sessions that run on this machine, from here or from a phone.
// Their traffic goes back through the proxy, so a prompt typed here is filtered like any other.
//
// Work is organised by engagement, so the page is too: a column of named sessions down the side,
// and one of them selected. Everything a person reads here is the profile's name and the client's
// label - never a client id, which is plumbing for the filter.

import { connected, getJSON, onConnection, postJSON, say, sayError } from '../api.js';
import { busyButton, h, iconButton, setChildren } from '../dom.js';
import { folderPicker } from '../picker.js';
import { pane } from '../panes.js';
import { downloadLink, fileTabs } from '../filetabs.js';
import { onFirstRow, onLastRow } from '../caretrow.js';
import { confirmLeave, forget, rebase, track } from '../unsaved.js';
import { fillShare, shareSlot } from '../sharecard.js';
import { handOverWheel } from '../wheelchain.js';
import { isSealed } from '../sealed.js';
import { renderMarkdown } from '../markdown.js';
import { paintWorktree } from '../worktree.js';
import { begin } from '../timing.js';
import { attachAutocorrect } from '../autocorrect.js';
import { clearSelection, groupMenu, isSelected, itemOf, MANY_TABS, menuItems, needsZip, selectedItems, selectOnly, selectRange, toggleOne,
  viewableFiles } from '../treeselect.js';

const view = { stream: null, state: null, name: '', poll: null, editing: false, profiles: null,
  past: [], commands: null, slashRows: [], slashAt: 0, transition: false };

// The proxy wraps every name it restored in these two sequences (see transform.strip_marks), so
// the page can show what the filter did. They are drawn as green spans, never as raw codes.
const MARK_OPEN = '\u001b[32;22m';
const MARK_CLOSE = '\u001b[39;22m';

// A reply drawn as the Markdown it is written in (D107: it showed **bold** as asterisks), with the
// restored names still green. The marks become two private-use characters before parsing - the
// escape codes carry a "[" the link parser could catch on - and green spans afterwards, carried
// across element boundaries, so a name inside **bold** or `code` keeps its colour.
const OPEN_PLACE = '';
const CLOSE_PLACE = '';

function replyMarkdown(text) {
  const doc = h('div', { class: 'md-doc md-reply' },
    renderMarkdown(String(text).split(MARK_OPEN).join(OPEN_PLACE).split(MARK_CLOSE).join(CLOSE_PLACE)));
  const walker = document.createTreeWalker(doc, NodeFilter.SHOW_TEXT);
  const texts = [];
  while (walker.nextNode()) texts.push(walker.currentNode);
  let green = false;
  for (const node of texts) {
    const value = node.nodeValue;
    if (!green && !value.includes(OPEN_PLACE) && !value.includes(CLOSE_PLACE)) continue;
    const pieces = [];
    let run = '';
    const flush = () => {
      if (!run) return;
      pieces.push(green ? h('span', { class: 'restored', title: 'Restored by aicleaner: the AI saw a token here' }, run)
        : document.createTextNode(run));
      run = '';
    };
    for (const ch of value) {
      if (ch === OPEN_PLACE) { flush(); green = true; } else if (ch === CLOSE_PLACE) { flush(); green = false; } else run += ch;
    }
    flush();
    node.replaceWith(...pieces);
  }
  return doc;
}

function withRestoredNames(text) {
  const nodes = [];
  let rest = text;
  while (rest) {
    const start = rest.indexOf(MARK_OPEN);
    if (start < 0) { nodes.push(rest); break; }
    if (start > 0) nodes.push(rest.slice(0, start));
    rest = rest.slice(start + MARK_OPEN.length);
    const end = rest.indexOf(MARK_CLOSE);
    const name = end < 0 ? rest : rest.slice(0, end);
    nodes.push(h('span', { class: 'restored', title: 'Restored by aicleaner: the AI saw a token here' }, name));
    rest = end < 0 ? '' : rest.slice(end + MARK_CLOSE.length);
  }
  return nodes;
}

// Follow the bottom only while the reader is AT the bottom.
//
// paint() ran `log.scrollTop = log.scrollHeight` unconditionally, and paint() is the poll
// handler - so scrolling up to read something was undone within seconds, with no new message
// needed. Now: note where the view was before touching the DOM, and put it back afterwards
// unless it was already at the bottom, in which case follow (D24).
const NEAR_BOTTOM_PX = 40;

function atBottom(log) {
  return log.scrollHeight - log.scrollTop - log.clientHeight <= NEAR_BOTTOM_PX;
}

// Held by the line being read, not by a scroll offset (D116, the user, 2026-10-06). Once the history
// is full each new line takes one off the top, and the same offset then showed later lines: what was
// being read moved up and out of view. The first line in view is found again after the change and
// put back where it was; only if it has itself dropped off does the view move, to the new first line.
// A line of the log: one drawn from the live history, or one "Show earlier" added above it (feature 82).
const LOG_LINES = ':scope > [data-seq], :scope > .log-head .log-earlier > [data-seq]';

function keepingPlace(log, change) {
  if (atBottom(log)) {
    change();
    log.scrollTop = log.scrollHeight;
    return;
  }
  const top = log.getBoundingClientRect().top;
  const lines = [...log.querySelectorAll(LOG_LINES)];
  const anchor = lines.find((line) => line.getBoundingClientRect().bottom > top + 1) || null;
  const offset = anchor ? anchor.getBoundingClientRect().top - top : 0;
  // A full redraw replaces the nodes, so the line is also known by its event and its place among
  // that event's lines (one reply can draw thinking, text and several tool lines).
  const seq = anchor ? anchor.dataset.seq : '';
  const nth = anchor ? lines.filter((line) => line.dataset.seq === seq).indexOf(anchor) : 0;
  const was = log.scrollTop;
  change();
  let target = anchor && anchor.isConnected ? anchor : null;
  if (!target && seq) target = [...log.querySelectorAll(LOG_LINES)].filter((line) => line.dataset.seq === seq)[nth] || null;
  if (!target && anchor) {
    // What was being read has dropped off the top: the line now first stands in its place.
    target = log.querySelector(LOG_LINES);
    if (target) log.scrollTop += target.getBoundingClientRect().top - log.getBoundingClientRect().top;
    return;
  }
  if (!target) { log.scrollTop = was; return; }
  log.scrollTop += target.getBoundingClientRect().top - log.getBoundingClientRect().top - offset;
}

// Reconcile, never rebuild, when the events only moved forward (D56).
//
// paintLog replaced every line on every poll, changed or not, and replacing a node discards any
// selection inside it - so a reader who selected a reply to copy it lost the selection within
// five seconds. Each line carries its event's seq, so the log can tell what it already shows:
// nothing new leaves the DOM alone; new events append only their own lines; and when the server's
// 500-event buffer slides, the lines of the events that fell off are removed rather than the
// whole log redrawn. Only a list that is not a continuation of what is drawn - another session,
// a reload - is drawn afresh.
// Replace a part of the page the poll repaints, but not needlessly and not under the user (D69).
//
// The 5 s poll rebuilt the session picker, Folder and Continue whether anything had changed or
// not, and replacing a select's options while it is open closes it - so a dropdown opened to
// choose a session shut itself within five seconds. Nothing changes when the new nodes are the
// same as the old; nothing changes while focus is inside, because an open dropdown has focus.
// What was skipped lands on the next tick after the user moves on.
function repaint(container, nodes) {
  const sig = nodes.map((n) => (n && n.outerHTML) || String(n)).join('');
  if (container.dataset.sig === sig) return false;
  if (container.contains(document.activeElement) && document.activeElement !== document.body) return false;
  container.replaceChildren(...nodes);
  container.dataset.sig = sig;
  return true;
}

function paintLog(log, events, key) {
  const list = events || [];
  const shown = log.drawnSeqs;
  const continues = shown && shown.key === key && list.length && list.every((e) => e.seq !== undefined)
    && list[0].seq >= shown.first && list.some((e) => e.seq === shown.last);
  if (continues) {
    const at = list.findIndex((e) => e.seq === shown.last);
    const fresh = list.slice(at + 1);
    if (fresh.length || list[0].seq !== shown.first) {
      keepingPlace(log, () => {
        // While earlier history is shown (feature 82), lines sliding off the live history stay, so
        // there is no gap under it; trimEarlier takes the oldest off instead.
        if (!log.earlierFrom) {
          for (const line of [...log.querySelectorAll(':scope > [data-seq]')]) {
            if (Number(line.dataset.seq) < list[0].seq) line.remove();
          }
        }
        const lines = transcript(fresh, { after: list[at], placeholder: false });
        // What arrived while the reader was up the page, for "View latest (3 new)" (43, part 2).
        if (!atBottom(log)) log.unseen = (log.unseen || 0) + lines.filter((l) => l.matches('.you, .claude')).length;
        // "Nothing yet" stood in for a log with no lines; the first real line replaces it.
        if (lines.length) for (const empty of log.querySelectorAll(':scope > p.muted')) empty.remove();
        log.append(...lines);
        trimEarlier(log, list);
      });
    }
  } else {
    // Another session, or a list that does not follow on (more arrived than the history holds):
    // the earlier lines no longer join what is drawn, so they go.
    if (log.head && (log.headKey !== key || log.earlierFrom)) clearEarlier(log);
    log.headKey = key;
    keepingPlace(log, () => log.replaceChildren(...(log.head ? [log.head] : []), ...transcript(list)));
  }
  log.drawnSeqs = list.length && list.every((e) => e.seq !== undefined)
    ? { key, first: list[0].seq, last: list[list.length - 1].seq } : null;
  // New content arriving while the reader is away from the bottom is exactly when the way back
  // is wanted; the scroll handler alone never fires, because nobody scrolled.
  const latest = document.getElementById('session-latest');
  if (latest) {
    if (atBottom(log)) log.unseen = 0;
    latest.hidden = atBottom(log);
    latest.textContent = log.unseen ? `View latest (${log.unseen} new)` : 'View latest';
  }
}

// "Show earlier" (feature 82, the user, 2026-10-06): up to 500 events before the live history, once.
// Never a new limit: as new events arrive the oldest lines go, until the log is back to the 500 the
// history keeps - no more than 1000 at any time. Further back than that is a job for Claude, reading
// the transcripts.
const EARLIER_MAX = 500;

function drawnEvents(log) {
  const seen = new Set();
  for (const line of log.querySelectorAll(LOG_LINES)) seen.add(line.dataset.seq);
  return [...seen];
}

// Oldest first: the earlier lines, then live lines that slid off while they were shown.
function trimEarlier(log, list) {
  if (!log.earlierFrom || !list.length) return;
  const newer = Math.max(0, list[list.length - 1].seq - log.earlierFrom);
  const allowed = Math.max(EARLIER_MAX, EARLIER_MAX * 2 - newer);
  const events = drawnEvents(log);
  for (const seq of events.slice(0, Math.max(0, events.length - allowed))) {
    for (const line of [...log.querySelectorAll(LOG_LINES)].filter((l) => l.dataset.seq === seq)) line.remove();
  }
  const earlierLeft = log.querySelector(':scope > .log-head .log-earlier > [data-seq]');
  const staleLeft = [...log.querySelectorAll(':scope > [data-seq]')].some((l) => Number(l.dataset.seq) < list[0].seq);
  if (!earlierLeft && !staleLeft) clearEarlier(log);
}

function clearEarlier(log) {
  log.earlierFrom = 0;
  const box = log.head && log.head.querySelector('.log-earlier');
  if (box) box.replaceChildren();
  if (log.head) log.head.dispatchEvent(new Event('earlier-changed'));
}

// What an interrupt did, in words. "Pending" is not failure: a session in the middle of a tool
// call reads its input late, and the request is already there - it will stop the turn when it
// does. Calling that an error, as the page used to, was how a turn came to stop dead a few
// seconds after the reader was told nothing had happened.
const INTERRUPT_TEXT = {
  interrupted: (event) => (event.late ? 'interrupted (the session was busy; it stopped when it next read its input)'
    : 'interrupted'),
  interrupt_pending: () => 'interrupt sent - the session is busy and will stop when it next reads its input',
  interrupt_refused: (event) => `interrupt refused: ${event.error || 'no reason given'}`,
};

// What a tool call is doing, not only its name (feature 68). One line - the input's `description`
// where there is one, since it is the field written to be read, else the command's first line or the
// file - and the whole input behind a <details>. Truncated on display, never on capture: the full
// input stays in the page for whoever opens it. Both go through withRestoredNames, as prose does, so
// a client name restored in a command is marked like one in a reply. A <details> holds its own
// open state, and D56's reconcile keeps the line from being rebuilt, so an opened one stays open.
const TOOL_LINE = 120;

function toolSummary(tool) {
  const input = tool.input || {};
  const first = (value) => String(value || '').split(/\r?\n/).find((l) => l.trim()) || '';
  const said = input.description || first(input.command) || input.file_path || input.path
    || input.pattern || input.url || first(input.prompt) || '';
  return said.length > TOOL_LINE ? `${said.slice(0, TOOL_LINE)}...` : said;
}

function toolDetail(tool) {
  const name = tool.name || 'tool';
  const input = tool.input || {};
  const summary = toolSummary(tool);
  if (!summary && !Object.keys(input).length) return h('div', {}, name);
  const full = typeof input.command === 'string' && Object.keys(input).length <= 3
    ? input.command
    : JSON.stringify(input, null, 2);
  return h('details', { class: 'tool-detail' },
    h('summary', {}, h('strong', {}, name), summary ? ' ' : '', ...withRestoredNames(summary)),
    h('pre', { class: 'mono' }, ...withRestoredNames(full)));
}

// dd/mm HH:MM in the reader's own time zone (feature 66), from epoch seconds.
function saidAt(at) {
  const d = new Date(at * 1000);
  const two = (n) => String(n).padStart(2, '0');
  return `${two(d.getDate())}/${two(d.getMonth() + 1)} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

// A permission answer, or - for a question - what was chosen, one question to a line, so it can be
// read again later (D80). It used to say only "allowed", which is true and tells you nothing.
function answeredLine(event) {
  const answers = event.answers && typeof event.answers === 'object' ? Object.entries(event.answers) : [];
  const body = answers.length
    ? answers.map(([question, answer]) => h('div', { class: 'answered' },
      h('span', { class: 'muted' }, ...withRestoredNames(question)), ' ', h('strong', {}, ...withRestoredNames(answer))))
    : [event.allow ? 'allowed' : 'denied'];
  if (event.late) body.push(h('div', { class: 'hint warn' }, 'after the question closed - Claude did not receive this'));
  return h('div', { class: 'turn tool' }, h('span', { class: 'who' }, 'you'), h('div', {}, ...body));
}

// `after` is the event just before these, when drawing only the new tail of a log (D56): the
// "resumed here" line depends on whether the previous event was replayed. `placeholder: false`
// gives an empty list rather than "Nothing yet" for a tail that draws no lines.
function transcript(events, { after = null, placeholder = true } = {}) {
  const lines = [];
  let wasReplayed = Boolean(after && after.replayed);
  for (const event of events || []) {
    const start = lines.length;
    // Where the conversation that was picked up ends and this sitting begins. Without it a
    // resumed session and a fresh one read identically.
    if (wasReplayed && !event.replayed) {
      lines.push(h('div', { class: 'turn tool resumed-here' },
        h('span', { class: 'who' }, ''), h('div', {}, 'resumed here')));
    }
    wasReplayed = Boolean(event.replayed);
    if (event.type === 'prompt') {
      lines.push(h('div', { class: 'turn you' }, h('span', { class: 'who' }, 'you'), h('div', {}, event.text)));
    } else if (event.type === 'assistant') {
      // Claude's reasoning, collapsed above the reply (the user, 2026-10-01): open it to see how a
      // request is being read and steer early. Summarised by the CLI (--thinking-display); an empty
      // block - what arrives without it - draws nothing. Never rehydrated (D8), so a client name in
      // it shows as its token.
      const thoughts = ((event.message || {}).content || [])
        .filter((c) => c.type === 'thinking' && String(c.thinking || '').trim()).map((c) => c.thinking);
      if (thoughts.length) {
        lines.push(h('div', { class: 'turn tool thinking-turn' }, h('span', { class: 'who' }, ''),
          h('details', { class: 'thinking' }, h('summary', {}, 'thinking'),
            h('div', { class: 'thinking-text' }, thoughts.join('\n\n')))));
      }
      const parts = ((event.message || {}).content || []).filter((c) => c.type === 'text').map((c) => c.text);
      if (parts.length) {
        lines.push(h('div', { class: 'turn claude' }, h('span', { class: 'who' }, 'claude'),
          replyMarkdown(parts.join('\n'))));
      }
      const tools = ((event.message || {}).content || []).filter((c) => c.type === 'tool_use');
      for (const tool of tools) {
        // The dot says whether the call is still running (D176); markTools sets it, on every
        // paint, because a result arrives after the line was drawn.
        const dot = h('span', { class: 'tool-dot', 'data-tool-id': tool.id || '', 'aria-hidden': 'true' });
        lines.push(h('div', { class: 'turn tool' }, h('span', { class: 'who' }, dot, 'tool'), toolDetail(tool)));
      }
    } else if (event.type === 'brief') {
      lines.push(h('div', { class: 'turn tool' }, h('span', { class: 'who' }, 'brief'), h('div', {}, event.text)));
    } else if (event.type === 'permission_answered') {
      lines.push(answeredLine(event));
    } else if (INTERRUPT_TEXT[event.type]) {
      // In the transcript, not only in a toast: a turn that stops part-way has to say why where
      // the reader is looking, or it reads as a dropped connection.
      lines.push(h('div', { class: 'turn tool' }, h('span', { class: 'who' }, 'session'),
        h('div', {}, INTERRUPT_TEXT[event.type](event))));
    } else if (event.type === 'exited' || event.type === 'stopped') {
      lines.push(h('div', { class: 'turn tool' }, h('span', { class: 'who' }, 'session'),
        h('div', {}, event.type === 'stopped' ? 'stopped' : `exited (${event.code})`)));
    }
    // Each line says which event drew it, so paintLog can tell what the log already shows.
    if (event.seq !== undefined) for (const line of lines.slice(start)) line.dataset.seq = String(event.seq);
    // When it was said, under the who (feature 66) - the event's own time, so a resumed
    // conversation shows yesterday as yesterday.
    if (event.at) {
      for (const line of lines.slice(start)) {
        const who = line.querySelector('.who');
        if (who && who.textContent) who.append(h('span', { class: 'when' }, saidAt(event.at)));
      }
    }
  }
  if (lines.length || !placeholder) return lines;
  return [h('p', { class: 'muted' }, 'Nothing yet. Start this session and ask it something.')];
}

// What was started in the background (feature 49).
//
// A session can start background shells and monitors and the page showed no sign of them: they
// could not be seen, counted or referred to, so "your shell command looks like it might have
// stalled" was not a question that could be asked, because there was nothing on screen to ask it
// about. The material was arriving and being thinned to a word - `transcript()` renders a tool as
// its bare name and drops the `input` that says whether it was backgrounded.
//
// Built from the completed `assistant` events the transcript already has, not from the streaming
// fragments, so it survives a reload and a resumed conversation.
//
// **It says "started", not "running".** Knowing a background job has finished would mean tracking
// tool results and completion notices, and claiming "running" for something that exited ten
// minutes ago would be worse than saying less. The elapsed time is from when it started.
const BACKGROUND_TOOLS = new Set(['Monitor']);

// The six `claude --permission-mode` accepts, said in words rather than in its flag names: the
// page has to answer "will this ask me?", and `dontAsk` answers it while `acceptEdits` does not.
const PERMISSION_MODES = [
  ['manual', 'Ask every time'],
  ['auto', 'Judge each action (sent to Anthropic)'],
  ['acceptEdits', 'Accept file edits, ask for the rest'],
  ['dontAsk', 'Do not ask'],
  ['bypassPermissions', 'Skip all permission checks'],
  ['plan', 'Plan only, change nothing'],
];
const MODE_WHY = {
  manual: 'Every command waits for you.',
  auto: 'Each action and its context go to Anthropic to be judged.',
  acceptEdits: 'File edits go through; commands still ask.',
  dontAsk: 'Nothing waits for you.',
  bypassPermissions: 'No checks at all. Least safe.',
  plan: 'It can read and plan, but not act.',
};

// A job is finished when a completion notice names the tool_use it belongs to. The notice hands
// over the id rather than making us infer it, so this is a join and not a guess:
//
//     <task-notification><task-id>b3wi7o8vm</task-id>
//     <tool-use-id>toolu_01Dt5M...</tool-use-id><status>completed</status>
//
// Matched loosely on purpose. The wrapper is the harness's to change, and a notice we fail to
// recognise must not resurrect a job that ended an hour ago - which is the whole fault (D54).
const FINISHED_ID = /tool[-_]use[-_]id["'>\s:]+(toolu_[A-Za-z0-9]+)/gi;

// Nothing is called "started" forever. If a notice never arrives - consumed inside the CLI, lost
// across a reload, a shape nobody anticipated - the panel still has to stop claiming the job is
// under way. Eleven stale rows is what this defect looked like in use, and a wrong count is worse
// than a missing one.
const STARTED_GOES_STALE_AFTER = 30 * 60;

// Under stream-json (the Session page's own sessions) the notice is not text at all but its own
// event, `{type:'system', subtype:'task_notification', tool_use_id}` - which the loop below never
// read, so nothing was ever seen to finish (D79). The same stream also sends
// `background_tasks_changed` with the live list by task id, and `task_started` joins a task id to
// its tool_use: a job the latest list no longer names is over, even if its notice was missed.
function finishedIds(events) {
  const done = new Set();
  const toolOfTask = new Map();
  for (const event of events || []) {
    if (event.type === 'system') {
      if (event.subtype === 'task_started' && event.task_id && event.tool_use_id) {
        toolOfTask.set(event.task_id, event.tool_use_id);
      } else if (event.subtype === 'task_notification' && event.tool_use_id) {
        done.add(event.tool_use_id);
      } else if (event.subtype === 'background_tasks_changed' && Array.isArray(event.tasks)) {
        const live = new Set(event.tasks.map((task) => task && task.task_id));
        for (const [taskId, toolId] of toolOfTask) if (!live.has(taskId)) done.add(toolId);
      }
      continue;
    }
    if (event.type !== 'user') continue;
    for (const block of ((event.message || {}).content || [])) {
      if (!block || typeof block !== 'object') continue;
      // A notice arrives as text; a tool_result carries its own id in a field. Both are read,
      // because which one a completion comes back as is not ours to decide.
      const body = block.type === 'text' ? String(block.text || '')
        : typeof block.content === 'string' ? block.content : '';
      for (const hit of body.matchAll(FINISHED_ID)) done.add(hit[1]);
    }
  }
  return done;
}

// Each tool call's state for its dot (D176): "done" once a result names it, "stopped" when the turn
// it belonged to ended without one (an interrupt, an exit) or the session is not running, and
// "running" otherwise. Several calls run at once, so each is answered for itself.
const TURN_ENDS = new Set(['result', 'interrupted', 'exited', 'stopped']);

function toolStates(events, running) {
  const list = events || [];
  const done = finishedIds(list);
  for (const event of list) {
    if (event.type !== 'user') continue;
    for (const block of ((event.message || {}).content || [])) {
      if (block && block.type === 'tool_result' && block.tool_use_id) done.add(block.tool_use_id);
    }
  }
  let lastEnd = -1;
  list.forEach((event, at) => { if (TURN_ENDS.has(event.type)) lastEnd = at; });
  const states = new Map();
  list.forEach((event, at) => {
    if (event.type !== 'assistant') return;
    for (const block of ((event.message || {}).content || [])) {
      if (block.type !== 'tool_use' || !block.id) continue;
      if (done.has(block.id)) states.set(block.id, 'done');
      else states.set(block.id, !running || at < lastEnd ? 'stopped' : 'running');
    }
  });
  return states;
}

function markTools(log, events, running) {
  const states = toolStates(events, running);
  for (const dot of log.querySelectorAll('.tool-dot[data-tool-id]')) {
    const state = states.get(dot.dataset.toolId) || 'done';
    dot.dataset.state = state;
    dot.title = state === 'running' ? 'still running' : state === 'stopped' ? 'stopped before it finished' : 'finished';
  }
}

// Prompts waiting for the running turn to end (D176): above the prompt box, not in the transcript,
// until the session takes them. Held ones came back from before a restart and go only when sent.
function queueChips(queued) {
  const items = queued || [];
  if (!items.length) return [];
  const head = h('div', { class: 'hint queue-head' },
    `${items.length} waiting - sent when the current turn ends, one at a time`);
  const chips = items.map((item) => h('div', { class: `queue-chip${item.held ? ' held' : ''}`, 'data-id': item.id },
    h('span', { class: 'queue-text', title: item.text }, item.text.length > 140 ? `${item.text.slice(0, 140)}...` : item.text),
    item.held ? h('span', { class: 'hint' }, ' (held after a restart)') : null,
    h('button', { type: 'button', class: 'queue-send', 'data-action': 'send', 'data-id': item.id }, 'Send now'),
    h('button', { type: 'button', class: 'queue-cancel', 'data-action': 'cancel', 'data-id': item.id }, 'Cancel')));
  return [head, ...chips];
}

function backgroundTasks(events) {
  const found = new Map();
  for (const event of events || []) {
    if (event.type !== 'assistant') continue;
    for (const block of ((event.message || {}).content || [])) {
      if (block.type !== 'tool_use' || !block.id) continue;
      const input = block.input || {};
      if (input.run_in_background !== true && !BACKGROUND_TOOLS.has(block.name)) continue;
      found.set(block.id, {
        id: block.id,
        name: block.name || 'tool',
        label: String(input.description || input.command || input.prompt || '').trim(),
        at: Number(event.at) || 0,
      });
    }
  }
  const done = finishedIds(events);
  const now = Date.now() / 1000;
  return [...found.values()].filter((task) => {
    if (done.has(task.id)) return false;
    return !task.at || (now - task.at) < STARTED_GOES_STALE_AFTER;
  });
}

// ---- what the page already holds (D140) --------------------------------------------------------

// As many events as the server keeps for a session: a merged list is trimmed to the same.
const KEPT_ON_PAGE = 500;

// The events held plus the newer ones the server sent, once each, in order.
function mergeEvents(held, newer) {
  const bySeq = new Map();
  for (const event of [...(held || []), ...(newer || [])]) {
    if (typeof event.seq === 'number') bySeq.set(event.seq, event);
  }
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq).slice(-KEPT_ON_PAGE);
}

function sinceText(at) {
  if (!at) return '';
  const seconds = Math.max(0, Date.now() / 1000 - at);
  if (seconds < 90) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

function paintBackground(panel, events, box) {
  const running = backgroundTasks(events);
  panel.hidden = !running.length;
  if (!running.length) {
    panel.replaceChildren();
    return;
  }
  panel.replaceChildren(
    h('div', { class: 'bg-head' }, `Tools currently running in the background (${running.length})`),
    ...running.map((task) => h('div', { class: 'bg-row' },
      h('span', { class: 'bg-name' }, task.name),
      // A tool input is a command line or a path, which is exactly the sort of string that
      // carries a client name - so it goes through the same restore the transcript uses. Note
      // that a tool input is rehydrated but never MARKED (an escape sequence inside a command
      // line is a bug), so expect real names here in plain text rather than in green.
      h('span', { class: 'bg-what' }, ...withRestoredNames(task.label || '(no description)')),
      h('span', { class: 'bg-since' }, sinceText(task.at)),
      h('button', {
        type: 'button', class: 'bg-insert',
        'aria-label': `Put ${task.label || task.name} in the prompt`,
        title: 'Put this in the prompt, so you can ask about it',
      }, h('span', { 'aria-hidden': 'true' }, '→')))));
  panel.onclick = (event) => {
    const button = event.target.closest('button.bg-insert');
    if (!button || !box) return;
    const row = button.closest('.bg-row');
    const what = row ? row.querySelector('.bg-what').textContent : '';
    if (what) insertAtCursor(box, what);
  };
}

// The file tree. Half of driving a session is saying where to look, and a phone has no terminal
// beside it to run `ls` in. Names only: nothing here reads a file.
const tree = { open: new Map(), separator: '\\', cache: new Map(), menu: null, searchOpen: false, query: '', found: null };

function joinPath(root, relative, name, separator) {
  const parts = [root];
  if (relative) parts.push(relative.split('/').join(separator));
  if (name) parts.push(name);
  return parts.join(separator).replace(new RegExp(`\\${separator}{2,}`, 'g'), separator);
}

function insertAtCursor(box, text) {
  // At the cursor, not at the end: a path usually belongs in the middle of a sentence.
  const at = box.selectionStart === null ? box.value.length : box.selectionStart;
  const to = box.selectionEnd === null ? at : box.selectionEnd;
  const spaced = (at > 0 && !/\s$/.test(box.value.slice(0, at))) ? ' ' + text : text;
  box.value = box.value.slice(0, at) + spaced + box.value.slice(to);
  const after = at + spaced.length;
  box.focus();
  box.setSelectionRange(after, after);
}

// The open/close triangle, a button of its own beside the name (the user, 2026-10-01). Inside the
// name button it also opened the actions menu - one tap, two effects. A file gets a blank space of
// the same width so names line up.
function treeToggle(name, root, relative, open) {
  return h('button', { type: 'button', class: 'tree-toggle', 'aria-expanded': String(open),
                       'aria-label': `${open ? 'Close' : 'Open'} ${name}`, dataset: { root, path: relative } },
    h('span', { class: 'tree-chev', 'aria-hidden': 'true' }, open ? '▾' : '▸'));
}

function entryRow(listing, entry, depth) {
  const full = joinPath(listing.root, listing.path, entry.name, listing.separator);
  const relative = listing.path ? `${listing.path}/${entry.name}` : entry.name;
  // A folder and a file each get their own mark (feature 39's last piece).
  //
  // The triangle used to do two jobs: it said "this is a folder" AND "this folder is open" -
  // one glyph, two meanings, which is D21's fault in the file tree. With a folder mark of its
  // own, the triangle goes back to meaning only open or closed, and a file stops being "the row
  // with no triangle".
  //
  // aria-hidden, and outside the name: concatenated into the text, a screen reader announced the
  // triangle before the filename and dragging to select a name picked the glyph up with it.
  // A folder that cannot be read, or a link out of the project, is shown and marked but cannot be
  // opened or used (the user, 2026-10-06: clicking .pytest_cache gave "Access is denied").
  const blocked = Boolean(entry.error);
  const open = !blocked && tree.open.has(`${listing.root}|${relative}`);
  const mark = h('span', { class: 'tree-mark', 'aria-hidden': 'true' },
    entry.dir ? (open ? '\uD83D\uDCC2' : '\uD83D\uDCC1') : '\uD83D\uDCC4');
  // What the row stands for, for acting on several at once (feature 97): its folder's listing is
  // its `parent`, which is what a Shift range stays within.
  const about = { full, root: listing.root, path: relative, name: entry.name, parent: `${listing.root}|${listing.path || ''}`,
    kind: entry.dir ? 'dir' : 'file', view: entry.dir ? '' : (entry.view || 'download') };
  const label = h('button', { type: 'button', class: `tree-name ${entry.dir ? 'dir' : 'file'}`, dataset: about },
    mark, h('span', { class: 'tree-text' }, entry.name));
  if (isSelected(full)) label.classList.add('selected');
  if (blocked) {
    label.disabled = true;
    label.classList.add('blocked');
    label.title = entry.error === 'unreadable' ? `${entry.name} cannot be read` : `${entry.name}: ${entry.error}`;
  }
  // The indent is a data attribute, not a style attribute: the page's CSP forbids inline styles,
  // which is what stops an injected string from carrying its own presentation.
  //
  // The actions open under the name when it is tapped or clicked (the user, 2026-09-30). A column
  // on the right - always there, then on hover - covered the end of long names with its background,
  // and hover does not exist on a phone. Tapping works the same with a finger and with a mouse.
  return h('div', { class: 'tree-row', dataset: { depth: String(Math.min(depth, 6)) } },
    entry.dir && !blocked ? treeToggle(entry.name, listing.root, relative, open) : h('span', { class: 'tree-toggle-space', 'aria-hidden': 'true' },
      h('span', { class: 'tree-chev' }, '')),
    label,
    entry.error ? h('span', { class: 'chip warn' }, entry.error) : null,
    tree.menu === full ? treeMenu(entry.name, full,
      entry.dir ? null : { root: listing.root, path: relative, view: entry.view || 'download' }, about) : null);
}

// What to do with a file or folder, shown under its name and over the row below it (DECISIONS 50).
// A folder: Prompt. A file a tab can show: Prompt, View, Download. A download-only file (Word,
// PowerPoint, anything a tab cannot show): Prompt, and Download in View's place - no View at all.
function treeMenu(name, full, file, about = null) {
  // Opened where the name was tapped (D143) - set from script, which the page's policy allows.
  const placed = (menu) => { if (tree.menuAt) menu.style.left = `${Math.round(tree.menuAt)}px`; return menu; };
  // Several selected, or a folder: the group menu - Prompt, View, Download - for all of them (feature 97).
  if (about) {
    const self = { ...about, dir: about.kind === 'dir' };
    const items = menuItems(self);
    if (items.length > 1 || self.dir) return placed(groupMenu(items, ownFiles, isSealed()));
  }
  const prompt = h('button', { type: 'button', class: 'tree-insert', role: 'menuitem', 'aria-label': `Put ${name} in the prompt`,
                  title: 'Put this path in the prompt', dataset: { insert: full } },
    h('span', { 'aria-hidden': 'true' }, '\u2192'), ' Prompt');
  if (!file) return placed(h('div', { class: 'tree-menu', role: 'menu' }, prompt));
  const download = downloadLink(view.name, file.root, file.path, '', {
    role: 'menuitem', class: 'tree-download', 'aria-label': `Download ${name}`, title: 'Download this file' });
  download.append(h('span', { 'aria-hidden': 'true' }, '\u2b07'), ' Download');
  if (file.view === 'download') return placed(h('div', { class: 'tree-menu', role: 'menu' }, prompt, download));
  const open = h('button', { type: 'button', class: 'tree-view', role: 'menuitem', 'aria-label': `Open ${name} in a tab`,
    title: 'Open this file in a tab', dataset: { root: file.root, path: file.path, name, view: file.view } },
  h('span', { 'aria-hidden': 'true' }, '\ud83d\udc41'), ' View');
  return placed(h('div', { class: 'tree-menu', role: 'menu' }, prompt, open, download));
}

// The menu opens below and right of the tap (D143), but one tapped near the end of a name ran past
// the panel's right edge and widened its scrollbar (D150). Once drawn and measurable, it moves left
// just enough to sit inside the visible part of the panel - never past its left edge.
function keepMenuInView(panel) {
  const menu = panel.querySelector('.tree-menu');
  if (!menu) return;
  let scroller = menu.parentElement;
  while (scroller && scroller !== document.body && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowX)) {
    scroller = scroller.parentElement;
  }
  if (!scroller || scroller === document.body) scroller = panel;
  const box = scroller.getBoundingClientRect();
  const visibleLeft = box.left + scroller.clientLeft;
  const visibleRight = visibleLeft + scroller.clientWidth;
  const rect = menu.getBoundingClientRect();
  const over = rect.right - visibleRight + 4;
  if (over <= 0) return;
  const shift = Math.min(over, Math.max(0, rect.left - visibleLeft));
  menu.style.left = `${Math.round(menu.offsetLeft - shift)}px`;
}

// One folder's listing, kept until something says it changed.
//
// Nothing was kept before, so every expand AND every collapse rebuilt the whole visible tree:
// showTree walked from the roots and re-fetched the share, each root, and every folder still
// open. With six folders expanded, opening a seventh cost about eight requests, seven of which
// returned exactly what they had a moment earlier - and each one blocked the event loop and
// re-resolved the project allow-list on the way in (D32). Collapsing, which strictly removes
// information and needs no request at all, cost the same.
async function listing(name, root, relative) {
  const key = `${name}|${root}|${relative || ''}`;
  const known = tree.cache.get(key);
  if (known) return known;
  const query = new URLSearchParams({ name, root, path: relative || '' });
  const got = await getJSON(`/api/session/files?${query}`);
  tree.cache.set(key, got);
  return got;
}

function forgetListings() {
  tree.cache.clear();
}

// A folder's own files as tree items, from its listing if it has been read; null until then.
function ownFiles(folder) {
  const found = tree.cache.get(`${view.name}|${folder.root}|${folder.path || ''}`);
  if (!found) return null;
  return found.entries.filter((e) => !e.dir && !e.error).map((e) => ({
    full: joinPath(found.root, found.path, e.name, found.separator), root: found.root,
    path: found.path ? `${found.path}/${e.name}` : e.name, name: e.name, dir: false, view: e.view || 'download' }));
}

// The folders an open group menu needs listed to count their files: read, then the tree redrawn.
async function listMenuFolders(panel) {
  const menu = panel.querySelector('.tree-menu.group');
  const own = menu && menu.closest('.tree-row, .tree-root');
  const button = own && own.querySelector('button.tree-name');
  if (!button) return false;
  const missing = menuItems(itemOf(button)).filter((i) => i.dir && ownFiles(i) === null);
  if (!missing.length) return false;
  await Promise.all(missing.map((i) => listing(view.name, i.root, i.path).catch(() => {
    tree.cache.set(`${view.name}|${i.root}|${i.path || ''}`, { root: i.root, path: i.path, separator: tree.separator, entries: [] });
  })));
  return true;
}

async function treeRows(name, root, relative, depth) {
  const found = await listing(name, root, relative);
  tree.separator = found.separator;
  const rows = [];
  for (const entry of found.entries) {
    rows.push(entryRow(found, entry, depth));
    const childRelative = found.path ? `${found.path}/${entry.name}` : entry.name;
    const key = `${root}|${childRelative}`;
    // Remembered as open but now unreadable: forgotten, so it is not asked for again on every redraw.
    if (entry.dir && entry.error && tree.open.has(key)) { tree.open.delete(key); tree.forgotten = true; }
    if (entry.dir && !entry.error && tree.open.has(key)) {
      // A folder that cannot be listed shows why on its own row and is no longer remembered as
      // open (D171): it used to take its whole project down, and Refresh asked for it again and
      // failed again, every time.
      try {
        rows.push(...await treeRows(name, root, childRelative, depth + 1));
      } catch (error) {
        tree.open.delete(key);
        tree.forgotten = true;
        rows.push(h('div', { class: 'tree-row tree-error', dataset: { depth: String(Math.min(depth + 1, 6)) } },
          h('span', { class: 'chip warn' }, String(error.message || error))));
      }
    }
  }
  return rows;
}

// A root heading is a folder like any other (D70): collapsed until opened, through the same
// toggle and the same open-set key (`root|` with an empty path), so D73's remembered state covers
// roots as well as what is inside them. It used to draw its first level unconditionally, so the
// list of roots was buried under the contents of the first one.
function rootRow(label, path, extraClass) {
  const open = tree.open.has(`${path}|`);
  const about = { path: '', root: path, full: path, name: label, parent: '#roots', kind: 'dir', view: '' };
  return h('div', { class: `tree-root${extraClass ? ` ${extraClass}` : ''}` },
    treeToggle(label, path, '', open),
    h('button', { type: 'button', class: `tree-name dir tree-root-name${isSelected(path) ? ' selected' : ''}`,
                  'aria-expanded': String(open), dataset: about },
      h('span', { class: 'tree-text' }, label)),
    tree.menu === path ? treeMenu(label, path, null, about) : null);
}

async function rootRows(name, root) {
  try {
    return await treeRows(name, root, '', 1);
  } catch (error) {
    return [h('p', { class: 'muted' }, String(error.message || error))];
  }
}

// ---- searching the tree by name (feature 90) --------------------------------------------------

// The magnifying glass beside "Files": opens the search, or closes it and puts the tree back.
// "Files" and "Search" as one pair of buttons (the user, 2026-10-08): the highlighted one is the
// view showing - the whole tree, or the name search - and the other switches to it.
function viewSwitch(panel, name) {
  const show = (search) => {
    if (Boolean(tree.searchOpen) === search) return;
    tree.searchOpen = search;
    // The query is kept (D145): back on Search it is put back and run again, so the results are
    // the folders as they are now, not as they were when last searched.
    tree.found = null;
    tree.menu = null;
    if (search) renderSearch(panel, name, true);
    else if (tree.redraw) tree.redraw();
  };
  const files = h('button', { type: 'button', class: 'btn tree-view-files', 'aria-pressed': String(!tree.searchOpen),
    title: 'Every folder of this project' }, 'Files');
  const search = h('button', { type: 'button', class: 'btn tree-view-search', 'aria-pressed': String(Boolean(tree.searchOpen)),
    title: 'Search files and folders by name' }, h('span', { 'aria-hidden': 'true' }, '🔍'), ' Search');
  files.addEventListener('click', () => show(false));
  search.addEventListener('click', () => show(true));
  return h('span', { class: 'md-switch tree-view-switch', role: 'group', 'aria-label': 'Files view' }, files, search);
}

function searchRow(result) {
  const separator = /^[A-Za-z]:/.test(result.root) ? '\\' : '/';
  const full = `${result.root.replace(/[\\/]+$/, '')}${separator}${result.path.split('/').join(separator)}`;
  const mark = h('span', { class: 'tree-mark', 'aria-hidden': 'true' }, result.dir ? '📁' : '📄');
  const label = h('button', { type: 'button', class: `tree-name ${result.dir ? 'dir' : 'file'}`,
    dataset: result.dir ? { full, root: result.root, path: result.path } : { full } },
  mark, h('span', { class: 'tree-text' }, full));
  return h('div', { class: 'tree-row search-hit', dataset: { depth: '0' } },
    h('span', { class: 'tree-toggle-space', 'aria-hidden': 'true' }, h('span', { class: 'tree-chev' }, '')), label,
    tree.menu === full ? treeMenu(result.name, full,
      result.dir ? null : { root: result.root, path: result.path, view: result.view || 'download' }) : null);
}

// What the search did, as nodes (D146): a search that stopped early leads with why, in bold, at a
// larger size than the ordinary count - the two reasons call for different next steps.
function searchStatus(found, keepSearching) {
  if (!found) return [];
  const count = found.results.length;
  const skipped = found.skipped ? ` ${found.skipped} folder(s) skipped (installed dependencies or unreadable).` : '';
  if (found.stopped === 'limit') {
    return [h('span', { class: 'search-stop' }, h('b', {}, 'Result Limit Reached: '),
      `the first ${count} matches are shown. Type more of the name to narrow it.`), skipped];
  }
  if (found.stopped === 'time') {
    return [h('span', { class: 'search-stop' }, h('b', {}, 'Search Timeout Reached: '),
      `${count} found in 30 seconds, but not every folder has been searched yet. `,
      found.more ? keepSearching : null), skipped];
  }
  return [count ? `${count} found.` : 'Nothing found.', skipped];
}

// The search pane replaces the tree while it is open. Built once and then only its results are
// redrawn, so the box keeps its focus and caret through the page's regular tree redraws.
function renderSearch(panel, name, focus = false) {
  let pane = panel.querySelector(':scope > .tree-search-pane');
  if (!pane || pane.dataset.session !== name) {
    const box = h('input', { type: 'search', class: 'tree-search-box', placeholder: 'Part of a file or folder name',
      'aria-label': 'Search files and folders by name', value: tree.query || '', 'data-unsaved-ignore': '' });
    const status = h('p', { class: 'hint tree-search-status', role: 'status' });
    const hits = h('div', { class: 'tree-search-hits' });
    // Search again from the start (D146) - after files changed, or to try a search that ran out of
    // time with the folders now in the disk cache.
    const again = h('button', { type: 'button', class: 'tree-refresh tree-search-again', title: 'Search again from the start' }, 'Refresh');
    pane = h('div', { class: 'tree-search-pane', dataset: { session: name } },
      h('div', { class: 'form-row tree-head' }, viewSwitch(panel, name), again), box, status, hits);
    let timer = null;
    let asked = 0;
    // `more`: carry on where a search that ran out of time stopped, adding to what is shown.
    const run = async (more = false) => {
      const query = box.value.trim();
      const mine = ++asked;
      if (query.length < 2) { tree.found = null; renderSearch(panel, name); return; }
      status.replaceChildren('Searching...');
      status.classList.add('searching');
      try {
        const found = await getJSON(`/api/session/search?name=${encodeURIComponent(name)}&q=${encodeURIComponent(query)}${more ? '&more=1' : ''}`);
        if (mine === asked) {
          if (more && found.continued && tree.found) found.results = [...tree.found.results, ...found.results];
          tree.found = found;
          tree.menu = null;
        }
      } catch (error) {
        if (mine === asked) { tree.found = null; sayError(error); }
      }
      if (mine === asked) { status.classList.remove('searching'); renderSearch(panel, name); }
    };
    pane.searchMore = () => run(true);
    again.addEventListener('click', () => { clearTimeout(timer); tree.query = box.value; run(false); });
    box.addEventListener('input', () => {
      clearTimeout(timer);
      tree.query = box.value;
      timer = setTimeout(() => run(false), 300);
    });
    panel.replaceChildren(pane);
  }
  const found = tree.found;
  const keepSearching = h('button', { type: 'button', class: 'tree-search-more' }, 'Keep searching');
  keepSearching.addEventListener('click', () => pane.searchMore());
  const statusLine = pane.querySelector('.tree-search-status');
  statusLine.classList.toggle('stopped', Boolean(found && found.stopped));
  statusLine.replaceChildren(...((tree.query || '').trim().length < 2
    ? ['Type at least 2 characters.'] : searchStatus(found, keepSearching)).filter(Boolean));
  pane.querySelector('.tree-search-hits').replaceChildren(...(found ? found.results.map(searchRow) : []));
  if (focus) {
    const box = pane.querySelector('.tree-search-box');
    box.focus();
    // Opened with a query kept from before (D145): run it again.
    if ((tree.query || '').trim().length >= 2 && !found) box.dispatchEvent(new Event('input'));
  }
}

async function renderTree(panel, name, roots, current, share) {
  if (tree.searchOpen) { renderSearch(panel, name); return; }
  // Each root paints as its own listing arrives, and they are fetched together rather than one
  // after another. Awaiting the whole set before calling setChildren meant the pane showed its
  // loading line until the LAST folder answered - and the share, which is a OneDrive folder and
  // the slowest thing in the list, was fetched first, so the whole tree waited on it (D27).
  //
  // The headings go up immediately, in their final order, and each one's contents fill in
  // underneath. Nothing moves once it has appeared.
  const again = h('button', { type: 'button', class: 'tree-refresh', title: 'Look at the folders again' }, 'Refresh');
  again.addEventListener('click', () => { forgetListings(); renderTree(panel, name, roots, current, share); });
  const head = [
    h('div', { class: 'form-row tree-head' }, viewSwitch(panel, name), again),
    // Listings are remembered until this says otherwise, so a file written while the session runs
    // is not noticed on its own (D33). Refresh is how you ask.
    h('p', { class: 'hint' }, 'Click (or right-click) a file or folder to view / download / put the path in the prompt. Shift+click selects a range in a folder; Ctrl+click adds or removes one.'),
  ];
  const sections = [];
  // One entry per folder (DECISIONS 64): the share was also listed again when it was one of the
  // profile's folders, and the two shared their open/closed state - opening one opened both. It is
  // listed once, as the share, saying when it is also where sessions start.
  const same = (a, b) => String(a || '').replace(/[\\/]+$/, '').toLowerCase()
    === String(b || '').replace(/[\\/]+$/, '').toLowerCase();
  if (share) {
    const shareLabel = same(share, current) ? `${share} (this session)` : share;
    sections.push({ head: rootRow(shareLabel, share, 'share'), root: share });
  } else {
    sections.push({ head: h('div', { class: 'tree-root share none' }, 'No OneDrive Share Location Configured') });
  }
  const seen = share ? [share] : [];
  for (const root of roots) {
    if (seen.some((known) => same(known, root))) continue;
    seen.push(root);
    sections.push({ head: rootRow(same(root, current) ? `${root} (this session)` : root, root), root });
  }

  // A redraw keeps what each open folder showed until its new listing lands (the user, 2026-10-01).
  // The tree is redrawn at the end of every turn (D33), and empty bodies put up first made the
  // whole tree vanish and come back each time - the "flicker every few minutes".
  const previous = new Map([...panel.querySelectorAll(':scope > .tree-branch')]
    .map((body) => [body.dataset.root, body]));
  // Where the list was scrolled to, put back once it is rebuilt (D104). Every click on a file or
  // folder redraws the tree, and replacing every child reset the panel to the top - so the row
  // just clicked, and its menu, disappeared off the bottom. Put back twice: after the rebuild, and
  // after the listings land, unless the reader has scrolled meanwhile.
  const scrolled = panel.scrollTop;
  const putBack = () => { if (panel.scrollTop !== scrolled) panel.scrollTop = scrolled; };
  const bodies = sections.map((section) => {
    const body = h('div', { class: 'tree-branch', dataset: { root: section.root || '' } });
    const old = section.root && tree.open.has(`${section.root}|`) ? previous.get(section.root) : null;
    if (old) body.append(...old.childNodes);
    return body;
  });
  setChildren(panel, ...head,
    ...sections.flatMap((section, i) => [section.head, bodies[i]]),
    roots.length ? null : h('p', { class: 'muted' }, 'This project profile has no repo folders yet.'));
  putBack();
  let moved = false;
  const watchScroll = () => { moved = true; };
  // Armed a frame later: the scroll event putBack itself raises is not the reader moving.
  requestAnimationFrame(() => requestAnimationFrame(() =>
    panel.addEventListener('scroll', watchScroll, { once: true, passive: true })));

  await Promise.all(sections.map(async (section, i) => {
    if (!section.root || !tree.open.has(`${section.root}|`)) return;   // collapsed: nothing to fetch
    bodies[i].replaceChildren(...await rootRows(name, section.root));
  }));
  panel.removeEventListener('scroll', watchScroll);
  if (!moved) putBack();
}

// ---- the sessions down the side ---------------------------------------------------------------

// A running session, as a button (feature 53). No state chip: membership of this list IS the
// state, which is the whole point - three stopped sessions used to say "stopped" three times,
// the least informative thing on the page taking a third of its rows.
function sessionRow(profile, state) {
  const session = (state.sessions || {})[profile.name] || {};
  const waiting = (session.pending || []).length;
  return h('button', {
    type: 'button',
    class: `session-row${profile.name === view.name ? ' selected' : ''}`,
    dataset: { session: profile.name },
    'aria-pressed': profile.name === view.name ? 'true' : 'false',
  },
    h('span', { class: 'session-name' }, profile.name),
    profile.label && profile.label !== profile.name ? h('span', { class: 'chip' }, profile.label) : null,
    // The one chip that survives: a session paused on a permission prompt is the only thing on
    // this page that needs answering, and it can only ever happen to a running one.
    waiting ? h('span', { class: 'chip warn' }, `${waiting} waiting`) : null);
}

// A picker and the running ones, taskbar-style (feature 53, decided 2026-09-27).
//
// Every profile used to get a full row carrying a state chip. Now: a dropdown of all of them
// with Start beside it, and the running ones as buttons underneath.
//
// **No description under the picker, deliberately.** This is the user's own environment and they
// know what each client name means; and the detail already appears at the top of the page above
// the conversation when a session is switched to. Repeating it here would say the same thing
// twice on the page with the least room.
//
// There is no "selected but not running" state to place, which is what the entry was stuck on:
// a session is either the thing in the dropdown, or a button because it is running.
const PICK_PROMPT = 'Pick a Project to chat about...';

const LAST_SESSION = 'aicleaner.session.last';

function rememberedSession() {
  try { return localStorage.getItem(LAST_SESSION) || ''; } catch (error) { return ''; }
}

function rememberSession(name) {
  if (!name) return;
  try { localStorage.setItem(LAST_SESSION, name); } catch (error) { /* not remembered */ }
}

// The address bar says which session is shown, without a navigation (D118). The router is told,
// so it does not take the new address for one it still has to route to.
function noteAddress(hash) {
  if (location.hash === hash) return;
  history.replaceState(null, '', hash);
  window.dispatchEvent(new CustomEvent('aicleaner:address', { detail: hash }));
}

function sessionList(state) {
  const profiles = state.profiles || [];
  const runningNames = new Set(state.running || []);
  const active = profiles.filter((p) => runningNames.has(p.name));
  const picker = h('select', { id: 'session-pick', 'aria-label': PICK_PROMPT },
    h('option', { value: '' }, profiles.length ? PICK_PROMPT : 'No project profiles yet'),
    ...profiles.map((p) => h('option', { value: p.name, selected: p.name === view.name ? true : null }, p.name)));
  // No Start beside the picker (the user, 2026-10-05). A project has one chat session at a time -
  // two Claude Codes in one working tree would commit over each other - and a Start here read as
  // "open another chat on this project" while one was running. Picking shows the project; the
  // card on the right starts or stops it. A second session on its own worktree is feature 86.
  return [
    h('div', { class: 'form-row session-pick-row' }, picker),
    active.length
      ? h('div', { class: 'active-sessions' }, ...active.map((profile) => sessionRow(profile, state)))
      : h('p', { class: 'muted active-sessions empty' }, 'Nothing running. Pick a project, then press Start beside it.'),
    profiles.length ? null : h('p', { class: 'muted' }, 'No project profiles yet. Add one with Edit project profiles.'),
    h('button', { type: 'button', id: 'session-edit-profiles', class: 'link' },
      view.editing ? 'Close project profiles' : 'Edit project profiles'),
  ];
}

// ---- conversations already held ---------------------------------------------------------------

// Claude Code keeps a transcript per conversation, so one can be picked up rather than started
// again. The list is fetched when the session changes, starts or stops, and every 30 seconds while
// it is idle (D86): a conversation begun in a terminal while the page was open never appeared. The
// server keeps what each transcript opens with, so a reload is a listing and a few stats.
//
// When, as a date and time rather than "2 h ago" (D86): three conversations of one client all
// read "2 h ago" and could not be told apart. Today's need only the time.
function when(seconds) {
  const at = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  const time = `${pad(at.getHours())}:${pad(at.getMinutes())}`;
  return at.toDateString() === new Date().toDateString()
    ? time : `${pad(at.getDate())}/${pad(at.getMonth() + 1)} ${time}`;
}

// Every folder this profile has, not just the one selected.
//
// past() already returns them grouped per project; this used to keep one group and drop the
// rest, so a
// conversation held in another of this profile's own folders was invisible unless you happened to
// choose that folder first (50). Nothing new is read and nothing is widened - the folders are the
// profile's either way.
//
// The branch comes with it. history.py collects it and a single flat option had nowhere to put
// it, and which branch a conversation was on is usually how you tell two of them apart.
// Cut on characters, not a pixel guess (D62): the column is a third of a viewport whose width
// nobody knows, so CSS ellipsis does the visible part of the closed box and this keeps the open
// list from growing as wide as the longest opening prompt. The whole title is on the option's
// `title`, and on the select's once it is chosen.
const TITLE_CHARS = 60;

const cut = (text) => (text.length > TITLE_CHARS ? `${text.slice(0, TITLE_CHARS).trimEnd()}...` : text);

// Conversations that open with the same brief are told apart by what was said last (D86): the
// label shows that instead, and the tooltip always carries both.
function conversationLabel(row, shared) {
  const branch = row.branch ? ` [${row.branch}]` : '';
  const said = shared && row.last && row.last !== row.title ? `latest: ${cut(row.last)}` : cut(row.title);
  return `${when(row.updated)}${row.busy ? ' (in use)' : ''}${branch} - ${said}`;
}

function conversationTip(row) {
  return row.last && row.last !== row.title ? `${row.title}\n\nLatest: ${row.last}` : row.title;
}

// Openings that more than one conversation in the list starts with.
function sharedTitles(groups) {
  const seen = new Map();
  for (const entry of groups) for (const row of entry.sessions) seen.set(row.title, (seen.get(row.title) || 0) + 1);
  return new Set([...seen].filter(([, n]) => n > 1).map(([title]) => title));
}

function pastOptions(past, project) {
  const groups = (past || []).filter((entry) => (entry.sessions || []).length);
  const total = groups.reduce((n, entry) => n + entry.sessions.length, 0);
  const first = [h('option', { value: '' }, total ? 'Start a new conversation' : 'Nothing to continue here')];
  if (!total) return first;
  const shared = sharedTitles(groups);
  // One folder's worth reads as a plain list; several need saying which is which - and a folder
  // this profile does not cover always says so (feature 50).
  if (groups.length === 1 && !groups[0].outside) {
    return [...first, ...groups[0].sessions.map((row) =>
      h('option', { value: row.id, title: conversationTip(row), dataset: { project: groups[0].project } },
        conversationLabel(row, shared.has(row.title))))];
  }
  const ordered = [...groups].sort((a, b) => (a.project === project ? -1 : b.project === project ? 1 : 0));
  return [...first, ...ordered.map((entry) =>
    h('optgroup', { label: entry.outside ? `Not in this project profile yet: ${entry.project}` : entry.project },
      ...entry.sessions.map((row) =>
        h('option', { value: row.id, title: conversationTip(row),
                      dataset: { project: entry.project, outside: entry.outside ? '1' : null } },
          conversationLabel(row, shared.has(row.title))))))];
}

// ---- the slash list ---------------------------------------------------------------------------

// What the list holds is read off this machine - the project's commands, this person's, the
// plugins' skills - plus the few built-ins that work in a session that has no terminal. It is
// not the terminal's menu: half of that menu opens a picker nobody here could see.
function slashWord(box) {
  const at = box.selectionStart === null ? box.value.length : box.selectionStart;
  const before = box.value.slice(0, at);
  const line = before.slice(before.lastIndexOf('\n') + 1);
  const found = /(^|\s)(\/[A-Za-z0-9:_-]*)$/.exec(line);
  return found ? found[2] : null;
}

function matching(all, typed) {
  const wanted = typed.slice(1).toLowerCase();
  const rows = (all || []).filter((row) => row.name.slice(1).toLowerCase().includes(wanted));
  // What starts with what was typed comes first: /commit before /plugin:precommit.
  rows.sort((a, b) => {
    const first = a.name.slice(1).toLowerCase().startsWith(wanted) ? 0 : 1;
    const second = b.name.slice(1).toLowerCase().startsWith(wanted) ? 0 : 1;
    return first - second || a.name.localeCompare(b.name);
  });
  // No cap. Everything past the twelfth match used to be discarded here, so it could not be
  // scrolled to, arrowed to or reached at all - view.slashRows was set to the truncated array and
  // the arrow handler wrapped around ITS length - and nothing said a cut had been made, so twelve
  // commands and a hundred looked identical (47). The page already holds them all; only the
  // display was capped.
  return rows;
}

function slashRows(rows, chosen) {
  return rows.map((row, index) => h('button', {
    type: 'button',
    class: `slash-row${index === chosen ? ' selected' : ''}`,
    dataset: { name: row.name },
  }, h('span', { class: 'slash-name' }, row.name + (row.takes_value ? ' <value>' : '')),
     h('span', { class: 'slash-kind chip' }, row.source === 'built-in' ? row.kind : row.source),
     h('span', { class: 'slash-what muted' }, row.description || '')));
}

// ---- permission cards -------------------------------------------------------------------------

// The waiting questions of the session on screen, and only those (the user, 2026-10-01): a card
// from another session answered in this one reads as the wrong conversation's question. Another
// session's waiting questions show as "N waiting" on its row in the session list, and each card
// still says which session asked.
function permissionCards(state) {
  const cards = [];
  for (const [name, session] of Object.entries(state.sessions || {})) {
    if (name !== view.name) continue;
    const profile = (state.profiles || []).find((p) => p.name === name) || {};
    for (const request of session.pending || []) {
      if (request.tool === 'AskUserQuestion' && ((request.detail || {}).questions || []).length) {
        cards.push(questionCard(name, request));
        continue;
      }
      cards.push(h('div', { class: 'card warn', dataset: { permission: request.id } },
        h('h3', {}, `${name}: allow ${request.tool}?`),
        profile.label && profile.label !== name ? h('p', { class: 'hint' }, `Client: ${profile.label}`) : null,
        h('pre', { class: 'mono' }, JSON.stringify(request.detail, null, 1)),
        h('div', { class: 'form-row' },
          h('button', { type: 'button', class: 'primary allow', dataset: { id: request.id } }, 'Allow'),
          h('button', { type: 'button', class: 'deny', dataset: { id: request.id } }, 'Deny'))));
    }
  }
  return cards;
}

// A question from Claude, drawn as the question it is (D58). It used to arrive as "allow
// AskUserQuestion?" over its raw JSON, as though it were a tool to permit. Now each question shows
// its options - radio buttons, or checkboxes where several may apply - and an "Other" box, and
// Answer sends the choices back; Claude Code receives them as the question's answers.
function questionCard(name, request) {
  const questions = (request.detail || {}).questions || [];
  return h('div', { class: 'card question-card', dataset: { permission: request.id } },
    h('h3', {}, `${name}: Claude is asking`),
    ...questions.map((q, i) => questionSet(q, `q${request.id}-${i}`)),
    h('div', { class: 'form-row' },
      h('button', { type: 'button', class: 'primary answer-question', dataset: { id: request.id } }, 'Answer'),
      h('button', { type: 'button', class: 'deny', dataset: { id: request.id } }, 'Decline')));
}

// One question: every option on its own line with its description under it, then Other - a choice
// like the rest, with a text area that grows (2026-09-30: Other had no button, so it could not be
// chosen, and its box showed about 20 characters). Typing in it chooses it.
const OTHER = '__other__';

function questionSet(q, group) {
  const type = q.multiSelect ? 'checkbox' : 'radio';
  const other = h('textarea', { class: 'other-answer', rows: '1', 'aria-label': `Other answer to: ${q.question}`,
    placeholder: 'Type your own answer' });
  const otherPick = h('input', { type, name: group, value: OTHER, class: 'other-pick', 'aria-label': 'Other' });
  other.addEventListener('input', () => {
    if (other.value.trim()) otherPick.checked = true;
    other.style.height = 'auto';
    other.style.height = `${other.scrollHeight}px`;
  });
  return h('fieldset', { class: 'question', dataset: { question: q.question } },
    h('legend', {}, q.header ? h('span', { class: 'chip' }, q.header) : null, ' ', q.question),
    ...(q.options || []).map((o) => h('label', { class: 'option' },
      h('input', { type, name: group, value: o.label }),
      h('span', { class: 'option-text' }, h('strong', {}, o.label),
        o.description ? h('span', { class: 'hint option-description' }, o.description) : null))),
    h('label', { class: 'option other' }, otherPick,
      h('span', { class: 'option-text' }, h('strong', {}, 'Other'), other)));
}

// The choices in a question card, keyed by question text; null when one is left unanswered.
function answersIn(card) {
  const answers = {};
  for (const set of card.querySelectorAll('fieldset.question')) {
    const picked = [...set.querySelectorAll('input:checked')].map((box) => box.value).filter((v) => v !== OTHER);
    const other = set.querySelector('.other-answer').value.trim();
    if (other && set.querySelector('.other-pick').checked) picked.push(other);
    if (!picked.length) return null;
    answers[set.dataset.question] = picked.join(', ');
  }
  return answers;
}

// ---- editing the profiles ---------------------------------------------------------------------

// The folders a session may work in, with a picker under them. Browse adds a line rather than
// replacing the box: a profile usually points at more than one folder, and replacing the list
// would quietly drop the rest. Saving checks every line against the allowed folders anyway.
// The workspace notes go in `notes`, refilled whenever the lists change (hintsFor).
function foldersField(profile, notes, shareNow) {
  const area = h('textarea', { class: 'p-projects', rows: '2' }, (profile.projects || []).join('\n'));
  // The client's OneDrive share is always included, so it is not added here as well (DECISIONS 64).
  const bare = (path) => String(path || '').replace(/[\\/]+$/, '').toLowerCase();
  const refuse = (path) => (shareNow() && bare(path) === bare(shareNow())
    ? 'That is the client\'s OneDrive share, which is always included - it does not need adding here.' : '');
  return h('div', { class: 'p-places' },
    h('label', { class: 'field' }, 'Folders added by hand (one per line)',
      area, folderPicker(area, { append: true, label: 'Add a folder', refuse })),
    notes);
}

// Workspace files (D99): every folder they list is in the Files tree, and the first one sets the
// home folder. Browse picks a folder and adds the .code-workspace files in it. `hint` holds the
// "found inside a folder" note, refilled with the others.
// The offer to create a workspace file sits beside the list and its Yes under it, level with the
// browse button (the user, 2026-10-05: tucked inside a closed <details> in the notices, its button
// could not be found). Both are slots, refilled with the other hints.
function workspacesField(profile, hint, homeNow) {
  const own = profile.workspaces || [];
  const area = h('textarea', { class: 'p-workspaces', rows: '2', id: `p-ws-${Math.random().toString(36).slice(2)}`,
    placeholder: 'C:\\Repos\\client\\client.code-workspace' }, own.join('\n'));
  // The picker chooses one .code-workspace file, and this adds it to the list.
  const chosen = h('input', { type: 'hidden', class: 'p-workspace-file', 'data-unsaved-ignore': '' });
  chosen.addEventListener('input', (event) => {
    event.stopPropagation();          // the list's own input event follows, once it has changed
    const file = chosen.value.trim();
    chosen.value = '';
    if (!file) return;
    const have = linesOf(area);
    if (!have.some((f) => f.toLowerCase() === file.toLowerCase())) area.value = [...have, file].join('\n');
    area.dispatchEvent(new Event('input', { bubbles: true }));
  });
  return h('div', { class: 'field p-ws' },
    h('label', { for: area.id }, 'Workspace files (one per line; the first sets the home folder)'),
    h('div', { class: 'p-ws-grid' },
      area, h('div', { class: 'p-slot-create-ask' }),
      h('div', {}, chosen, folderPicker(chosen, { label: 'Browse to workspace file', workspaceFiles: true,
        chooseLabel: 'Add to workspace list', start: () => besideHome(homeNow()) })),
      h('div', { class: 'p-slot-create-yes' })),
    hint);
}

function linesOf(area) {
  return area.value.split('\n').map((line) => line.trim()).filter(Boolean);
}

// An older profile's workspace file, found inside one of its folders: in use, not yet its own.
function implicitHint(row, workspace) {
  if (linesOf(row.querySelector('.p-workspaces')).length) return null;
  if (!workspace || !workspace.implicit || !(workspace.workspaces || []).length) return null;
  return h('p', { class: 'hint' }, `In use, found inside a folder: ${workspace.workspaces[0]}. Add it here to keep it.`);
}

const HOME_WHY = 'Sessions start here, it is the one folder aicleaner writes Claude settings into, and a '
  + 'command that changes folder is brought back here when it ends - so Claude keeps working in the right repo.';

// Where this profile's work is homed (D99): worked out from the first workspace file, or the
// user's own choice. The override is saved with the profile; Reset clears it, and the derived
// folder - worked out afresh each time - is what it goes back to. Two parts: the block (heading,
// why, the folder, its buttons on one row) and any drift, which goes with the notices.
function homeField(values, workspace, row) {
  const home = (workspace && workspace.home) || {};
  // Whether a workspace file decides the home, or (with none) the profile's first folder: the
  // Reset button and the "how" both say which (the user, 2026-10-05).
  const fromWorkspace = Boolean(workspace && (workspace.workspaces || []).length);
  const derivedHow = fromWorkspace ? 'from workspace file' : 'first profile folder';
  const howOf = () => {
    if (home.overridden) return 'set by you';
    if (/no longer exists/.test(home.how || '')) return home.how;
    return derivedHow;
  };
  const override = h('input', { type: 'hidden', class: 'p-home-override', value: values.home_override || '' });
  const was = h('input', { type: 'hidden', class: 'p-home-derived-was', value: values.home_derived_was || '' });
  // The whole path, beside the heading, so it can be checked at a glance (the user, 2026-10-05).
  const shown = h('span', { class: 'mono p-home' }, home.folder || '(none yet)');
  const how = h('span', { class: 'p-home-how' }, home.folder ? ` (${howOf()})` : '');
  override.addEventListener('input', () => {
    was.value = home.derived || '';
    shown.textContent = override.value;
    how.textContent = ' (set by you - save the project profile to use it)';
    reset.hidden = false;
  });
  const reset = h('button', { type: 'button', class: 'p-home-reset', hidden: !home.overridden },
    fromWorkspace ? 'Set to 1st workspace\'s Home' : 'Set to 1st Profile folder');
  reset.addEventListener('click', () => {
    override.value = '';
    was.value = '';
    shown.textContent = home.derived || '(none yet)';
    how.textContent = ` (${derivedHow} - save the project profile to use it)`;
    reset.hidden = true;
    row.dispatchEvent(new Event('hints-stale'));
  });
  // Only a real disagreement: a choice that matches what the workspace file says now is not drift,
  // however it read when the choice was made.
  const drift = home.overridden && values.home_derived_was && home.derived && home.folder
    && home.derived.toLowerCase() !== values.home_derived_was.toLowerCase()
    && home.derived.toLowerCase() !== home.folder.toLowerCase()
    ? h('p', { class: 'hint warn' }, `The workspace file now says ${lastSegment(home.derived)}; you chose `
      + `${lastSegment(home.folder)} when it said ${lastSegment(values.home_derived_was)}.`)
    : null;
  return {
    block: h('div', { class: 'p-home-row' },
      h('div', { class: 'p-home-head' }, h('span', { class: 'p-home-label' }, 'Home folder - '), shown, how),
      h('span', { class: 'hint p-home-why' }, HOME_WHY),
      h('div', { class: 'p-home-line' }, override, was,
        // Together, so the pair moves to the next line as one rather than splitting.
        h('span', { class: 'p-home-buttons' },
          folderPicker(override, { label: 'Choose another home folder', start: () => besideHome(workspace) }),
          reset))),
    drift,
  };
}

// The folder that holds the home folder: where its sibling repos and, usually, the workspace file
// are. Empty (the picker's own default) when there is no home yet.
function besideHome(workspace) {
  const folder = (workspace && workspace.home && workspace.home.folder) || '';
  return folder.replace(/[\\/][^\\/]+[\\/]?$/, '');
}

// No workspace file at all (D99): offer one, shown in full before it is written.
// The question and its Yes are separate pieces: the question beside the workspace list, Yes under
// it, level with the browse button (the user, 2026-10-05). What will be written stays one click
// away. The server saves the profile with the new file first; the list and the saved copy the
// other tabs save from are brought up to date with it.
function createOffer(name, workspace, row) {
  const create = workspace && workspace.create;
  if (!create) return null;
  const ask = h('div', { class: 'hint create-offer' },
    h('p', {}, `No workspace file. Create ${create.path}?`),
    h('details', {}, h('summary', {}, 'What it will contain'), h('pre', { class: 'mono' }, create.text)));
  const yes = h('button', { type: 'button', class: 'primary create-workspace' }, 'Yes, create it');
  busyButton(yes, async () => {
    try {
      await postJSON('/api/session/workspace-create', { name });
      say(`${create.path} created and added to the project profile.`, 'ok');
      const area = row.querySelector('.p-workspaces');
      const have = linesOf(area).filter((f) => f.toLowerCase() !== create.path.toLowerCase());
      area.value = [create.path, ...have].join('\n');
      rebase(row);                      // saved by the server already
      view.profiles = await getJSON('/api/session/profiles');
      ask.replaceChildren(h('p', {}, `Created ${lastSegment(create.path)}.`));
      yes.remove();
      area.dispatchEvent(new Event('input', { bubbles: true }));
    } catch (error) { sayError(error); }
  });
  return { ask, yes };
}

// The workspace file's terminal working directory (feature 71). It holds Claude to the home
// folder: a command that changes directory elsewhere comes back when it ends. Offered when missing,
// written only on a click, and the server recomputes what to write rather than trusting the page.
// With none, the folder is chosen from those the file lists - never a multi-repo parent (D131) - and
// written into the file whether or not the profile has been saved; the home follows it.
function cwdOffer(name, workspace, row) {
  const cwd = (workspace && workspace.cwd) || {};
  const file = workspace && workspace.workspace ? lastSegment(workspace.workspace) : '';
  if (cwd.current) {
    return h('p', { class: 'hint cwd-set' }, `Terminal working directory: ${cwd.current} (from ${file}).`);
  }
  const choices = cwd.choices || [];
  if (!choices.length) return null;
  const preferred = /^\$\{workspaceFolder:(.+)\}$/.exec(cwd.suggested || '');
  const pick = h('select', { class: 'workspace-cwd-pick', 'aria-label': 'Home folder for the workspace file',
    'data-unsaved-ignore': '' },
    ...choices.map((choice) => h('option', { value: choice,
      selected: preferred && preferred[1] === choice ? true : null }, choice)));
  const set = h('button', { type: 'button', class: 'set-workspace-cwd' }, 'Add it');
  busyButton(set, async () => {
    try {
      await postJSON('/api/session/workspace-cwd', { name, workspace: workspace.workspace, folder: pick.value });
      say(`${file} now keeps the terminal in ${pick.value}, and that is the home folder.`, 'ok');
      set.replaceWith(h('span', { class: 'hint' }, 'Added.'));
      if (row) row.dispatchEvent(new Event('hints-stale'));
    } catch (error) { sayError(error); }
  });
  return h('p', { class: 'hint warn cwd-offer' },
    `${file} does not say which of its folders sessions work in (terminal.integrated.cwd). Choose one `
    + 'to add to it - it becomes the home folder: ', pick, ' ', set);
}

// A workspace file just added to the list brings the folders it lists into the folders box (D132),
// once, without duplicates; one already in the list keeps its "Add N folders" offer instead.
function addNewWorkspaceFolders(row, workspace) {
  const pending = row.wsPending || new Set();
  row.wsPending = new Set();
  if (!pending.size || !workspace) return;
  const area = row.querySelector('.p-projects');
  const have = linesOf(area);
  const lower = new Set(have.map((f) => f.toLowerCase().replace(/\//g, '\\')));
  const added = [];
  for (const gap of workspace.missing || []) {
    if (!pending.has(String(gap.in).toLowerCase())) continue;
    for (const folder of gap.folders) {
      const key = String(folder).toLowerCase().replace(/\//g, '\\');
      if (!lower.has(key)) { lower.add(key); added.push(folder); }
    }
  }
  if (!added.length) return;
  area.value = [...have, ...added].join('\n');
  say(`${added.length} folder(s) from the workspace file added. Save the project profile to keep them.`, 'ok');
  area.dispatchEvent(new Event('input', { bubbles: true }));
}

function lastSegment(path) {
  return String(path).split(/[\\/]/).filter(Boolean).pop() || String(path);
}

// A hand-added folder that is the client's OneDrive share (DECISIONS 64): removed on save, since
// the share is always included - unless it is where sessions start, which it then stays as.
function shareNote(workspace) {
  if (!workspace || !workspace.share_in_folders) return [];
  return [h('p', { class: 'hint p-share-note' }, workspace.share_is_home
    ? `${workspace.share} is also the client's OneDrive share. It stays in this list: it is where sessions start.`
    : `${workspace.share} is the client's OneDrive share, which is always included. It will be removed from this list when you save.`)];
}

function workspaceNotes(area, workspace) {
  if (!workspace) return [];
  const notes = [];                     // the home folder has its own line now (homeField, D99)
  for (const gap of workspace.missing || []) {
    const add = h('button', { type: 'button', class: 'add-workspace-folders' },
      `Add ${gap.folders.length} folder${gap.folders.length === 1 ? '' : 's'}`);
    add.addEventListener('click', () => {
      const have = linesOf(area);
      const lower = new Set(have.map((f) => f.toLowerCase()));
      const added = gap.folders.filter((f) => !lower.has(String(f).toLowerCase()));
      area.value = [...have, ...added].join('\n');
      add.disabled = true;
      say(`${added.length} folder(s) added. Save the project profile to keep them.`, 'ok');
      area.dispatchEvent(new Event('input', { bubbles: true }));
    });
    notes.push(h('p', { class: 'hint' },
      `${gap.folders.length} folder(s) in ${gap.workspace} are not in this project profile: `,
      h('span', { class: 'mono' }, gap.folders.map(lastSegment).join(', ')),
      ' ', add));
  }
  return notes;
}

// Everything worked out from the folder lists, put back from one answer: the implicit workspace
// note, the "not in this profile" notes, the home folder and the notices. Called when the row is
// built and again whenever its lists change, so removing a folder takes its offers with it (the
// user, 2026-10-04: a removed folder's workspace file was still offered).
function hintsFor(row, workspace, values) {
  row.workspace = workspace;
  const slot = (name) => row.querySelector(`.p-slot-${name}`);
  slot('implicit').replaceChildren(...[implicitHint(row, workspace)].filter(Boolean));
  slot('notes').replaceChildren(...workspaceNotes(row.querySelector('.p-projects'), workspace),
    ...shareNote(workspace));
  const home = homeField(values || readProfile(row), workspace, row);
  slot('home').replaceChildren(home.block);
  const name = row.dataset.was || '';
  const offer = createOffer(name, workspace, row);
  slot('create-ask').replaceChildren(...(offer ? [offer.ask] : []));
  slot('create-yes').replaceChildren(...(offer ? [offer.yes] : []));
  const notices = [cwdOffer(name, workspace, row), home.drift].filter(Boolean);
  slot('notices').replaceChildren(...notices);
  slot('notices').hidden = !notices.length;
}

// Asked again a moment after the lists stop changing; an answer to an older question is dropped.
function watchHints(row) {
  let timer = null;
  let asked = 0;
  const again = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const mine = ++asked;
      try {
        const answer = await postJSON('/api/session/workspace-view', { profile: readProfile(row) });
        if (mine === asked) {
          addNewWorkspaceFolders(row, answer.workspace || null);
          hintsFor(row, answer.workspace || null);
        }
      } catch (error) { /* the hints are a help, never a reason to interrupt */ }
    }, 400);
  };
  // The workspace files already listed, so only one added from now on brings its folders (D132).
  const workspaceLines = () => linesOf(row.querySelector('.p-workspaces')).map((f) => f.toLowerCase());
  row.wsSeen = new Set(workspaceLines());
  row.wsPending = new Set();
  row.addEventListener('input', (event) => {
    if (event.target.matches('.p-workspaces')) {
      const now = workspaceLines();
      for (const line of now) if (!row.wsSeen.has(line)) row.wsPending.add(line);
      row.wsSeen = new Set(now);
    }
    if (event.target.matches('.p-projects, .p-workspaces, .p-home-override')) again();
  });
  row.addEventListener('hints-stale', again);
}

const RUNNING_LOCK = 'This project profile\'s session is running. Stop the session to rename or delete it.';

function profileRow(profile, clients, workspace, running) {
  const labelOf = (cid) => (clients.find((c) => c.id === cid) || {}).label || cid;
  const client = h('select', { class: 'p-client' },
    h('option', { value: '' }, 'None (Global only)'),
    ...clients.map((c) => h('option', { value: c.id, selected: c.id === profile.client ? true : null }, c.label)));
  // The client's OneDrive share (D99, moved from the Clients page): per client, kept on this
  // machine. Saved on its own, so it follows the drop-down rather than the profile's Save.
  const share = shareSlot(profile.client, labelOf(profile.client));
  client.addEventListener('change', () => {
    share.dataset.client = client.value;
    if (client.value) fillShare(share, client.value, labelOf(client.value));
    else share.replaceChildren();
  });
  // A running session is known by its profile's name, so the name cannot change under it
  // (the user, 2026-10-04): locked, and saying why, until the session stops.
  const name = h('input', { class: 'p-name', value: profile.name || '', placeholder: 'ACME',
    disabled: running ? true : null, title: running ? RUNNING_LOCK : null });
  const row = h('fieldset', { class: 'profile-row', dataset: { profile: 'row', was: profile.name || '' } },
    h('div', { class: 'p-grid' },
      h('label', { class: 'field', title: running ? RUNNING_LOCK : null }, 'Name', name),
      h('label', { class: 'field' }, 'Client', client),
      // A list, offered rather than enforced (feature 45). A name that does not exist used to be
      // accepted here and fail when the session started, which is the worst moment to find out.
      // Still an <input>, deliberately: the names come from the API using the key this machine
      // already holds, and a list that cannot be fetched must not turn this into a box you cannot
      // type in. `check_model` remains the thing that decides what is allowed.
      // A real drop-down since 2026-09-30 (the user): the list, then "Other..." to type a name. The
      // text box stays the value; the drop-down only fills it, and is hidden while there is no list.
      h('label', { class: 'field' }, 'Model',
        h('select', { class: 'p-model-pick', hidden: true, 'aria-label': 'Model' }),
        h('input', { class: 'p-model', value: profile.model || '', placeholder: 'default' })),
      h('div', { class: 'p-half p-slot-home' }),
      h('div', { class: 'p-half' },
        workspacesField(profile, h('div', { class: 'p-slot-implicit' }), () => row.workspace)),
      h('div', { class: 'p-half' },
        foldersField(profile, h('div', { class: 'p-slot-notes' }), () => (row.workspace || {}).share || '')),
      h('div', { class: 'p-half' }, share),
      h('label', { class: 'field p-half' }, 'Standing brief',
        h('textarea', { class: 'p-brief', rows: '2', placeholder: 'What this engagement is, and what not to touch.' },
          profile.brief || '')),
      h('div', { class: 'p-full p-notices p-slot-notices' })));
  row.running = Boolean(running);
  name.addEventListener('input', () => {        // the tab says the name as it is typed
    const tab = document.querySelector(`#profile-tabs .p-tab[data-index="${profileRows().indexOf(row)}"]`);
    if (tab) tab.lastChild.textContent = tabLabel(row);
  });
  hintsFor(row, workspace, profile);
  watchHints(row);
  return row;
}

const MODEL_OTHER = '__other__';

// The drop-down in front of a profile's Model box: Default, Claude Code's own model names (always -
// they need no key, D95), any full model names the API listed, then Other..., which shows the box
// for a name not on the list.
function modelPick(row, names, aliases) {
  const pick = row.querySelector('.p-model-pick');
  const box = row.querySelector('.p-model');
  const short = (aliases || []).map((a) => a.name);
  if (!names.length && !short.length) { pick.hidden = true; box.hidden = false; return; }
  const current = box.value.trim();
  pick.replaceChildren(
    h('option', { value: '' }, "Default (Claude Code's own choice)"),
    ...(aliases || []).map((a) => h('option', { value: a.name }, a.label)),
    ...names.map((name) => h('option', { value: name }, name)),
    h('option', { value: MODEL_OTHER }, 'Other...'));
  const listed = current === '' || short.includes(current) || names.includes(current);
  pick.value = listed ? current : MODEL_OTHER;
  pick.hidden = false;
  box.hidden = listed;
  pick.addEventListener('change', () => {
    if (pick.value === MODEL_OTHER) {
      box.hidden = false;
      box.focus();
    } else {
      box.value = pick.value;
      box.hidden = true;
    }
  });
}

function readProfile(row) {
  return {
    name: row.querySelector('.p-name').value.trim(),
    client: row.querySelector('.p-client').value,
    projects: linesOf(row.querySelector('.p-projects')),
    // D99: this machine's workspace files and home folder choice.
    workspaces: linesOf(row.querySelector('.p-workspaces')),
    home_override: row.querySelector('.p-home-override').value.trim(),
    home_derived_was: row.querySelector('.p-home-derived-was').value.trim(),
    model: row.querySelector('.p-model').value.trim(),
    brief: row.querySelector('.p-brief').value,
    // The name it was saved under, so a rename keeps its earlier homes (the server decides).
    was: row.dataset.was || '',
  };
}

// ---- the tabs: one project profile at a time (the user, 2026-10-04) ---------------------------

function profileRows() {
  const rows = document.getElementById('profile-rows');
  return rows ? Array.from(rows.querySelectorAll('[data-profile="row"]')) : [];
}

function activeRow() {
  return profileRows().find((row) => !row.hidden) || null;
}

function tabLabel(row) {
  return row.querySelector('.p-name').value.trim() || 'New project profile';
}

// Draws the tab strip from the rows, and shows the one chosen (by index; the first when none is).
function showTab(index) {
  const rows = profileRows();
  const strip = document.getElementById('profile-tabs');
  if (!strip) return;
  const chosen = Math.max(0, Math.min(index, rows.length - 1));
  strip.replaceChildren(...rows.map((row, i) => h('button', {
    type: 'button', role: 'tab', class: `p-tab${i === chosen ? ' selected' : ''}`,
    'aria-selected': String(i === chosen), dataset: { index: String(i) },
  }, row.running ? h('span', { class: 'dot', title: 'Running' }) : null, tabLabel(row))),
  // A new project profile from the tab strip itself (the user, 2026-10-05).
  h('button', { type: 'button', class: 'p-tab p-tab-add', title: 'Add a project profile',
    'aria-label': 'Add a project profile' }, '+'));
  rows.forEach((row, i) => { row.hidden = i !== chosen; });
  const active = rows[chosen];
  view.profileTab = active ? active.dataset.was : '';
  const remove = document.getElementById('profile-delete');
  if (remove) {
    remove.disabled = !active || active.running;
    remove.title = active && active.running ? RUNNING_LOCK : 'Delete this project profile';
  }
}

function profileEditor(data) {
  const running = new Set(data.running || []);
  const rows = h('div', { id: 'profile-rows' },
    ...(data.profiles || []).map((profile) =>
      profileRow(profile, data.clients || [], (data.workspace || {})[profile.name], running.has(profile.name))));
  // One datalist for every Model field on the page. Filled after the card is built, because the
  // names come from the API and a card that waited for them would be a card that never appeared
  // on a machine with no key (feature 45).
  const known = h('datalist', { id: 'known-models' });
  const why = h('p', { class: 'hint', hidden: true });
  getJSON('/api/models').then((answer) => {
    known.replaceChildren(...(answer.models || []).map((name) => h('option', { value: name })));
    // Kept for rows added later with "Add a project profile": they get the same picker (D95).
    view.modelChoices = { models: answer.models || [], aliases: answer.aliases || [] };
    for (const row of rows.querySelectorAll('[data-profile="row"]')) modelPick(row, answer.models || [], answer.aliases);
    // Without a key the full names cannot be listed, but Claude Code's own names are offered
    // anyway, so that is nothing worth a warning; a failure with nothing to offer still is.
    if (answer.why && !(answer.aliases || []).length) {
      why.textContent = `Model names could not be listed: ${answer.why}`;
      why.hidden = false;
    }
  }).catch(() => {});      // the field works without them; that is the whole design
  return h('section', { class: 'card', id: 'session-profiles' },
    known, why,
    h('h3', {}, 'Project profiles'),
    h('p', { class: 'hint' },
      `A project profile is one engagement: its client is what the session is labelled and logged as, and its folders are what the file tree shows. Filtering is the same in every session. Stored in ${data.path}.`),
    data.problem ? h('p', { class: 'chip warn' }, data.problem) : null,
    h('div', { id: 'profile-tabs', class: 'p-tabs', role: 'tablist' }),
    rows,
    h('div', { class: 'form-row p-actions' },
      h('button', { type: 'button', id: 'profile-add' }, 'Add a project profile'),
      h('span', { class: 'grow' }),
      iconButton('trash', 'Delete this project profile', { id: 'profile-delete', class: 'icon p-remove' }),
      h('button', { type: 'button', id: 'profile-save', class: 'primary' }, 'Save'),
      h('button', { type: 'button', id: 'profile-close' }, 'Close')));
}

// ---- the page ----------------------------------------------------------------------------------

function subscribe(name, refresh, onEvent) {
  if (view.stream) view.stream.close();
  view.stream = null;
  // Through the encrypted tunnel the stream cannot be sealed (an EventSource carries no body), so
  // the page polls instead: the state it polls carries the same events (feature 21).
  if (!name || isSealed()) return;
  view.stream = new EventSource(`/api/session/events?name=${encodeURIComponent(name)}`);
  // Use what arrives. This was `onmessage = () => refresh()` - the handler did not even take the
  // event - so a payload the server had just serialised in full was thrown away and replaced by
  // a GET of the entire session state, a parse of it, and a rebuild of every part of the page.
  // With --include-partial-messages that is one such cycle PER TEXT FRAGMENT: hundreds per
  // answer, each a round trip plus a complete DOM rebuild. That was the lag on this page (D34).
  view.stream.onmessage = (message) => {
    let event = null;
    try {
      event = JSON.parse(message.data);
    } catch (error) {
      refresh();               // unreadable: fall back to the old behaviour rather than miss it
      return;
    }
    onEvent(event);
  };
  view.stream.onopen = () => { setLink('live'); view.linkTries = 0; };
  // A way to sever the stream from a test, because the thing being tested is what happens when
  // it dies and there is no other way to make that happen on demand. Closing the EventSource
  // does not fire onerror - the browser treats it as deliberate - so the handler is called
  // directly, which is exactly what a server-side close does to a live page.
  window.__aicleanerStreamForTest = () => {
    if (view.stream) { view.stream.close(); view.stream.onerror(); }
  };
  // Not a no-op. This used to read "the browser retries; the page also polls on actions", and
  // both halves let the page go quiet without saying so (D61):
  //
  //   * EventSource retries a dropped *connection* on its own. It does NOT retry once the
  //     server has answered with an HTTP error - readyState goes to CLOSED and stays there, and
  //     the page is deaf for good.
  //   * "polls on actions" means state is refetched when the reader does something. Watching a
  //     reply arrive is not an action, so the one moment the stream is the only thing keeping
  //     the page alive is the moment the fallback does not cover.
  //
  // Reported as "you just dropped the session again" and, separately, as a delay that looked
  // exactly the same from the outside. That is the real fault: a slow stream and a dead one
  // were indistinguishable, so say which it is.
  view.stream.onerror = () => {
    if (view.stream && view.stream.readyState === EventSource.CONNECTING) {
      setLink('retrying');     // the browser is handling it; just say so
      return;
    }
    setLink('down');
    // Closed for good, so reconnect ourselves. Backoff because the usual cause is the proxy
    // restarting, and a tight loop against something that is still coming up is how a page
    // turns a five-second outage into a hammering.
    view.linkTries = (view.linkTries || 0) + 1;
    const wait = Math.min(30000, 500 * (2 ** Math.min(view.linkTries, 6)));
    clearTimeout(view.linkTimer);
    view.linkTimer = setTimeout(view.resubscribe, wait);
  };
  view.resubscribe = () => {
    clearTimeout(view.linkTimer);
    setLink('retrying');
    subscribe(name, refresh, onEvent);
    refresh().catch(() => {});   // catch up on whatever was missed while deaf
  };
  // The network coming back (a lift, Wi-Fi to mobile data) or the phone being unlocked is the
  // moment to try, not whenever the back-off next fires - up to 30 s later (DECISIONS 49).
  if (!view.wakeHooked) {
    view.wakeHooked = true;
    const wake = () => {
      if (view.link !== 'live' && view.resubscribe && !document.hidden) {
        view.linkTries = 0;
        view.resubscribe();
      }
    };
    window.addEventListener('online', wake);
    document.addEventListener('visibilitychange', wake);
  }
}

// What the AI service is doing with this session's request (DECISIONS 47). Quiet while a reply is
// arriving; after a minute with no reply text it says how long it has waited, and after two it
// offers Interrupt. Pings do not count as reply text: a stream of pings alone is a stuck one.
const MODEL_QUIET_S = 60;
const MODEL_STUCK_S = 120;

function minutes(seconds) {
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function showModelRequest(request) {
  const box = document.getElementById('session-model');
  if (!box) return;
  const text = box.querySelector('.model-state-text');
  const stop = box.querySelector('.model-state-stop');
  if (!request) { box.hidden = true; return; }
  const waited = request.waiting_s || 0;
  box.hidden = false;
  if (waited < MODEL_QUIET_S) {
    box.dataset.level = 'green';
    text.textContent = request.content_at ? 'AI service: replying' : 'AI service: request sent';
  } else {
    box.dataset.level = waited >= MODEL_STUCK_S ? 'red' : 'amber';
    text.textContent = request.content_at
      ? `AI service: no reply text for ${minutes(waited)}`
      : `Waiting on the AI service for ${minutes(waited)}`;
  }
  stop.hidden = waited < MODEL_STUCK_S;
}

// Feature 65. Three states, because "connected" and "not connected" cannot tell a reader the
// thing they actually want to know - whether silence means it is working or it is broken.
const LINK_TEXT = {
  live: ['green', 'Connected'],
  retrying: ['amber', 'Reconnecting...'],
  down: ['red', 'Not connected'],
};

function setLink(state) {
  view.link = state;
  const dot = document.getElementById('session-link');
  if (!dot) return;
  const [level, label] = LINK_TEXT[state] || LINK_TEXT.down;
  dot.dataset.level = level;
  dot.title = state !== 'live' ? 'The page is not receiving events. What you see may be out of date.'
    : isSealed() ? 'Through the encrypted tunnel the page asks this session for news every 2 seconds, and it is answering.'
      : 'The page is receiving events from this session.';
  dot.textContent = label;
  dot.hidden = false;
}

export async function render(main, context = {}) {
  // `#/session/<name>` opens on that session (D118): a notice's Open, and a status-stack entry,
  // name the session they are about. The name goes to the server, which answers for it when it is
  // a profile and falls back to its own choice when it is not.
  // With no name in the address - the menu's Project Sessions, a hard refresh of #/session - the
  // session last looked at, rather than the server's pick, which is the first alphabetically when
  // nothing runs (the user, 2026-10-06).
  const asked = (context.route && context.route.id) || rememberedSession();
  const toChat = Boolean(context.route && context.route.chat);
  let state;
  try {
    state = await getJSON(asked ? `/api/session?name=${encodeURIComponent(asked)}` : '/api/session');
  } catch (error) {
    main.replaceChildren(h('p', { class: 'muted' }, 'Driving a session is switched off (session.enabled in the config).'));
    return;
  }
  view.state = state;
  view.name = state.selected || '';
  rememberSession(view.name);
  // The state carries one history, the selected session's, at its top level (D111); the per-session
  // entries hold the rest (running, pending, project) without one.
  const current = () => {
    const session = (view.state.sessions || {})[view.name] || {};
    const mine = view.name && view.state.selected === view.name;
    return { ...session, events: mine ? (view.state.events || []) : [] };
  };
  const profileOf = (name) => (view.state.profiles || []).find((p) => p.name === name) || {};

  // Where every session of this profile starts (DECISIONS 64): shown, no longer chosen. It was a
  // drop-down, and a conversation resumed from another folder drew a blank page (D113).
  const homeShown = h('div', { id: 'session-home', class: 'mono session-home' });
  const resume = h('select', { id: 'session-resume', 'aria-label': 'Conversation to continue' });
  // Conversations a program started are hidden - except the ones started here, which are listed
  // (2026-09-30). This shows the rest too, including Session-page ones from before that fix.
  const showAll = h('label', { class: 'check hint', id: 'session-show-all',
    title: 'Also list conversations started by programs (automated reviews, and Session-page conversations from before 30 Sep)' },
    h('input', { type: 'checkbox' }), ' all', h('span', { id: 'session-all-note', class: 'muted' }, ''));
  // What "all" added, so ticking it visibly does something even when it finds nothing (D86).
  const allNote = showAll.querySelector('#session-all-note');
  const resumeLine = h('div', { class: 'hint warn', id: 'session-resume-hint' }, '');
  const slash = h('div', { id: 'session-slash', class: 'slash-list', hidden: true });
  // The keys are named in the placeholder because there is nowhere else to put them: a box with
  // hidden keyboard behaviour is a box people keep using the mouse on (feature 48).
  const box = h('textarea', { id: 'session-prompt', rows: '3',
    placeholder: 'Ask it something...  (Enter sends, Ctrl+Enter or Shift+Enter for a new line, Escape stops the turn)',
    'aria-label': 'Prompt' });
  // Each session has its own prompt (the user, 2026-10-01): switching shows that session's draft
  // and never clears another's. Kept in sessionStorage - it survives a reload of this tab, and is
  // gone when the tab closes, so what you were typing about a client is not left in the browser.
  const draftKey = (name) => `aicleaner.prompt.${name}`;
  const saveDraft = (name = view.name) => {
    if (!name) return;
    try {
      if (box.value) sessionStorage.setItem(draftKey(name), box.value);
      else sessionStorage.removeItem(draftKey(name));
    } catch (error) { /* storage refused: the draft lives until the page is left */ }
  };
  const loadDraft = () => {
    try { box.value = sessionStorage.getItem(draftKey(view.name)) || ''; } catch (error) { box.value = ''; }
    view.historyAt = null;
  };
  loadDraft();
  const log = h('div', { id: 'session-log', class: 'log' });
  // "Show earlier" at the top of the conversation, and what it brought (feature 82).
  const earlierButton = h('button', { type: 'button', class: 'link show-earlier', hidden: true }, 'Show earlier');
  const earlierBox = h('div', { class: 'log-earlier' });
  const head = h('div', { class: 'log-head' }, earlierButton, earlierBox);
  log.head = head;
  log.append(head);
  const showEarlierWhen = () => {
    const session = current();
    const first = (session.events || [])[0];
    earlierButton.hidden = Boolean(log.earlierFrom) || !first || !view.state.session_id;
  };
  head.addEventListener('earlier-changed', showEarlierWhen);
  busyButton(earlierButton, async () => {
    const events = current().events || [];
    if (!events.length) return;
    try {
      const answer = await getJSON(`/api/session/earlier?name=${encodeURIComponent(view.name)}&before=${encodeURIComponent(events[0].at || 0)}`);
      const older = (answer.events || []).map((event, i) => ({ ...event, seq: `e${i}`, replayed: true }));
      if (!older.length) { say('Nothing earlier in this conversation.'); return; }
      keepingPlace(log, () => {
        earlierBox.replaceChildren(...transcript(older, { placeholder: false }));
        log.earlierFrom = events[events.length - 1].seq;
      });
      showEarlierWhen();
      if (answer.more) say('Showing 500 earlier items. For anything further back, ask Claude to look through the transcript.');
    } catch (error) { sayError(error); }
  });
  handOverWheel(log);                   // at its top or bottom, the wheel carries on to the page (D100)
  // A reader at the bottom stays there when the transcript changes size (D103): a question card
  // takes space from it, and the view was left above the bottom - so the answer and everything
  // after it arrived out of sight. The scroll event keeps `pinned` true while they are there.
  let pinned = true;
  log.addEventListener('scroll', () => { pinned = atBottom(log); });
  new ResizeObserver(() => { if (pinned) log.scrollTop = log.scrollHeight; }).observe(log);
  // What Claude is doing right now (46), and the way back down once the transcript has stopped
  // following (43, part 2). Both belong under the transcript, which is where the new content is
  // and where the eye is leaving from.
  const doing = h('div', { id: 'session-doing', class: 'hint', hidden: true });
  const background = h('div', { id: 'session-background', hidden: true });
  const toLatest = h('button', { type: 'button', id: 'session-latest', class: 'to-latest', hidden: true },
    'View latest');
  toLatest.addEventListener('click', () => {
    log.scrollTop = log.scrollHeight;
    toLatest.hidden = true;
    log.unseen = 0;
    toLatest.textContent = 'View latest';
  });
  // Shown only while the reader is away from the bottom - while they are at it, following is
  // automatic and a button offering to do what is already happening is noise.
  log.addEventListener('scroll', () => {
    toLatest.hidden = atBottom(log);
    if (toLatest.hidden) { log.unseen = 0; toLatest.textContent = 'View latest'; }
  });
  const list = h('aside', { id: 'session-list', class: 'card tree' });
  const panel = h('aside', { id: 'session-files', class: 'card tree' }, h('p', { class: 'muted' }, 'Loading files...'));
  const cards = h('div', { id: 'session-permissions' });
  const heading = h('h2', { id: 'session-heading' }, view.name || 'No session');
  const clientChip = h('span', { id: 'session-client', class: 'chip' }, '');
  const runningLine = h('div', { class: 'hint', id: 'session-running' }, '');
  // How this session asks before it acts. It was settable only by hand-editing
  // aicleaner-config.json, shown nowhere, and read at proxy construction - so the safest mode was
  // also the only reachable one, and a day was spent approving every action. Changing it here
  // reaches a running session over the control channel, with no restart.
  const modeSelect = h('select', { id: 'session-mode', 'aria-label': 'How it asks before it acts' },
    PERMISSION_MODES.map(([value, label]) => h('option', { value }, label)));
  // A third of the controls row (D62), with what the chosen mode means under it rather than on a
  // row of its own.
  const modeField = h('div', { class: 'field' },
    h('label', { for: 'session-mode' }, 'Asks'), modeSelect,
    h('span', { class: 'hint', id: 'session-mode-why' }, ''));
  const editorSlot = h('div', { id: 'session-editor' });
  // Prompts waiting for the turn to end (D176), above the box they were typed in.
  const queueBox = h('div', { id: 'session-queue', hidden: true });
  const paintQueue = (queued) => {
    repaint(queueBox, queueChips(queued));
    queueBox.hidden = !(queued || []).length;
  };
  queueBox.addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    button.disabled = true;
    try {
      const path = button.dataset.action === 'send' ? '/api/session/queue/send' : '/api/session/queue/cancel';
      const answer = await postJSON(path, { name: view.name, id: button.dataset.id });
      if (answer.outcome === 'interrupting') say('Stopping the current turn, then sending this.', 'ok');
      await refresh();
    } catch (error) {
      button.disabled = false;
      sayError(error);
    }
  });

  const paint = () => {
    // A refresh that lands after the page was left - another page routed in while it was in flight,
    // or the reconnect after a restart (D138) - has nothing to paint, and its elements are gone.
    if (!sessionCard.isConnected) return;
    const state_ = view.state;
    showModelRequest(state_ && state_.model_request);
    const session = current();
    const profile = profileOf(view.name);
    repaint(list, sessionList(state_).filter(Boolean));
    // Each session has its own tabs; redrawn only when the session changes, never on a poll -
    // a PDF frame redrawn every 5 s would reload every 5 s.
    if (view.tabsShownFor !== view.name && view.tabs) {
      view.tabsShownFor = view.name;
      view.tabs.paint();
    }
    // repaint() leaves the list alone while it holds focus (so an open drop-down is not torn
    // down) - and clicking a session button leaves focus on that button, so the highlight stayed on
    // the old one (2026-09-30). The selection is marked directly, whether or not it repainted.
    for (const row of list.querySelectorAll('.session-row')) {
      const on = row.dataset.session === view.name;
      row.classList.toggle('selected', on);
      row.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    heading.textContent = view.name || 'No session';
    const chosenProject = Boolean(view.name);
    nonePicked.hidden = chosenProject;
    for (const part of sessionCard.querySelectorAll(':scope > .session-head, :scope > .session-controls')) {
      part.hidden = !chosenProject;
    }
    resumeLine.hidden = !chosenProject;
    paintWorktree(worktreeSlot, profile, Boolean(session.running), selectSession, refresh);
    // The client this work is for. It is a label on the session, not the scope of the filter:
    // every client's names are replaced in every session.
    // The binding is profile.client, not the label. Reading it off "the label differs from the
    // name" meant a session named after its client - the obvious way to name one - reported
    // "no client", as did any profile with no explicit label, since the server serves
    // `label or name`. The label still decides the wording; it no longer decides the answer.
    clientChip.textContent = !profile.client
      ? 'no client'
      : (profile.label && profile.label !== profile.name ? `for ${profile.label}` : 'client set');
    // A running session shows where it did start; a stopped one, where the next one will.
    const homeFolder = session.running && session.project ? session.project : ((profile.home || {}).folder || '');
    homeShown.textContent = homeFolder || '(no folder yet - add one in Edit project profiles)';
    homeShown.title = homeFolder;
    const drawing = begin('paint.transcript', { events: (session.events || []).length });
    paintLog(log, session.events, view.name);
    markTools(log, session.events, Boolean(session.running));
    drawing.end();
    paintQueue(session.queued);
    showEarlierWhen();
    paintBackground(background, session.events, box);
    // Not rebuilt while unchanged or in use: a half-answered question must survive the poll (D58, D69).
    repaint(cards, permissionCards(state_));
    box.disabled = !session.running;
    // One control, labelled for what it will do - but never relabelled while a start or stop is
    // in flight, or the label would change under the pointer between the request resolving and
    // the refresh landing, which is the mis-click this design has to avoid.
    const run = document.getElementById('session-run');
    if (run && !view.transition) {
      run.textContent = session.running ? 'Stop' : 'Start';
      run.classList.toggle('primary', !session.running);
      run.classList.toggle('danger', session.running);
    }
    // Start chatting only means something while the session is running (DECISIONS 35).
    chat.disabled = !session.running;
    runningLine.textContent = session.running
      ? `running in ${session.project}` : (view.name ? 'not running' : 'nothing selected');
    // Painted from the session's own state, so a mode changed elsewhere - another tab, the
    // config file, a second session - shows here rather than the page keeping its own idea.
    // Skipped while the select has focus, or the poll would move the value under the pointer.
    const mode = session.permission_mode || '';
    if (mode && document.activeElement !== modeSelect) modeSelect.value = mode;
    document.getElementById('session-mode-why').textContent =
      MODE_WHY[modeSelect.value] + (session.running ? '' : ' Applies when a session starts.');
    // The poll repaints every few seconds, so a choice already made has to survive the rebuild.
    const held = resume.value;
    // Said while it loads (D86): a cold list took seconds, and an empty select read as "nothing".
    repaint(resume, view.pastLoading
      ? [h('option', { value: '' }, 'Loading conversations...')]
      : pastOptions(view.past, homeFolder));
    if (held && Array.from(resume.options).some((option) => option.value === held)) resume.value = held;
    resume.disabled = Boolean(session.running || view.pastLoading);
    // Across every folder, not just the selected one: the chosen conversation may belong to
    // another of this profile's folders now that Continue offers them all.
    const picked = (view.past || [])
      .flatMap((entry) => entry.sessions || [])
      .find((row) => row.id === resume.value);
    // Each third is cut to fit, so each closed box carries its whole value on hover (D62).
    resume.title = picked ? picked.title : '';
    modeSelect.title = MODE_WHY[modeSelect.value] || '';
    resumeLine.textContent = picked && picked.busy
      ? 'That transcript was written to minutes ago, so a terminal or an editor may still have it '
        + 'open. Continuing it here puts a second process on the one conversation.'
      : '';
  };

  const refresh = async () => {
    const asked = view.name;
    const wasRunning = view.state && asked ? Boolean(current().running) : null;
    const timing = begin('refresh', { session: asked });
    // Only what this page has not seen (D140): each session's last answer is kept, and the server
    // is told how far that answer covered (`through`) and which buffer it was. Not the newest event
    // the page holds: the stream adds only what it draws, so a notice can be missing below that.
    // The server answers with the newer events (`delta`), or the whole list when there could be a
    // gap. A switch back to a session seen before is a few bytes rather than its whole conversation.
    if (!view.known) view.known = new Map();
    const known = asked ? view.known.get(asked) : null;
    const last = known ? Number(known.through) || 0 : 0;
    const since = known && known.epoch ? `&since=${last}&epoch=${encodeURIComponent(known.epoch)}` : '';
    const answer = await getJSON(`/api/session?name=${encodeURIComponent(view.name)}${since}`);
    if (answer.delta) answer.events = mergeEvents(known ? known.events : [], answer.events || []);
    timing.lap('fetch');
    // A poll answered before an event the stream has already drawn must not take that event away
    // (D111): the log redrew without it, then put it back on the next poll - lines vanished for a
    // few seconds. What the stream added beyond the answer's last event is kept.
    const had = view.state && view.state.selected === answer.selected ? (view.state.events || []) : [];
    const theirs = answer.events || [];
    const lastSeq = theirs.length ? theirs[theirs.length - 1].seq : 0;
    const newer = had.filter((event) => event.seq !== undefined && event.seq > lastSeq);
    if (newer.length) answer.events = [...theirs, ...newer];
    view.state = answer;
    if (answer.selected) view.known.set(answer.selected, answer);
    // The server's own choice fills an empty name, except when the person chose "nothing".
    if (!view.name && !view.noneChosen) view.name = view.state.selected || '';
    paint();
    timing.lap('paint');
    timing.end({ events: (answer.events || []).length });
    // Starting writes a new transcript and stopping finishes one: either way the list moved (D86).
    if (wasRunning !== null && asked === view.name && wasRunning !== Boolean(current().running)) {
      loadPast({ quiet: true }).catch(() => {});
    }
  };

  // One pushed event, applied to the one thing it describes.
  //
  // What the server sends is a complete event, so the page can add it to the transcript it
  // already has. The types that change more than the transcript - a session stopping, a
  // permission arriving - still go the long way round, because they move several parts of the
  // page at once and there is one of them per turn rather than one per fragment (D34).
  // These move more than the transcript - a session ending, a permission arriving or being
  // answered - so they take the long way round. There is one of them per turn, not one per
  // fragment, so the cost that mattered is not in them.
  const RELOADS = new Set(['exited', 'stopped', 'permission', 'permission_timeout',
                           'permission_answered']);
  const DRAWN = new Set(['prompt', 'assistant', 'brief', 'interrupted', 'interrupt_pending', 'interrupt_refused']);

  // What Claude is doing, from the fragments that were being thrown away.
  //
  // Between sending a prompt and the reply landing the page showed nothing at all - not a
  // thinking indicator, not a spinner - because transcript() draws a COMPLETED assistant event,
  // so the first sign of anything was the whole answer arriving at once. The material was already
  // crossing the stream and being dropped (46, and D29 is why it is dropped from the buffer).
  //
  // The tool's NAME only, never its input: an input is a command line or a path, which is exactly
  // the sort of string that carries a client name, and it would reach the screen without going
  // through the restore that transcript text does.
  const sayDoing = (what) => {
    doing.textContent = what || '';
    doing.hidden = !what;
  };

  const noteActivity = (event) => {
    // The CLI wraps the API's own events in `stream_event` with --include-partial-messages; the
    // test stub emits them bare. Handle both rather than depend on which.
    const inner = event.event && typeof event.event === 'object' ? event.event : event;
    const kind = inner.type;
    if (kind === 'content_block_start') {
      const block = inner.content_block || {};
      if (block.type === 'thinking') return sayDoing('thinking...');
      if (block.type === 'tool_use') return sayDoing(`${block.name || 'a tool'}...`);
      if (block.type === 'text') return sayDoing('writing...');
      return;
    }
    // A turn ending is the only reliable "stopped": a delta says nothing about whether more is
    // coming, and guessing from a pause would flicker.
    if (kind === 'result' || kind === 'message_stop' || event.type === 'assistant') sayDoing('');
  };

  const applyEvent = (event) => {
    if (!event) return;
    // The waiting prompts changed (D176): the chips follow; nothing in the transcript does.
    if (event.type === 'queue') {
      const mine = (view.state.sessions || {})[view.name];
      if (mine) mine.queued = event.queued || [];
      paintQueue(event.queued);
      return;
    }
    noteActivity(event);
    // A turn has ended, which is exactly when files have just changed (D33): look at the open
    // folders again. Only open ones are fetched - collapsed roots are not listed (D70) - so this
    // costs a request per open folder, once per turn, not per fragment.
    if (event.type === 'result') { forgetListings(); showTree(); }
    if (event.type === 'interrupted' && event.late) say('The interrupt landed: this turn was stopped.', 'ok');
    if (event.type === 'interrupt_refused') say(`The session refused the interrupt: ${event.error || 'no reason given'}`, 'warn');
    if (RELOADS.has(event.type)) { sayDoing(''); refresh(); return; }
    if (!DRAWN.has(event.type)) return;    // a streaming fragment: nothing drawn depends on it
    if (!view.name || view.state.selected !== view.name) return;
    const events = view.state.events || [];
    // The stream replays what is buffered when it connects, and refresh() has just fetched the
    // same events - so without this every one of them would be appended a second time. seq is
    // assigned once, by _emit, and only ever increases.
    const last = events.length ? events[events.length - 1].seq : 0;
    if (event.seq !== undefined && last !== undefined && event.seq <= last) return;
    view.state.events = [...events, event];
    paintLog(log, view.state.events, view.name);
    markTools(log, view.state.events, Boolean(current().running));
    paintBackground(background, view.state.events, box);
  };

  // A profile with no history, or a machine with no transcripts, is an empty list and not an
  // error: the page still has to start a new conversation.
  // `quiet` is the poll's reload: the list already showing stays until the new one lands.
  const pastCount = (past) => (past || []).reduce((n, entry) => n + (entry.sessions || []).length, 0);
  const loadPast = async ({ quiet = false } = {}) => {
    if (!view.name) { view.past = []; paint(); return; }
    const asked = view.name;
    const all = showAll.querySelector('input').checked;
    if (!quiet) { view.pastLoading = true; paint(); }
    let past = [];
    try {
      past = (await getJSON(`/api/session/past?name=${encodeURIComponent(asked)}${all ? '&all=1' : ''}`)).projects || [];
    } catch (error) { /* an empty list, never an error: a new conversation can still start */ }
    if (asked !== view.name) return;   // a switch overtook it, and loads its own
    view.past = past;
    view.pastLoading = false;
    if (!all) view.pastBase = { name: asked, count: pastCount(past) };
    const base = view.pastBase && view.pastBase.name === asked ? view.pastBase.count : null;
    const extra = base === null ? null : pastCount(past) - base;
    allNote.textContent = !all ? '' : extra === null ? ` (${pastCount(past)} listed)`
      : extra > 0 ? ` (+${extra})` : ' (none extra)';
    paint();
  };

  // A folder listing that cannot change while somebody is typing, so it is fetched once per
  // session and kept.
  const loadCommands = async () => {
    if (!view.name || view.commands) return;
    try {
      view.commands = (await getJSON(`/api/session/commands?name=${encodeURIComponent(view.name)}`)).commands || [];
    } catch (error) {
      view.commands = [];
    }
  };

  const closeSlash = () => { slash.hidden = true; slash.replaceChildren(); view.slashAt = 0; view.slashRows = []; };

  const showSlash = async () => {
    const typed = slashWord(box);
    if (typed === null) { closeSlash(); return; }
    await loadCommands();
    const rows = matching(view.commands, typed);
    if (!rows.length) { closeSlash(); return; }
    if (view.slashAt >= rows.length) view.slashAt = 0;
    view.slashRows = rows;
    // Say how many, so the list is honest about its own length.
    slash.replaceChildren(...slashRows(rows, view.slashAt),
      h('div', { class: 'slash-count muted small' },
        `${rows.length} ${rows.length === 1 ? 'command' : 'commands'}`));
    slash.hidden = false;
  };

  // The name replaces what was typed of it, and a space follows so anything the command takes
  // can just be typed on.
  const takeSlash = (name) => {
    const typed = slashWord(box);
    if (typed === null) { closeSlash(); return; }
    const at = box.selectionStart === null ? box.value.length : box.selectionStart;
    box.value = box.value.slice(0, at - typed.length) + name + ' ' + box.value.slice(at);
    const after = (at - typed.length) + name.length + 1;
    box.focus();
    box.setSelectionRange(after, after);
    closeSlash();
  };

  const showTree = () => {
    const profile = profileOf(view.name);
    const session = current();
    // The primary repo's workspace folders first, as VS Code opens a workspace (D72), then the
    // profile's own; a running session's folders are kept in case they differ.
    const listed = profile.tree || profile.projects || [];
    const extra = session.running ? (session.projects || []).filter((p) => !listed.includes(p)) : [];
    const roots = [...listed, ...extra];
    if (!view.name) {
      panel.replaceChildren(h('h3', {}, 'Files'), h('p', { class: 'muted' }, 'Pick a project to see its files.'));
      return;
    }
    renderTree(panel, view.name, roots, session.project || roots[0], profile.share || '')
      .then(async () => {
        keepMenuInView(panel);
        // A group menu counting files of folders not yet read: read them, then count (feature 97).
        if (await listMenuFolders(panel)) showTree();
        // A remembered folder found unreadable while drawing: the saved list loses it too.
        if (tree.forgotten) { tree.forgotten = false; saveTreeState(); }
      })
      .catch(sayError);
  };

  // The tree opens as it was last left for this client (D73) - the client, not the profile, so
  // two profiles for one engagement share it, as they share the repos. A profile with no client
  // keeps its own. Remembering is best effort: a failed save costs the memory, never the tree.
  const treeKey = () => profileOf(view.name).client || view.name;

  async function loadTreeState() {
    tree.open.clear();
    if (!view.name) return;
    try {
      const saved = await getJSON(`/api/session/tree-state?client=${encodeURIComponent(treeKey())}`);
      for (const key of saved.open || []) tree.open.set(key, true);
    } catch (error) { /* nothing remembered: every root starts collapsed (D70) */ }
  }

  function saveTreeState() {
    if (!view.name) return;
    postJSON('/api/session/tree-state', { client: treeKey(), open: [...tree.open.keys()] }).catch(() => {});
  }

  const selectSession = async (name) => {
    // Each step of a switch, timed while Diagnostics records (feature 91).
    const switching = begin('switch', { to: name });
    saveDraft();                        // what was typed here stays with this session
    view.name = name;
    view.noneChosen = !name;
    rememberSession(name);
    // The address follows the session shown, so a notice naming it is a change of address only
    // when it names another one (D118). Replaced, not pushed: switching is not a page to go back to.
    noteAddress(name ? `#/session/${encodeURIComponent(name)}` : '#/session');
    loadDraft();
    tree.open.clear();
    clearSelection();
    Object.assign(tree, { searchOpen: false, query: '', found: null });   // another session's folders (feature 90)
    forgetListings();
    switching.lap('prepare');
    await loadTreeState();
    switching.lap('tree-state');
    view.commands = null;               // another profile, another set of project folders
    closeSlash();
    await refresh();
    switching.lap('refresh');
    showTree();
    switching.lap('tree');
    await loadPast();
    switching.lap('past');
    subscribe(current().running ? name : '', refresh, applyEvent);
    switching.end({ events: (view.state && view.state.events || []).length });
  };

  // Back to the working band on request (DECISIONS 35): what Start does on its own, for a session
  // already running - arriving on the page never scrolls, so this is the way down. Enabled only
  // while the selected session is running; there is nothing to chat to otherwise.
  const chat = h('button', { type: 'button', id: 'session-chat', disabled: true }, 'Start chatting');
  chat.addEventListener('click', () => {
    document.getElementById('session-band').scrollIntoView({ block: 'start', behavior: 'smooth' });
    box.focus({ preventScroll: true });
  });
  // Nothing picked: the card says what the picker says, instead of controls for no project (the
  // user, 2026-10-05). Toggled in paint().
  const nonePicked = h('p', { class: 'muted session-none', hidden: true }, PICK_PROMPT);
  // Create a worktree; Merge and Remove on a stopped worktree session (DECISIONS 66). Painted in paint().
  const worktreeSlot = h('div', { class: 'worktree-slot' });
  const sessionCard = h('section', { class: 'card session-card', dataset: { card: 'session' } },
    nonePicked,
    // Two rows where there were five (D62): who and whether it is running, with Start; then
    // Continue | Folder | Asks in thirds.
    h('div', { class: 'form-row session-head' }, heading, clientChip, runningLine,
      h('button', { type: 'button', id: 'session-run', class: 'primary' }, 'Start'), chat),
    h('div', { class: 'session-controls' },
      h('label', { class: 'field' }, 'Continue', resume, showAll),
      h('div', { class: 'field' }, h('span', {}, 'Starts in (home folder)'), homeShown),
      modeField),
    resumeLine,
    worktreeSlot);
  view.nonePicked = nonePicked;
  // Autocorrect (feature 94): its green marks sit in a layer behind the box, and its on/off button
  // to the left of the link indicator (the user, 2026-10-08).
  const autocorrect = attachAutocorrect(box, { conversation: () => log.textContent || '' });
  view.autocorrect = autocorrect;
  const promptPane = h('section', { class: 'card', id: 'prompt-pane' },
    queueBox,
    autocorrect.stack,
    slash,
    h('div', { class: 'form-row' },
      h('button', { type: 'button', id: 'session-send', class: 'primary' }, 'Send'),
      autocorrect.toggle,
      // Beside Send, because that is where the eye already is when waiting for a reply,
      // and waiting is when "is this thing still connected?" gets asked (feature 65).
      h('span', { id: 'session-link', class: 'link-state', hidden: true })),
    // The model request itself (DECISIONS 47): "Connected" only ever meant the page could reach
    // aicleaner, and stayed green through an 82-minute stall.
    h('div', { id: 'session-model', class: 'model-state', hidden: true },
      h('span', { class: 'model-state-text' }),
      h('button', { type: 'button', class: 'model-state-stop', hidden: true }, 'Interrupt')));
  const filesBar = h('div', { id: 'files-resize', class: 'drag-bar x', role: 'separator',
    'aria-orientation': 'vertical', title: 'Drag to change the width of the files' });
  const promptBar = h('div', { id: 'prompt-resize', class: 'drag-bar y', role: 'separator',
    'aria-orientation': 'horizontal', title: 'Drag to change the height of the prompt' });
  // The file browser opens and closes from one toggle at the band's top edge, on every screen size
  // (the user, 2026-10-01; it was a phone's alone - DECISIONS 30, 35). It starts as it was last
  // left; the first time, open where there is room and closed on a phone.
  const PHONE = '(max-width: 40rem)';
  const FILES_OPEN = 'aicleaner.files.open';
  let filesOpen = !window.matchMedia(PHONE).matches;
  try {
    const kept = localStorage.getItem(FILES_OPEN);
    if (kept !== null) filesOpen = kept === '1';
  } catch (error) { /* storage blocked: the default stands */ }
  // The label says what a click does, not what is showing (the user, 2026-10-01): "<< Files"
  // folds the panel away, "Files >>" brings it back. aria-expanded carries the state for a reader.
  const railLabel = (open) => (open ? '<< Files' : 'Files >>');
  const railTitle = (open) => (open ? 'Hide the files' : 'Show the files');
  const rail = h('button', { type: 'button', id: 'session-rail', class: 'rail-toggle',
    'aria-expanded': String(filesOpen), title: railTitle(filesOpen) }, railLabel(filesOpen));
  // The page, top to bottom (DECISIONS 35, D76): the session selector and its controls; the working
  // band, exactly one viewport tall, holding only the files, the transcript and the prompt (with
  // what Claude is doing, and any question waiting on you, inside it); and tool usage below.
  // Start scrolls the band into view. Scroll up to stop, down for tool usage.
  const top = h('div', { class: 'session-top' }, list, sessionCard);
  // Between the transcript and the prompt (the user, 2026-09-30): what was started in the
  // background, with Claude's questions and permission requests to its right.
  // File tabs over the transcript (DECISIONS 50): the conversation is the first tab; a file tab
  // shows in the transcript's place while the prompt stays below it.
  const stream = h('div', { class: 'session-stream' }, log, h('div', { class: 'session-underlog' }, doing, toLatest));
  const tabStrip = h('div', { id: 'file-tabs', class: 'file-tabs', role: 'tablist', hidden: true });
  const filePane = h('div', { id: 'file-pane', class: 'file-pane', hidden: true });
  const tabs = fileTabs({ strip: tabStrip, pane: filePane, stream, name: () => view.name });
  if (view.tabs) view.tabs.stop();
  view.tabs = tabs;
  // The 15-second check, on demand, for tests - the same hook style as the stream's.
  window.__aicleanerFileCheck = () => tabs.check();
  const work = h('div', { class: 'session-work' },
    tabStrip,
    stream,
    filePane,
    h('div', { class: 'session-mid' }, background, cards),
    promptBar,
    promptPane);
  const band = h('div', { class: `session-band${filesOpen ? ' rail-open' : ''}`, id: 'session-band' },
    rail, panel, filesBar, work);
  // The profile editor opens between the session list and the band (the user, 2026-10-04): at the
  // foot of the page, below a band a screen tall, it was easy never to see it open at all.
  const shell = h('div', { class: 'session-shell' }, top, editorSlot, band);
  const setFilesOpen = (open, remember) => {
    band.classList.toggle('rail-open', open);
    rail.setAttribute('aria-expanded', String(open));
    rail.textContent = railLabel(open);
    rail.title = railTitle(open);
    if (!remember) return;
    try { localStorage.setItem(FILES_OPEN, open ? '1' : '0'); } catch (error) { /* not remembered */ }
  };
  rail.addEventListener('click', () => setFilesOpen(!band.classList.contains('rail-open'), true));
  pane(band, { key: 'aicleaner.files.width', variable: '--files-w', min: 200, maxShare: 0.5, axis: 'x' })
    .bar(filesBar, () => panel.getBoundingClientRect().width);
  pane(band, { key: 'aicleaner.prompt.height', variable: '--prompt-h', min: 90, maxShare: 0.5, axis: 'y', invert: true })
    .bar(promptBar, () => promptPane.getBoundingClientRect().height);

  setChildren(main,
    h('h1', {}, 'Project Sessions'),
    h('p', { class: 'muted' },
      'Claude Code runs on this machine and its traffic goes back through the proxy, so what you type here is filtered like anything else. Naming a session says whose work it is, not whose names may travel: every client\'s names are replaced in every session.'),
    state.available ? null : h('p', { class: 'chip warn' }, 'The Claude Code CLI was not found on this machine.'),
    // The tree moves out of the transcript's column and under the session list (42).
    //
    // It was taking 14-22rem out of the one pane that needs width, while the space below the
    // session list - capped at 32rem, aligned to the start - sat empty for the rest of the page.
    // The transcript now gets the whole of .session-main, and `panel` is already an <aside>, so
    // beside the session list is the more honest place for it as well as the roomier one.
    // The app layout (51): sessions and files on the left, a drag bar, then the session panel,
    // the conversation taking whatever is left, a second drag bar, and the prompt at the bottom.
    // Background jobs go below the prompt - they only matter when something has gone wrong, and
    // between the conversation and the prompt they interrupted the thing people use.
    shell);

  paint();
  loadTreeState().finally(showTree);
  loadPast().catch(() => {});
  showAll.querySelector('input').addEventListener('change', () => { loadPast().catch(() => {}); });
  // Picking a conversation no longer moves a Folder choice: every session starts in the home, and
  // a conversation filed elsewhere is found where it is (DECISIONS 64).
  resume.addEventListener('change', paint);

  // What Send does, so the key and the button cannot drift apart (feature 48).
  async function sendPrompt() {
    const text = box.value.trim();
    if (!text) return;
    const sentFrom = view.name;
    try {
      await postJSON('/api/session/prompt', { name: sentFrom, text });
      if (view.name === sentFrom) { box.value = ''; if (view.autocorrect) view.autocorrect.clear(); }
      saveDraft(sentFrom);
      view.historyAt = null;
      await refresh();
    } catch (error) {
      sayError(error);
    } finally {
      // Back where the typing was. Inserting a path and picking a slash command both do this
      // already; Send was the one control that left the cursor on itself, so continuing the
      // conversation meant reaching for the mouse. On the error path too: a failure is more
      // reason to leave the cursor in the text, not less.
      box.focus();
    }
  }

  // Up and Down walk what was sent before (feature 69), as the terminal does. Read from this
  // session's own transcript, not a store of its own (DECISIONS 34): a prompt is client text, and
  // this keeps it on the proxy's machine rather than at rest in the browser. Up only from the
  // first line and Down only from the last, so moving the caret inside a long prompt still works;
  // the half-typed draft comes back at the bottom. Returns true when it took the key.
  function promptHistory() {
    const sent = (current().events || []).filter((e) => e.type === 'prompt' && e.text).map((e) => e.text);
    return sent.filter((text, i) => text !== sent[i - 1]);        // a prompt sent twice in a row, once
  }

  function walkHistory(step) {
    // From the first row as laid out (Up) or the last (Down), not merely with no line break in the
    // way: a long wrapped paragraph has none, and Up in its middle recalled history (2026-10-01).
    if (step < 0 && !onFirstRow(box)) return false;
    if (step > 0 && !onLastRow(box)) return false;
    const history = promptHistory();
    if (view.historyAt === null || view.historyAt === undefined) {
      if (step > 0 || !history.length) return false;
      view.historyDraft = box.value;
      view.historyAt = history.length;
    }
    const next = view.historyAt + step;
    if (next < 0) return true;                                      // at the oldest: stay there
    if (next >= history.length) {
      box.value = view.historyDraft || '';
      view.historyAt = null;
    } else {
      box.value = history[next];
      view.historyAt = next;
    }
    box.setSelectionRange(box.value.length, box.value.length);
    return true;
  }

  function insertNewline() {
    // By hand, because preventDefault has already stopped the textarea doing it. setRangeText
    // keeps this in the browser's own undo stack, which rebuilding value does not.
    const at = box.selectionStart;
    box.setRangeText('\n', at, box.selectionEnd, 'end');
    box.dispatchEvent(new Event('input', { bubbles: true }));
  }

  async function interruptTurn() {
    // Escape stops the turn and keeps the session. Not Stop, which ends it: the conversation,
    // its context and its permission grants all survive an interrupt, and none of them survive
    // Stop. On a session that is not running there is nothing to interrupt and saying so would
    // be noise, so it does nothing.
    if (!current().running) return;
    try {
      const answer = await postJSON('/api/session/interrupt', { name: view.name });
      if (answer && answer.state === 'pending') {
        say('Interrupt sent. The session is busy and will stop when it next reads its input.', 'info');
      } else {
        say('Stopped this turn. The session is still running.', 'ok');
      }
      await refresh();
    } catch (error) {
      sayError(error);
    }
  }

  // The same interrupt, offered where a stuck request is reported (DECISIONS 47).
  document.querySelector('#session-model .model-state-stop').addEventListener('click', interruptTurn);


  modeSelect.addEventListener('change', async () => {
    const wanted = modeSelect.value;
    modeSelect.disabled = true;
    try {
      const answer = await postJSON('/api/session/permission-mode', { name: view.name, mode: wanted });
      // "live" says whether a running session took it now or it was only remembered for the next
      // one. Saying which matters: the two look identical on the page and are not the same thing.
      say(answer.live ? `Now ${MODE_WHY[wanted].toLowerCase()}`
                      : `Saved. Takes effect when a session starts.`, 'ok');
      await refresh();
    } catch (error) {
      sayError(error);
      await refresh();          // put the control back to what is actually in force
    } finally {
      modeSelect.disabled = false;
    }
  });

  // Typing makes what is in the box yours again: Up starts from it next time (69). Only a person
  // raises `input`; putting a recalled prompt in the box does not.
  box.addEventListener('input', () => { view.historyAt = null; saveDraft(); showSlash().catch(() => {}); });
  box.addEventListener('blur', () => { setTimeout(closeSlash, 150); });   // after a click lands
  box.addEventListener('keydown', (event) => {
    // Layered, and the order matters (feature 48). While the slash picker is open it owns
    // Enter, Escape and the arrows; with it closed the same keys mean what they mean everywhere
    // else. Before this there was no keyboard path to send at all - Enter inserted a newline and
    // the only way out of the box was the mouse.
    if (slash.hidden) {
      if (event.key === 'Enter' && !event.ctrlKey && !event.shiftKey && !event.altKey && !event.metaKey) {
        event.preventDefault();
        sendPrompt();
        return;
      }
      // Ctrl+Enter and Shift+Enter both insert a newline. Two conventions, both common, and
      // supporting both costs nothing - a person should not have to learn which one this box
      // chose. The textarea already does this for Shift+Enter, so only Ctrl needs help.
      if (event.key === 'Enter' && event.ctrlKey && !event.altKey && !event.metaKey) {
        event.preventDefault();
        insertNewline();
        return;
      }
      if (event.key === 'Escape') { event.preventDefault(); interruptTurn(); }
      if ((event.key === 'ArrowUp' || event.key === 'ArrowDown') && !event.altKey && !event.ctrlKey
          && !event.metaKey && !event.shiftKey && walkHistory(event.key === 'ArrowUp' ? -1 : 1)) {
        event.preventDefault();
      }
      return;
    }
    const rows = view.slashRows || [];
    if (event.key === 'Escape') { closeSlash(); event.preventDefault(); return; }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (!rows.length) return;
      view.slashAt = (view.slashAt + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
      slash.replaceChildren(...slashRows(rows, view.slashAt));
      // The box has max-height and scrolls, but every keypress rebuilds all the rows, which
      // resets scrollTop - so without this, removing the cap makes things worse: the selection
      // walks off the bottom of a box that never follows it.
      const picked = slash.children[view.slashAt];
      if (picked) picked.scrollIntoView({ block: 'nearest' });
      event.preventDefault();
      return;
    }
    if (event.key === 'Enter' || event.key === 'Tab') {
      const row = rows[view.slashAt];
      if (row) { takeSlash(row.name); event.preventDefault(); }
    }
  });
  slash.addEventListener('click', (event) => {
    const row = event.target.closest('button.slash-row');
    if (row) takeSlash(row.dataset.name);
  });
  if (current().running) subscribe(view.name, refresh, applyEvent);
  // Asked for its chat - a notice's Open, a running session in the menu (the user, 2026-10-06):
  // down to the conversation as "Start chatting" does. Only when asked: arriving on the page
  // otherwise does not scroll (DECISIONS 35). The address drops `/chat`, so a refresh does not.
  if (view.name) noteAddress(`#/session/${encodeURIComponent(view.name)}`);
  if (toChat && view.name) {
    requestAnimationFrame(() => {
      document.getElementById('session-band').scrollIntoView({ block: 'start', behavior: 'smooth' });
      if (!box.disabled) box.focus({ preventScroll: true });
    });
  }
  // Another session's question must not wait for the selected session to say something, so the
  // page asks periodically as well as listening to the stream it is watching.
  // Every sixth tick, while the session is idle, the Continue list too: a conversation started in
  // a terminal meanwhile shows up without a reload (D86). Not while it runs - Continue is disabled.
  view.pollTicks = 0;
  // Not while aicleaner is not answering (D138): the link's probe asks instead, and the page is
  // refreshed the moment it is back.
  if (view.offConnection) view.offConnection();
  view.offConnection = onConnection((up) => {
    if (isSealed()) setLink(up ? 'live' : 'retrying');
    if (up) refresh().catch(() => {});
  });
  // Through the tunnel there is no event stream to say "Connected" (D167): each poll does instead -
  // green when it was answered, amber when it was not.
  const polled = (asked) => {
    if (isSealed()) asked.then(() => setLink('live'), () => setLink('retrying'));
    asked.catch(() => {});
  };
  if (isSealed() && view.name) polled(refresh());
  view.poll = setInterval(() => {
    if (!connected()) { if (isSealed()) setLink('down'); return; }
    polled(refresh());
    view.pollTicks += 1;
    if (view.pollTicks % 6 === 0 && view.name && !current().running) loadPast({ quiet: true }).catch(() => {});
  }, isSealed() ? 2000 : 5000);         // sealed: polling is the stream (feature 21)

  list.addEventListener('change', async (event) => {
    if (!event.target.closest('#session-pick')) return;
    // "Pick a Project to chat about..." clears the card and the band (the user, 2026-10-05): it
    // used to be ignored, leaving the last project's details showing.
    await selectSession(event.target.value).catch(sayError);
  });

  list.addEventListener('click', async (event) => {
    const edit = event.target.closest('#session-edit-profiles');
    if (edit) {
      // Closing the editor drops what was not saved: ask first (D98).
      if (view.editing && !(await confirmLeave())) return;
      view.editing = !view.editing;
      if (view.editing) {
        try {
          view.profiles = await getJSON('/api/session/profiles');
          editorSlot.replaceChildren(profileEditor(view.profiles));
          // Each tab is its own form: saving one leaves another's unsaved edits still counted (D98).
          for (const row of profileRows()) track(row, () => readProfile(row));
          const names = profileRows().map((row) => row.dataset.was);
          const back = names.indexOf(view.profileTab);
          showTab(back >= 0 ? back : Math.max(0, names.indexOf(view.name)));
        } catch (error) { sayError(error); view.editing = false; }
      } else {
        editorSlot.replaceChildren();
      }
      paint();
      return;
    }
    const row = event.target.closest('button.session-row');
    if (row) await selectSession(row.dataset.session).catch(sayError);
  });

  editorSlot.addEventListener('click', async (event) => {
    const rows = document.getElementById('profile-rows');
    if (!rows) return;
    const tab = event.target.closest('#profile-tabs .p-tab');
    if (tab && tab.classList.contains('p-tab-add')) { document.getElementById('profile-add').click(); return; }
    if (tab) { showTab(Number(tab.dataset.index)); return; }
    if (event.target.closest('#profile-close')) {
      document.getElementById('session-edit-profiles').click();
      return;
    }
    if (event.target.closest('#profile-add')) {
      const added = profileRow({}, (view.profiles || {}).clients || []);
      rows.append(added);
      if (view.modelChoices) modelPick(added, view.modelChoices.models, view.modelChoices.aliases);
      track(added, () => readProfile(added));
      showTab(profileRows().length - 1);
      added.querySelector('.p-name').focus();
      return;
    }
    // Save and Delete act on the open tab only (the user, 2026-10-04). The other profiles go back
    // as they are saved on disk, whatever is typed in their tabs, so nothing unsaved rides along.
    const active = activeRow();
    if (!active) return;
    const saved = (view.profiles || {}).profiles || [];
    const was = active.dataset.was;
    const commit = async (list, done) => {
      const answer = await postJSON('/api/session/profiles', { profiles: list });
      view.profiles = await getJSON('/api/session/profiles');
      say(done, 'ok');
      for (const note of (answer && answer.notes) || []) say(note);
      await refresh();
    };
    if (event.target.closest('#profile-save')) {
      const mine = readProfile(active);
      try {
        await commit(was ? saved.map((p) => (p.name === was ? mine : p)) : [...saved, mine],
          `Project profile ${mine.name} saved.`);
        active.dataset.was = mine.name;
        rebase(active);                 // saved: nothing to lose on leaving this tab now (D98)
        hintsFor(active, (view.profiles.workspace || {})[mine.name] || null);
        showTab(profileRows().indexOf(active));
      } catch (error) { sayError(error); }
      return;
    }
    const remove = event.target.closest('#profile-delete');
    if (remove) {
      // Two clicks, the second within five seconds, so a stray one cannot delete a profile.
      if (!remove.classList.contains('armed')) {
        remove.classList.add('armed');
        remove.append(h('span', { class: 'p-delete-ask' }, ` Delete ${tabLabel(active)}? Click again`));
        setTimeout(() => {
          remove.classList.remove('armed');
          for (const ask of remove.querySelectorAll('.p-delete-ask')) ask.remove();
        }, 5000);
        return;
      }
      remove.classList.remove('armed');
      for (const ask of remove.querySelectorAll('.p-delete-ask')) ask.remove();
      const index = profileRows().indexOf(active);
      try {
        if (was) await commit(saved.filter((p) => p.name !== was), `Project profile ${was} deleted.`);
        forget(active);
        active.remove();
        showTab(Math.max(0, index - 1));
      } catch (error) { sayError(error); }
    }
  });

  // One control. Two buttons meant one of them was always meaningless, and stopping an already
  // stopped session still reported success. The settle applies only on the way into Stop: a stray
  // second click there kills the session that just started, whereas the other way only restarts.
  busyButton(document.getElementById('session-run'), async () => {
    const running = current().running;
    view.transition = true;               // paint() must not relabel mid-flight
    try {
      if (running) {
        await postJSON('/api/session/stop', { name: view.name });
        say(`${view.name} stopped.`, 'ok');
        await refresh();
      } else {
        await postJSON('/api/session/start',
          { name: view.name, resume: resume.value });
        say(resume.value ? `${view.name} picked up where it left off.` : `${view.name} started.`, 'ok');
        await refresh();
        subscribe(view.name, refresh, applyEvent);
        forgetListings();      // a resumed session may have written files since this was listed
        showTree();
        // Start, and only Start, brings the working band into view (DECISIONS 35). Arriving on
        // the page with a session running does not scroll: whoever came to stop it would be
        // chasing the Stop button up the screen.
        document.getElementById('session-band').scrollIntoView({ block: 'start', behavior: 'smooth' });
        box.focus({ preventScroll: true });
      }
    } catch (error) {
      sayError(error);
    } finally {
      view.transition = false;
    }
  }, { busyLabel: 'Working...', settle: 600 });
  document.getElementById('session-send').addEventListener('click', () => { sendPrompt(); });
  // On this render's shell, not on `main`: `main` outlives the page, so a listener added to it on
  // every visit piled up, and one click on Answer sent the answer once per visit - the second
  // refused with "that request is not waiting for an answer" (2026-09-30). Each request is also
  // answered at most once from this page, and its buttons are disabled while it is sent.
  const answering = new Set();
  const answerOnce = async (id, card, body) => {
    if (answering.has(id)) return;
    answering.add(id);
    for (const b of card ? card.querySelectorAll('button') : []) b.disabled = true;
    try {
      const result = await postJSON('/api/session/permission', { id, name: view.name, ...body });
      // The question closed before the answer arrived (the session restarted, say): Claude has
      // moved on, so it goes in the transcript and in the prompt box, ready to send - never lost.
      if (result && result.late && body.answers) {
        const text = Object.entries(body.answers).map(([q, a]) => `${q}\n${a}`).join('\n\n');
        box.value = box.value.trim() ? `${box.value.trimEnd()}\n\n${text}` : text;
        saveDraft();
        say('That question had already closed, so Claude did not get your answer. It is in the transcript and in the prompt box, ready to send.');
      }
      await refresh();
      // Answered: what comes next is at the bottom, so that is where the transcript goes (D103).
      log.scrollTop = log.scrollHeight;
      toLatest.hidden = true;
      log.unseen = 0;
    } catch (error) {
      answering.delete(id);
      for (const b of card ? card.querySelectorAll('button') : []) b.disabled = false;
      sayError(error);
    }
  };
  shell.addEventListener('click', async (event) => {
    const answer = event.target.closest('button.answer-question');
    if (answer) {
      const card = answer.closest('.question-card');
      const answers = answersIn(card);
      if (!answers) { say('Choose an answer for every question, or write one under Other.'); return; }
      await answerOnce(answer.dataset.id, card, { allow: true, answers });
      return;
    }
    const button = event.target.closest('button.allow, button.deny');
    if (!button) return;
    await answerOnce(button.dataset.id, button.closest('.card'), { allow: button.classList.contains('allow') });
  });
  // Each viewable file in a tab - asking first above MANY_TABS - and saying what was left out.
  const viewMany = async (items) => {
    for (const folder of items.filter((i) => i.dir && ownFiles(i) === null)) {
      try { await listing(view.name, folder.root, folder.path); } catch (error) { sayError(error); }
    }
    const { files, skipped } = viewableFiles(items, (i) => ownFiles(i) || []);
    if (!files.length) { say('Nothing there can be shown in a tab.', 'warn'); return; }
    if (files.length > MANY_TABS && !window.confirm(`Open ${files.length} tabs?`)) return;
    for (const file of files) tabs.open(file.root, file.path, file.name, file.view);
    if (skipped) say(`${skipped} cannot be shown in a tab - left out.`);
    if (window.matchMedia(PHONE).matches && band.classList.contains('rail-open')) setFilesOpen(false, false);
  };
  // A few files one by one; more, or any folder, as one zip that keeps each file under its folders.
  const downloadMany = async (items) => {
    // Through the tunnel the links below are caught and fetched sealed, and fetch() is sealed (D168).
    const save = (href, filename = '') => {
      const link = h('a', { href, download: filename, hidden: true });
      document.body.append(link);
      link.click();
      link.remove();
    };
    if (!needsZip(items)) {
      for (const [i, file] of items.entries()) {
        setTimeout(() => save(`/api/session/file/download?${new URLSearchParams({ name: view.name, root: file.root, path: file.path })}`), i * 400);
      }
      return;
    }
    say(`Making a zip of ${items.length} item(s)...`);
    try {
      const response = await fetch('/api/session/files/zip', { method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/zip, application/json' },
        body: JSON.stringify({ name: view.name, items: items.map((i) => ({ root: i.root, path: i.path })) }) });
      if (!response.ok) {
        const answer = await response.json().catch(() => ({}));
        throw new Error(answer.error || `The zip could not be made (HTTP ${response.status})`);
      }
      const named = /filename\*=UTF-8''([^;]+)/.exec(response.headers.get('Content-Disposition') || '');
      const url = URL.createObjectURL(await response.blob());
      save(url, named ? decodeURIComponent(named[1]) : 'files.zip');
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (error) { sayError(error); }
  };
  panel.addEventListener('click', async (event) => {
    const insert = event.target.closest('button.tree-insert');
    if (insert) { insertAtCursor(box, insert.dataset.insert); tree.menu = null; showTree(); return; }
    const viewButton = event.target.closest('button.tree-view');
    if (viewButton) {
      const d = viewButton.dataset;
      tree.menu = null;
      showTree();
      tabs.open(d.root, d.path, d.name, d.view);
      // On a phone the Files panel covers the band; the file goes where the transcript is, so the
      // panel is put away to show it - for this file only, not remembered. Beside it on a wider
      // screen, the panel stays as it was.
      if (window.matchMedia(PHONE).matches && band.classList.contains('rail-open')) setFilesOpen(false, false);
      return;
    }
    // A group menu's View and Download (feature 97): for the selection, or the one folder.
    const many = event.target.closest('button.tree-view-many, button.tree-download-many');
    if (many) {
      const own = many.closest('.tree-row, .tree-root').querySelector('button.tree-name');
      const items = menuItems(itemOf(own));
      tree.menu = null;
      showTree();
      if (many.classList.contains('tree-view-many')) await viewMany(items);
      else await downloadMany(items);
      return;
    }
    if (event.target.closest('.tree-menu')) return;
    // The triangle opens or closes its folder and does nothing else - no actions menu.
    const toggle = event.target.closest('button.tree-toggle');
    if (toggle) {
      const key = `${toggle.dataset.root}|${toggle.dataset.path}`;
      if (tree.open.has(key)) { tree.open.delete(key); } else { tree.open.set(key, true); }
      saveTreeState();
      showTree();
      return;
    }
    const name = event.target.closest('button.tree-name');
    if (!name) {
      if (tree.menu || clearSelection()) { tree.menu = null; showTree(); }
      return;
    }
    // Where in the row it was tapped, so the menu opens there (D143): a long path scrolled to the
    // right had its menu at the row's left edge, out of view.
    const row = name.closest('.tree-row, .tree-root');
    tree.menuAt = row ? Math.max(0, event.clientX - row.getBoundingClientRect().left - 12) : 0;
    // Shift or Ctrl/Cmd builds a selection (feature 97) and never opens or closes a folder. Search
    // results carry no folder of their own (`parent`), so they keep the one-item click.
    const item = itemOf(name);
    if (item.parent && (event.shiftKey || event.ctrlKey || event.metaKey)) {
      let at = item.full;
      if (event.shiftKey) selectRange(panel, item);
      else if (!toggleOne(item)) {
        // Taken out: the menu stays, on the last of what is still chosen.
        const left = selectedItems();
        at = left.length ? left[left.length - 1].full : null;
      }
      tree.menu = at;
      showTree();
      return;
    }
    if (item.parent) selectOnly(item);
    // Tapping a name shows its menu, and tapping it again hides it; a folder also opens or closes.
    tree.menu = tree.menu === name.dataset.full ? null : name.dataset.full;
    if (name.classList.contains('dir')) {
      const key = `${name.dataset.root}|${name.dataset.path}`;
      if (tree.open.has(key)) { tree.open.delete(key); } else { tree.open.set(key, true); }
      saveTreeState();
    }
    showTree();
  });
  // Right-click opens the same menu as a click - on a selected item, the selection's - without
  // opening or closing a folder, and in place of the browser's own menu (feature 97).
  panel.addEventListener('contextmenu', (event) => {
    const name = event.target.closest('button.tree-name');
    if (!name || name.disabled) return;
    event.preventDefault();
    const item = itemOf(name);
    if (item.parent && !isSelected(item.full)) selectOnly(item);
    const row = name.closest('.tree-row, .tree-root');
    tree.menuAt = row ? Math.max(0, event.clientX - row.getBoundingClientRect().left - 12) : 0;
    tree.menu = item.full;
    showTree();
  });
  // At page level, once: the tree redraws when a menu opens, so focus rarely stays in the panel.
  // It calls whichever render of the page is current - leaving and returning builds a new one.
  tree.redraw = showTree;
  if (!tree.escapeHooked) {
    tree.escapeHooked = true;
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !(tree.menu || clearSelection())) return;
      tree.menu = null;
      if (tree.redraw) tree.redraw();
    });
  }
}

// Which session this page is showing, so a notice about it is not raised while you watch it (44).
export function currentSession() {
  return view.name || '';
}

export function leave() {
  if (view.tabs) view.tabs.stop();
  view.tabsShownFor = undefined;
  if (view.stream) view.stream.close();
  view.stream = null;
  if (view.poll) clearInterval(view.poll);
  view.poll = null;
  if (view.offConnection) view.offConnection();
  view.offConnection = null;
  view.editing = false;
}
