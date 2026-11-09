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

import { getJSON, postJSON, say } from './api.js';

const notices = new Map();   // key -> { level, text, href } - kept until its page is visited
let previous = null;
// Shared between computers (D200, Brad 2026-10-22: "if I respond to one at work, the alert is still there
// when I get home"): looking at a session tells AI Cleaner, every page's next poll carries `seen_turns`, and
// an alert someone has seen anywhere goes everywhere. A question answered anywhere drops `waiting` to 0.
const reported = new Map();  // session -> the turn count already told to AI Cleaner as seen
const heldFinish = new Set(); // sessions whose turn ended while background work still ran (D202)
// Feature 154: Markdown files Claude has opened for review - per session, those not looked at yet. The Session page
// opens them (aicleaner:reviews); the flag goes when each file's tab has been in front (aicleaner:tab-shown).
const unread = new Map();     // session -> Map(root|path -> review)
const handed = new Map();     // session -> the review number already handed to the page
let reviewFiles = true;       // session.review_files, on by default
const noteReviewSetting = (s) => { if ('session.review_files' in s) reviewFiles = s['session.review_files'] !== false; };
window.addEventListener('aicleaner:settings', (event) => noteReviewSetting(event.detail || {}));
getJSON('/api/config').then((config) => noteReviewSetting(config.settings || {})).catch(() => {});
window.addEventListener('aicleaner:tab-shown', (event) => {
  const { session, root, path } = event.detail || {};
  const waiting = unread.get(session);
  if (!waiting || !waiting.delete(`${root}|${path}`)) return;
  if (!waiting.size) { unread.delete(session); drop(`review:${session}`); }
});

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
  noticeReviews(now.sessions || []);
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

// Feature 154: reviews newer than the last ones handed over. The page's first poll only remembers where things stand,
// so a page loaded later does not reopen files reviewed before it; a session that appears after that (started since)
// counts from its first review.
let reviewBaseline = false;
function noticeReviews(sessions) {
  const first = !reviewBaseline;
  reviewBaseline = true;
  for (const s of sessions) {
    const latest = s.review_n || 0;
    if (!handed.has(s.name)) {
      handed.set(s.name, first ? latest : 0);
      if (first) continue;
    }
    const from = handed.get(s.name);
    if (latest <= from) continue;
    handed.set(s.name, latest);
    if (!reviewFiles) continue;
    const fresh = (s.reviews || []).filter((r) => r.n > from);
    if (!fresh.length) continue;
    if (!unread.has(s.name)) unread.set(s.name, new Map());
    for (const r of fresh) unread.get(s.name).set(`${r.root}|${r.path}`, r);
    window.dispatchEvent(new CustomEvent('aicleaner:reviews', { detail: { session: s.name, reviews: fresh } }));
    // Said even while the session is on screen: the file opens behind the tab in front, so this is the clue. Its Open
    // (the toast's, or the entry in the menu bar) brings that file's tab to the front - asked for by the click, never
    // done to the page by itself (Brad, 2026-11-06).
    const last = fresh[fresh.length - 1];
    announce(`review:${s.name}`, 'green', `${s.name}: Claude has opened ${last.name} for you to review`,
      `${sessionHref(s.name).replace(/\/chat$/, '')}/review/${encodeURIComponent(`${last.root}|${last.path}`)}`);
  }
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
  // Told to AI Cleaner, so the other computers clear it too (D200) - once per new turn or question.
  const current = ((previous && previous.sessions) || []).find((s) => s.name === shown);
  if (!current || !watchingSession(route, shown, current.name)) return;
  const mark = `${current.turns}/${current.waiting}`;
  if (reported.get(current.name) === mark) return;
  reported.set(current.name, mark);
  postJSON('/api/activity/seen', { session: current.name, turns: current.turns }).catch(() => {
    reported.delete(current.name);           // tried again on the next poll
  });
}

// Alerts (feature 161): raised on the AI Cleaner machine, kept by AI Cleaner until dismissed - so the stack shows what
// the poll carries, not what this page remembers. A toast for each one this page has not seen before (not on its first
// poll: a page opened later lists them without a burst of toasts).
let openAlerts = [];
let knownAlerts = null;
export function noticeAlerts(activity) {
  openAlerts = ((activity || {}).alerts || []).slice().reverse();          // newest first
  const ids = new Set(openAlerts.map((a) => a.id));
  if (knownAlerts) {
    for (const alert of openAlerts) {
      if (knownAlerts.has(alert.id)) continue;
      say(`Alert: ${alert.title}`, alert.level === 'green' ? 'ok' : 'warn',
        { action: { label: 'Open', run: () => { location.hash = '/alerts'; } } });
    }
  }
  knownAlerts = ids;
}

export function alertItems() {
  return openAlerts.map((a) => ({ level: a.level, text: a.title, href: '#/alerts', alert: true }));
}

// The status-stack entries still waiting to be looked at, newest first.
export function activityItems() {
  return [...notices.values()].reverse();
}
