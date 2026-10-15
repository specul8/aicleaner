// The encrypted tunnel, in the browser (feature 21, DECISIONS 57 and 60).
//
// Through the tunnel with encryption on, nothing under /api/ crosses Cloudflare - or anything else
// that can open HTTPS on the way - in clear. From the code shown on the aicleaner machine this page
// derives K (PBKDF2-SHA256, the proxy's salt and count). K encrypts nothing: it only proves each end
// to the other in a key exchange run for every connection - a throwaway ECDH P-256 key pair on each
// side, the shared secret through HKDF as this connection's key. Every /api/ call then goes as one
// sealed POST to /api/sealed, numbered so it cannot be replayed, and its sealed answer comes back as
// an ordinary Response, so every page works unchanged.
//
// Kept on the device: K only - never the code - for the tab or on the device, as the owner chose.
// The connection's key lives in this page's memory and nowhere else. On the machine itself
// /api/tunnel/params says "not sealed" and none of this runs.

import { note } from './timing.js';

const KEY_NAME = 'aicleaner.tunnel.key';
const PARAMS = '/api/tunnel/params';
const CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
const INFO = 'aicleaner tunnel 2';
let sealedOn = false;

// Sign-in expiry (the user, 2026-10-14). Cloudflare Access ends a sign-in after 24 hours and then
// answered every call with a redirect to its sign-in page - cross-site, so the browser failed it like
// a network error and the page sat on "Not connected" for good. Marked as a page's own request,
// Access answers 401 instead; aicleaner marks its own 401 (run the exchange again) with a header, so
// any other 401, or a redirect, means "sign in again". Said once, by api.js, on this event.
const AJAX = { 'X-Requested-With': 'XMLHttpRequest' };
const REKEY_HEADER = 'X-Aicleaner-Tunnel';

function wantsSignIn(response) {
  return response.type === 'opaqueredirect'
    || (response.status === 401 && response.headers.get(REKEY_HEADER) !== 'rekey');
}

export function signedOutError() {
  const error = new Error('Your sign-in has expired: sign in again.');
  error.signedOut = true;
  window.dispatchEvent(new CustomEvent('aicleaner:signedout'));
  return error;
}

export function isSealed() {
  return sealedOn;
}

const fromB64 = (text) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
const toB64 = (bytes) => {
  let text = '';
  for (const b of new Uint8Array(bytes)) text += String.fromCharCode(b);
  return btoa(text);
};
const join = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
};
const ascii = (text) => new TextEncoder().encode(text);

// The code as typed - any case, hyphens or spaces - as the code itself, as the proxy reads it.
export function normaliseCode(code) {
  return [...String(code).toUpperCase()].filter((c) => CODE_ALPHABET.includes(c)).join('');
}

// Exported for the interoperability test; the page itself uses openTunnel.
export async function deriveLongTerm(code, params) {
  const material = await crypto.subtle.importKey('raw', ascii(normaliseCode(code)), 'PBKDF2', false, ['deriveBits']);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: fromB64(params.salt),
    iterations: params.iterations }, material, 256));
}

async function hmac(key, data) {
  const k = await crypto.subtle.importKey('raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', k, data));
}

function sameBytes(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

// One connection's key exchange (exported for the interoperability test). `post` sends JSON and
// returns the parsed answer; the page passes plain fetch, the test the proxy itself. Resolves to
// { sid, key } - key an AES-GCM CryptoKey - or throws 'wrong-code' when the proxy's proof fails.
export async function handshake(longTerm, post) {
  const mine = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  const clientPub = new Uint8Array(await crypto.subtle.exportKey('raw', mine.publicKey));
  const clientNonce = crypto.getRandomValues(new Uint8Array(16));
  const hello = await post('/api/tunnel/hello', { client_pub: toB64(clientPub), client_nonce: toB64(clientNonce) });
  const serverPub = fromB64(hello.server_pub);
  const serverNonce = fromB64(hello.server_nonce);
  const exchange = join(clientPub, clientNonce, serverPub, serverNonce);
  if (!sameBytes(fromB64(hello.proof), await hmac(longTerm, join(ascii('server'), exchange)))) {
    throw new Error('wrong-code');
  }
  const peer = await crypto.subtle.importKey('raw', serverPub, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const shared = await crypto.subtle.deriveBits({ name: 'ECDH', public: peer }, mine.privateKey, 256);
  const hkdf = await crypto.subtle.importKey('raw', shared, 'HKDF', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey({ name: 'HKDF', hash: 'SHA-256', salt: join(clientNonce, serverNonce),
    info: join(ascii(INFO), clientPub, serverPub) }, hkdf, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
  await post('/api/tunnel/confirm', { sid: hello.sid, proof: toB64(await hmac(longTerm, join(ascii('client'), exchange))) });
  return { sid: hello.sid, key };
}

export async function sealRequest(session, seq, text) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: ascii(`${session.sid}|${seq}`) },
    session.key, ascii(text));
  return { v: 2, sid: session.sid, seq, iv: toB64(iv), ct: toB64(ct) };
}

export async function openReply(session, envelope) {
  const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: fromB64(envelope.iv),
    additionalData: ascii(`${envelope.sid}|${envelope.seq}|reply`) }, session.key, fromB64(envelope.ct));
  return new TextDecoder().decode(plain);
}

