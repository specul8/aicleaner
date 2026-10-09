// Fetch wrapper for the aicleaner API. JSON in and out; every failure becomes an ApiError with
// the server's message, and say() shows it in the single message area.

export class ApiError extends Error {
  constructor(message, status, suggest, why, offer) {
    super(message);
    this.status = status;
    this.suggest = suggest;  // what the server offers instead, e.g. 'public'
    // A refusal worth explaining carries two more things (feature 41): `why` is the rule it is
    // protecting, `offer` is {action, label, ...} naming something the page can do about it.
    // The server names the action; the page decides which call that is. Both may be undefined.
    this.why = why;
    this.offer = offer;
  }
}

// ---- is aicleaner reachable? (D138) -----------------------------------------------------------
//
// While the proxy restarts, every poll failed and each failure became a "Request failed (HTTP 502)"
// toast - 502 being what the tunnel says when nothing answers behind it. A request that cannot reach
// aicleaner now marks the link down: the polls hold off (they ask `connected()`), one small probe
// asks every PROBE_MS whether it is back, a single notice says what is happening, and when it
// answers the notice goes and the pages are told to refresh.
const UNREACHABLE = new Set([502, 503, 504, 520, 521, 522, 523, 524, 530]);
const PROBE_MS = 3000;
const PROBE_URL = '/api/tunnel/params';     // cheap, allowed locally and through the tunnel, never sealed
const WAITING = 'aicleaner is not answering - it may be restarting. Reconnecting...';
const link = { up: true, timer: null, listeners: new Set() };

export function connected() { return link.up; }

// Called with true when aicleaner answers again (and false when it stops).
export function onConnection(listener) { link.listeners.add(listener); return () => link.listeners.delete(listener); }

function linkDown() {
  if (!link.up) return;
  link.up = false;
  say(WAITING, 'warn');
  for (const listener of link.listeners) { try { listener(false); } catch (e) { /* one page, not all */ } }
  link.timer = setInterval(async () => {
    try {
      const answer = await fetch(PROBE_URL, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!UNREACHABLE.has(answer.status)) linkUp();
    } catch (error) { /* still down */ }
  }, PROBE_MS);
}

function linkUp() {
  if (link.up) return;
  link.up = true;
  clearInterval(link.timer);
  link.timer = null;
  for (const toast of document.querySelectorAll('#toasts .toast')) {
    if (toast.dataset.text === WAITING) toast.remove();
  }
  say('aicleaner is back.', 'ok');
  for (const listener of link.listeners) { try { listener(true); } catch (e) { /* one page, not all */ } }
}

class Unreachable extends Error {
  constructor(status) {
    super(status ? `aicleaner is not answering (HTTP ${status})` : 'aicleaner is not answering');
    this.unreachable = true;
    this.status = status || 0;
  }
}

// fetch, with the link watched: a network failure or a gateway status means aicleaner is not there.
async function watched(url, init) {
  let response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    linkDown();
    throw new Unreachable(0);
  }
  if (UNREACHABLE.has(response.status)) { linkDown(); throw new Unreachable(response.status); }
  if (!link.up) linkUp();
  return response;
}

async function parse(response) {
  let data = null;
  try {
    data = await response.json();
  } catch (e) {
    data = null;
  }
  if (!response.ok || (data && data.error)) {
    const message = (data && data.error) || `Request failed (HTTP ${response.status})`;
    throw new ApiError(message, response.status, data && data.suggest,
                       data && data.why, data && data.offer);
  }
  return data;
}

export async function getJSON(url) {
  return parse(await watched(url, { headers: { Accept: 'application/json' } }));
}

export async function postJSON(url, body) {
  return parse(await watched(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body || {}),
  }));
}

// Messages stack as toasts rather than sharing one line in the toolbar.
//
// That line had two faults. It sat at the top of the content column and scrolled away with it, so
// on a page of several hundred rows - which is the normal case - a message was written somewhere
// off screen, waited out its timer and cleared itself unseen. And it was a single slot assigned
// with textContent, so a second message destroyed the first: a real failure could be replaced by
// a passing status-poll message seconds later, with no history anywhere (D20).
//
// Fixed position, so scrolling cannot hide one. Stacked, so nothing replaces anything. A
// confirmation goes on its own after a few seconds; a warning or an error waits to be dismissed,
// because something that needs doing should not disappear while you are reading the rest of the
// screen.
const KEEP_MS = 5000;              // how long a confirmation stays
const ACTION_KEEP_MS = 10000;      // one with an Undo on it: the window to notice a mis-click (D68)
const STACK_MAX = 5;               // beyond this the oldest dismissible one goes
const LINGERS = new Set(['error', 'warn']);   // these wait to be dismissed

