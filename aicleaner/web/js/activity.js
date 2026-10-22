// Work that finishes without being watched says so (feature 44; DECISIONS 27-28).
//
// A Session turn ending, a client scan landing, and a Session waiting on you are announced when
// they happen somewhere you are not looking: a toast on whatever page is open, and an entry in the
// status stack that stays until you go and look. The server only counts (turns finished, questions
// waiting, the scan job's state); what changed is worked out here by comparing one poll with the
// next. A tab in the background does not poll, so something that lands while the phone is locked
// is announced the next time the page is in front - which is when it can be read.
//
// "Somewhere you are not looking" is the page, not the session: being on the Session page for one
// session does not count as watching another.

import { postJSON, say } from './api.js';

const notices = new Map();   // key -> { level, text, href } - kept until its page is visited
let previous = null;
// Shared between computers (D200, Brad 2026-10-22: "if I respond to one at work, the alert is still there
// when I get home"): looking at a session tells aicleaner, every page's next poll carries `seen_turns`, and
// an alert someone has seen anywhere goes everywhere. A question answered anywhere drops `waiting` to 0.
const reported = new Map();  // session -> the turn count already told to aicleaner as seen
const heldFinish = new Set(); // sessions whose turn ended while background work still ran (D202)

function drop(key) {
  const notice = notices.get(key);
  if (!notice) return;
  notices.delete(key);
  for (const toast of document.querySelectorAll('#toasts .toast')) {
    if (toast.dataset.text === notice.text) toast.remove();
  }
}

// The session the notice is about (D118). It was the page alone - "#/session" - so Open went to
// the page without saying which session, and did nothing at all when that page was already open.
function sessionHref(name) {
  // `/chat`: straight down to its conversation, as "Start chatting" does (the user, 2026-10-06).
  return `#/session/${encodeURIComponent(name)}/chat`;
}

function watchingSession(route, shown, name) {
  return !document.hidden && route && route.name === 'session' && shown === name;
}

function watchingScan(route, client) {
  return !document.hidden && route && route.name === 'clients' && (!client || route.id === client);
}

function announce(key, level, text, href) {
  notices.set(key, { level, text, href });
  say(text, level === 'amber' ? 'warn' : 'ok', { action: { label: 'Open', run: () => { location.hash = href.slice(1); } } });
}

// What changed between the last poll and this one, announced. `shown` is the session the Session
// page is showing, when it is the page open.
export function noticeActivity(activity, route, shown) {
  const now = activity || { sessions: [], scan: null };
  if (previous) {
    const before = new Map((previous.sessions || []).map((s) => [s.name, s]));
    for (const s of now.sessions || []) {
      const was = before.get(s.name) || { turns: 0, waiting: 0 };
      const here = watchingSession(route, shown, s.name);
      const seenElsewhere = (s.seen_turns || 0) >= s.turns;
      // A turn that ends with work still running in the background is not the end (D202): the task's result
      // starts the agent again. The alert is held until nothing runs and no turn is in progress.
      if (s.turns > was.turns) heldFinish.add(s.name);
      if (heldFinish.has(s.name) && !(s.background > 0) && !s.busy) {
        heldFinish.delete(s.name);
        if (!here && !seenElsewhere) {
          announce(`turn:${s.name}`, 'green', `${s.name}: Claude has finished`, sessionHref(s.name));
        }
      }
      if (s.waiting > 0 && s.waiting > was.waiting && !here && (s.seen_waiting || 0) < s.waiting) {
        announce(`wait:${s.name}`, 'amber', `${s.name} is waiting on you`, sessionHref(s.name));
      }
    }
  }
  // Cleared wherever it was dealt with (D200): looked at on any computer, or the question answered.
  for (const s of now.sessions || []) {
    if ((s.seen_turns || 0) >= s.turns) drop(`turn:${s.name}`);
    if (!s.waiting || (s.seen_waiting || 0) >= s.waiting) drop(`wait:${s.name}`);
  }
  if (previous) {
    const was = previous.scan;
    const scan = now.scan;
    if (scan && was && scan.id === was.id && was.state === 'running' && scan.state !== 'running'
        && !watchingScan(route, scan.client)) {
      const whose = scan.client || 'the folder';
      announce(`scan:${scan.id}`, scan.state === 'done' ? 'green' : 'amber',
        scan.state === 'done' ? `Scan of ${whose} finished` : `Scan of ${whose} failed`,
        scan.client ? `#/clients/${encodeURIComponent(scan.client)}` : '#/clients');
    }
  }
  previous = now;
}

// Visiting what a notice points at is looking at it: the entry goes, and so does its toast if it is
// still up (the user, 2026-10-06: opening it from the status stack left the toast behind).
export function seen(route, shown) {
  for (const [key, notice] of notices) {
    const session = key.startsWith('turn:') || key.startsWith('wait:') ? key.slice(5) : null;
    const looked = (session && watchingSession(route, shown, session))
      || (key.startsWith('scan:') && route && route.name === 'clients' && notice.href.endsWith(`/${route.id || ''}`));
    if (looked) drop(key);
  }
  // Told to aicleaner, so the other computers clear it too (D200) - once per new turn or question.
  const current = ((previous && previous.sessions) || []).find((s) => s.name === shown);
  if (!current || !watchingSession(route, shown, current.name)) return;
  const mark = `${current.turns}/${current.waiting}`;
  if (reported.get(current.name) === mark) return;
  reported.set(current.name, mark);
  postJSON('/api/activity/seen', { session: current.name, turns: current.turns }).catch(() => {
    reported.delete(current.name);           // tried again on the next poll
  });
}

// The status-stack entries still waiting to be looked at, newest first.
export function activityItems() {
  return [...notices.values()].reverse();
}