function store(params) {
  return params.storage === 'device' ? window.localStorage : window.sessionStorage;
}

export function forgetKey() {
  window.sessionStorage.removeItem(KEY_NAME);
  window.localStorage.removeItem(KEY_NAME);
}

// The code box: the only thing the page shows through the tunnel until the exchange succeeds.
function askForCode(params, connect) {
  return new Promise((resolve) => {
    const box = document.createElement('input');
    box.type = 'password';
    box.id = 'tunnel-passphrase';
    box.autocomplete = 'off';
    box.setAttribute('aria-label', 'Tunnel code');
    const go = document.createElement('button');
    go.type = 'submit';
    go.className = 'primary';
    go.textContent = 'Unlock';
    const why = document.createElement('p');
    why.className = 'hint';
    why.id = 'tunnel-why';
    const form = document.createElement('form');
    form.id = 'tunnel-lock';
    form.className = 'card tunnel-lock';
    const head = document.createElement('h2');
    head.textContent = 'Encrypted tunnel';
    const said = document.createElement('p');
    said.className = 'hint';
    said.textContent = 'Everything this page sends and receives is encrypted with keys Cloudflare never sees. '
      + 'Enter the code shown on the aicleaner machine (Settings > Encrypted tunnel).';
    form.append(head, said, box, go, why);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      go.disabled = true;
      why.textContent = 'Checking...';
      try {
        const longTerm = await deriveLongTerm(box.value, params);
        const session = await connect(longTerm);
        store(params).setItem(KEY_NAME, toB64(longTerm));
        form.remove();
        resolve({ longTerm, session });
        return;
      } catch (error) {
        why.textContent = error.message === 'wrong-code'
          ? 'That is not the code set on the aicleaner machine.' : `Could not connect: ${error.message}`;
      }
      go.disabled = false;
      box.select();
    });
    document.body.prepend(form);
    box.focus();
  });
}

// Compressed answers (D178): opened with the browser's own DecompressionStream, where there is one.
const CAN_GUNZIP = typeof DecompressionStream === 'function';

async function gunzip(bytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

function wrapFetch(longTerm, first, connect) {
  const plain = window.fetch.bind(window);
  let session = first;
  let seq = 0;
  let reconnecting = null;
  const reconnect = () => {
    if (!reconnecting) {
      reconnecting = connect(longTerm).then((fresh) => { session = fresh; seq = 0; }).finally(() => { reconnecting = null; });
    }
    return reconnecting;
  };
  const send = async (inner) => {
    const n = seq++;
    const mine = session;
    const envelope = await sealRequest(mine, n, inner);
    const response = await plain('/api/sealed', { method: 'POST', redirect: 'manual',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...AJAX }, body: JSON.stringify(envelope) });
    if (wantsSignIn(response)) throw signedOutError();
    return { response, mine };
  };
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, window.location.href);
    if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/') || url.pathname.startsWith('/api/tunnel/')) {
      return plain(input, init);
    }
    const method = String(init.method || (typeof input === 'string' ? 'GET' : input.method) || 'GET').toUpperCase();
    const body = init.body == null ? null : String(init.body);
    // `gzip`: this page can open a compressed answer (D178); a page from before it gets plain ones.
    const inner = JSON.stringify({ method, path: url.pathname + url.search, body, gzip: CAN_GUNZIP });
    const started = performance.now();          // Diagnostics (feature 91): trip, then decryption
    let { response, mine } = await send(inner);
    if (response.status === 401) {
      // The proxy restarted, or the connection sat idle: a fresh exchange, then the request again.
      await reconnect();
      ({ response, mine } = await send(inner));
    }
    if (response.status === 403) {
      // The code was changed on the machine, or encryption switched: start over from the box. Only
      // for the gate's own "locked" answer - any other 403 (a firewall, Access refusing this account)
      // is shown as it is, and the kept key stays.
      const said = await response.clone().json().catch(() => ({}));
      if (String(said.error || '').startsWith('locked')) {
        forgetKey();
        window.location.reload();
      }
    }
    if (!response.ok) return response;
    const envelope = await response.json();
    const arrived = performance.now();
    const answer = JSON.parse(await openReply(mine, envelope));
    const bytes = answer.encoding === 'gzip' ? await gunzip(fromB64(answer.body || '')) : fromB64(answer.body || '');
    if (url.pathname !== '/api/debug/timings') {
      note('sealed', performance.now() - started, { path: (url.pathname + url.search).slice(0, 300),
        trip: Math.round(arrived - started), decrypt: Math.round(performance.now() - arrived), bytes: bytes.length });
    }
    const empty = answer.status === 204 || answer.status === 304;
    const headers = { 'Content-Type': answer.type || 'application/json' };
    if (answer.disposition) headers['Content-Disposition'] = answer.disposition;
    return new Response(empty ? null : bytes, { status: answer.status, headers });
  };
  // A download link goes straight to the address, past the sealed fetch above, and the tunnel turns
  // it away. Through the tunnel every one is fetched sealed and saved from memory instead (D168).
  document.addEventListener('click', (event) => {
    const link = event.target.closest && event.target.closest('a[href^="/api/"]');
    if (!link || !(link.hasAttribute('download') || link.classList.contains('file-download'))) return;
    event.preventDefault();
    saveThrough(link.getAttribute('href'), link.getAttribute('download') || '').catch((error) => {
      window.dispatchEvent(new CustomEvent('aicleaner:error', { detail: error }));
    });
  }, true);
}