function toastHost() {
  let host = document.getElementById('toasts');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toasts';
    document.body.append(host);
  }
  return host;
}

function trim(host) {
  // "Never replace" plus "errors do not expire" can fill the column, so there is a ceiling - and
  // what goes is always the oldest one that would have gone by itself anyway.
  const all = [...host.children];
  const droppable = all.filter((t) => !LINGERS.has(t.dataset.level));
  let over = all.length - STACK_MAX;
  for (const toast of droppable) {
    if (over <= 0) break;
    toast.remove();
    over -= 1;
  }
  while (host.children.length > STACK_MAX) host.firstElementChild.remove();
}

// `extra` is the feature-41 shape: { why: string, action: { label, run } }. `why` is a second
// line explaining the rule; `action` becomes a button that does something about it. A toast
// carrying an action always lingers, whatever the level - a question that scrolls past
// unanswered is worse than no question.
export function say(text, level = 'info', extra = {}) {
  if (!text) return null;
  const host = toastHost();
  const { why = '', action = null } = extra || {};
  // Only a problem waits to be dismissed. A confirmation carrying an action - an Undo, mostly -
  // used to wait too, and piled up: the user reported them stuck. A mis-click is noticed at once,
  // so the window it needs is seconds, not for ever (D68).
  const lingers = LINGERS.has(level);

  // The same thing twice counts rather than stacks: a poll that keeps failing should not bury
  // everything else under identical copies of itself. The explanation is part of the identity,
  // so the same sentence for two different reasons stays two toasts.
  const same = [...host.children].find((t) => t.dataset.level === level
    && t.dataset.text === text && (t.dataset.why || '') === why);
  if (same) {
    const seen = Number(same.dataset.seen || '1') + 1;
    same.dataset.seen = String(seen);
    same.querySelector('.toast-count').textContent = `x${seen}`;
    same.querySelector('.toast-count').hidden = false;
    return same;
  }

  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.dataset.level = level;
  toast.dataset.text = text;
  if (why) toast.dataset.why = why;
  // An error should interrupt what a screen reader is saying; a confirmation should wait its turn.
  toast.setAttribute('role', lingers ? 'alert' : 'status');
  toast.setAttribute('aria-live', lingers ? 'assertive' : 'polite');

  const body = document.createElement('span');
  body.className = 'toast-text';
  body.textContent = text;
  const count = document.createElement('span');
  count.className = 'toast-count';
  count.hidden = true;
  toast.append(body, count);

  if (why) {
    const reason = document.createElement('span');
    reason.className = 'toast-why';
    reason.textContent = why;
    toast.append(reason);
  }

  if (action && typeof action.run === 'function') {
    const go = document.createElement('button');
    go.type = 'button';
    go.className = 'toast-action';
    go.textContent = action.label || 'Do it';
    go.addEventListener('click', async () => {
      go.disabled = true;
      try {
        await action.run();
        toast.remove();
      } catch (e) {
        go.disabled = false;
        sayError(e);
      }
    });
    toast.append(go);
  }

  // Every toast can be closed, whatever its level and whether or not it carries an action (the
  // user, 2026-10-01). A confirmation used to have no x and had to be waited out; closing one with
  // an Undo on it only dismisses it - it never runs the action.
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'toast-close';
  close.setAttribute('aria-label', 'Dismiss');
  close.textContent = '×';
  close.addEventListener('click', () => toast.remove());
  toast.append(close);

  if (!lingers) {
    // An action gets the longer window, and the clock stops while the pointer is on the toast, so
    // it cannot vanish from under a hand on its way to Undo.
    const keep = action ? ACTION_KEEP_MS : KEEP_MS;
    let timer = setTimeout(() => toast.remove(), keep);
    toast.addEventListener('mouseenter', () => clearTimeout(timer));
    toast.addEventListener('mouseleave', () => { timer = setTimeout(() => toast.remove(), keep); });
  }

  host.append(toast);
  trim(host);
  return toast;
}

// `handlers` maps a server-named action to the call this page can make for it, e.g.
// { ignore_global: (offer) => ignoreGlobally(offer.value) }. A page that has no handler for the
// action the server named still gets the message and the explanation - it just has no button,
// which is the right outcome rather than a button that does nothing.
export function sayError(error, handlers = null) {
  // aicleaner not answering is already said, once, by the reconnecting notice (D138).
  if (error && error.unreachable) return null;
  const text = error && error.message ? error.message : String(error);
  const why = (error && error.why) || '';
  const offer = error && error.offer;
  const run = offer && handlers && handlers[offer.action];
  const action = run ? { label: offer.label, run: () => run(offer) } : null;
  return say(text, 'error', { why, action });
}
