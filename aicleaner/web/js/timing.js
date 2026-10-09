// Diagnostics: timing what the page does (feature 91). Off unless Settings > Diagnostics > Record
// timings is on (the status says so). On, each step a caller wraps is measured, the browser's own
// timing of every API request (network time, bytes, the server's Server-Timing parts) is collected,
// and a summary goes to the server every few seconds for logs/timings-<date>.jsonl. Names,
// durations and sizes only - never content.

const FLUSH_MS = 5000;
const state = { on: false, queue: [], timer: null, seen: 0 };

export function timingOn() { return state.on; }

// From each status poll: start or stop recording to match the setting.
export function setTiming(on) {
  if (on === state.on) return;
  state.on = Boolean(on);
  if (state.on) {
    if (performance.setResourceTimingBufferSize) performance.setResourceTimingBufferSize(2000);
    state.timer = setInterval(flush, FLUSH_MS);
  } else {
    clearInterval(state.timer);
    state.timer = null;
    state.queue = [];
  }
}

// One measured thing: { step, ms, ...detail }.
export function note(step, ms, detail = {}) {
  if (!state.on) return;
  state.queue.push({ source: 'page', step, ms: Math.round(ms * 10) / 10, at: new Date().toISOString(),
    route: location.hash.slice(0, 200), ...detail });
}

// Time an async step: `await timed('switch.state', () => getJSON(...))`. Costs nothing when off.
export async function timed(step, run, detail = {}) {
  if (!state.on) return run();
  const started = performance.now();
  try {
    return await run();
  } finally {
    note(step, performance.now() - started, detail);
  }
}

// A step made of several: `const t = begin('switch', {to}); ...; t.lap('state'); ...; t.end()`.
export function begin(step, detail = {}) {
  const started = performance.now();
  let last = started;
  const laps = {};
  return {
    lap(name) {
      const now = performance.now();
      laps[name] = Math.round((now - last) * 10) / 10;
      last = now;
    },
    end(extra = {}) { note(step, performance.now() - started, { laps, ...detail, ...extra }); },
  };
}

function resourceEntries() {
  const all = performance.getEntriesByType('resource');
  const fresh = all.slice(state.seen);
  state.seen = all.length;
  return fresh.filter((r) => r.name.includes('/api/') && !r.name.includes('/api/debug/timings')).map((r) => {
    const url = new URL(r.name);
    return { source: 'browser', step: 'request', path: (url.pathname + url.search).slice(0, 300),
      ms: Math.round(r.duration), wait: Math.round(r.responseStart - r.requestStart),
      download: Math.round(r.responseEnd - r.responseStart), bytes: r.transferSize || r.encodedBodySize || 0,
      server: (r.serverTiming || []).map((s) => `${s.name}=${Math.round(s.duration)}`).join(' ') };
  });
}

async function flush() {
  if (!state.on) return;
  const entries = [...state.queue, ...resourceEntries()];
  state.queue = [];
  if (!entries.length) return;
  try {
    await fetch('/api/debug/timings', { method: 'POST', body: JSON.stringify({ entries }),
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' } });
  } catch (error) { /* a lost report is a gap in the timings, nothing more */ }
}