// Fetch an /api/ address (sealed, through the tunnel) and hand the browser the bytes as a download.
export async function saveThrough(href, filename = '') {
  const response = await window.fetch(href);
  if (!response.ok) {
    const answer = await response.json().catch(() => ({}));
    throw new Error(answer.error || `The download failed (HTTP ${response.status})`);
  }
  const named = /filename\*=UTF-8''([^;]+)/.exec(response.headers.get('Content-Disposition') || '')
    || /filename="?([^";]+)"?/.exec(response.headers.get('Content-Disposition') || '');
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = filename || (named ? decodeURIComponent(named[1]) : 'download');
  link.hidden = true;
  document.body.append(link);
  link.dispatchEvent(new MouseEvent('click'));    // not bubbling: the handler above must not see it
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// An /api/ address as an object URL the page can show (an image or a PDF in a tab), fetched sealed.
export async function objectUrlThrough(href) {
  const response = await window.fetch(href);
  if (!response.ok) {
    const answer = await response.json().catch(() => ({}));
    throw new Error(answer.error || `Could not load it (HTTP ${response.status})`);
  }
  return URL.createObjectURL(await response.blob());
}

// Whether this page is sealed. A page loaded while aicleaner restarts got a gateway error here and
// was taken as "not sealed": every call then came back 403 locked until a second refresh (the user,
// 2026-10-14). An unreachable answer is tried again, for up to two minutes, before giving up.
const UNREACHABLE_PARAMS = new Set([502, 503, 504, 520, 521, 522, 523, 524, 530]);
async function tunnelParams() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await fetch(PARAMS, { headers: { Accept: 'application/json', ...AJAX } });
      if (!UNREACHABLE_PARAMS.has(response.status)) return response.ok ? await response.json() : { sealed: false };
    } catch (error) { /* not answering yet */ }
    await new Promise((done) => setTimeout(done, 3000));
  }
  return { sealed: false };
}

// Before the first page draws. Resolves once the page may talk to the API.
export async function openTunnel() {
  const params = await tunnelParams();
  if (!params.sealed) return;
  sealedOn = true;
  const plain = window.fetch.bind(window);
  const post = async (path, body) => {
    const response = await plain(path, { method: 'POST', redirect: 'manual',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...AJAX }, body: JSON.stringify(body) });
    if (wantsSignIn(response)) throw signedOutError();
    const answer = await response.json();
    if (!response.ok) throw new Error(answer.error || `HTTP ${response.status}`);
    return answer;
  };
  const connect = (longTerm) => handshake(longTerm, post);
  const kept = store(params).getItem(KEY_NAME);
  let opened = null;
  if (kept) {
    try {
      const longTerm = fromB64(kept);
      opened = { longTerm, session: await connect(longTerm) };
    } catch (error) {
      forgetKey();                      // a new code was generated since: ask for it
    }
  }
  if (!opened) opened = await askForCode(params, connect);
  wrapFetch(opened.longTerm, opened.session, connect);
}
