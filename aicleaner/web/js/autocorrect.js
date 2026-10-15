// Autocorrect in the Session page's prompt (feature 94; spec 2026-10-08-f94-prompt-autocorrect).
//
// On a word boundary typed while it is on, the word just finished is checked against a SCOWL list
// ranked by band (10 = most common) and the user's own words (which carry their casing:
// powershell -> PowerShell). A word not in either, within 1 edit (up to 4 letters) or 2 (longer), is
// replaced by the best candidate - own words first, then the closest, then the commonest - and marked
// green; clicking or tapping it opens a bubble below it with Revert, the alternatives and "Add to my
// words" (right-click any word does the same while it is on). Code
// and paths are never touched; client names, words already used and capitalised words are left alone
// when chosen in Settings. Turning it on corrects from then on, never what is already in the box.
// Every change goes through execCommand('insertText'), so Ctrl+Z undoes it.

import { getJSON, postJSON, sayError } from './api.js';
import { h } from './dom.js';

// Not the apostrophe: typed mid-word it is a contraction going on (couldn't), and checking the part
// before it corrected "couldn" to "could" before the rest arrived (D163).
const BOUNDARY = /[\s.,;:!?)\]}"]/;
// Contractions whose first part is not a word on its own (D163, the user 2026-10-08).
const CONTRACTIONS = new Set(["can't", "won't", "shan't", "ain't", "y'all", "o'clock", "ma'am"]);

// One of the shipped terms (feature 100), in any case.
function isTerm(lower) {
  return (words.terms || []).some((w) => w.toLowerCase() === lower);
}

// A contraction of a known word: there'll, they'll, we've (the part before the apostrophe is a word),
// couldn't, didn't (a word plus n't), and the few in CONTRACTIONS.
function knownContraction(lower, own = []) {
  const at = lower.indexOf("'");
  if (at < 1 || !words.band) return false;
  if (CONTRACTIONS.has(lower)) return true;
  const known = (w) => words.band.has(w) || isTerm(w) || own.some((o) => o.toLowerCase() === w);
  if (known(lower.slice(0, at))) return true;
  return lower.endsWith("n't") && known(lower.slice(0, -3));
}
const MAX_ALTERNATIVES = 5;
const words = { band: null, byLength: null, loading: null, clients: null };

async function loadWords() {
  if (words.band) return;
  if (!words.loading) {
    words.loading = (async () => {
      const answer = await getJSON('/api/autocorrect/words');
      const band = new Map();
      const byLength = new Map();
      for (const [rank, list] of Object.entries(answer.bands || {})) {
        for (const word of list.split(' ')) {
          if (!word || band.has(word)) continue;
          band.set(word, Number(rank));
          if (!byLength.has(word.length)) byLength.set(word.length, []);
          byLength.get(word.length).push(word);
        }
      }
      words.band = band;
      words.byLength = byLength;
      // Days, months and product names with their capitals (feature 100): treated as own words.
      words.terms = answer.terms || [];
    })().catch((error) => { words.loading = null; throw error; });
  }
  await words.loading;
}

async function clientWords() {
  if (!words.clients) {
    try { words.clients = new Set((await getJSON('/api/autocorrect/clients')).words || []); }
    catch (error) { words.clients = new Set(); }
  }
  return words.clients;
}

// Damerau-Levenshtein distance, giving up (returning limit + 1) once it must exceed `limit`.
export function distance(a, b, limit) {
  if (Math.abs(a.length - b.length) > limit) return limit + 1;
  const rows = [];
  for (let i = 0; i <= a.length; i += 1) rows.push([i]);
  for (let j = 1; j <= b.length; j += 1) rows[0][j] = j;
  for (let i = 1; i <= a.length; i += 1) {
    let best = Infinity;
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      let value = Math.min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) value = Math.min(value, rows[i - 2][j - 2] + 1);
      rows[i][j] = value;
      if (value < best) best = value;
    }
    if (best > limit) return limit + 1;
  }
  return rows[a.length][b.length];
}

// The typed word's capitalisation on a replacement: Teh -> The, TEH -> THE; an own word keeps its own.
function shaped(typed, word, own) {
  if (own) return word;
  if (typed.length > 1 && typed === typed.toUpperCase()) return word.toUpperCase();
  if (typed[0] === typed[0].toUpperCase()) return word[0].toUpperCase() + word.slice(1);
  return word;
}

// { best, alternatives } for a typed word, or null when it should be left as it is.
// `ownOnly`: just the own-word casing (no list needed), else null.
export function correctionFor(typed, ownWords, ownOnly = false) {
  const lower = typed.toLowerCase();
  // The shipped terms first, so a word of your own with the same letters wins (feature 100).
  const own = new Map([...(words.terms || []), ...(ownWords || [])].map((w) => [w.toLowerCase(), w]));
  if (own.has(lower)) return own.get(lower) === typed ? null : { best: own.get(lower), alternatives: [] };
  // A contraction of a known spelling takes its casing: i'm is I'm, monday's is Monday's (the user,
  // 2026-10-14).
  const at = lower.indexOf("'");
  if (at > 0 && own.has(lower.slice(0, at))) {
    const cased = own.get(lower.slice(0, at)) + typed.slice(at);
    return cased === typed ? null : { best: cased, alternatives: [] };
  }
  if (ownOnly) return null;
  if (!words.band || words.band.has(lower) || knownContraction(lower, ownWords)) return null;
  const limit = lower.length <= 4 ? 1 : 2;
  const found = [];
  for (const [key, word] of own) {
    const d = distance(lower, key, limit);
    if (d <= limit) found.push({ word, d, rank: 0, own: true });
  }
  for (let n = lower.length - limit; n <= lower.length + limit; n += 1) {
    for (const word of words.byLength.get(n) || []) {
      const d = distance(lower, word, limit);
      if (d <= limit) found.push({ word, d, rank: words.band.get(word), own: false });
    }
  }
  if (!found.length) return null;
  // Closest first, then the same first letter, then the commonest (D154, the user 2026-10-08: hamer
  // became "later" - commoner, but two edits away - when "hammer" was one). A doubled letter typed
  // once or a single one typed twice (hamer, adress, untill) is the commonest slip of all, so it
  // counts half an edit. Then the likelier typo: the same letters in another order (a swap - teh is
  // the, not tea or ten), the same length, the same last letter.
  const squash = (w) => w.toLowerCase().replace(/(.)\1+/g, '$1');
  const typedSquash = squash(lower);
  for (const f of found) f.cost = f.d - (squash(f.word) === typedSquash ? 0.5 : 0);
  const letters = (w) => [...w.toLowerCase()].sort().join('');
  const typedLetters = letters(lower);
  const near = (f) => [letters(f.word) === typedLetters, f.word.length === lower.length,
    f.word.slice(-1).toLowerCase() === lower.slice(-1)];
  const sameStart = (f) => f.word[0].toLowerCase() === lower[0];
  found.sort((x, y) => {
    if (x.own !== y.own) return x.own ? -1 : 1;
    if (x.cost !== y.cost) return x.cost - y.cost;
    if (sameStart(x) !== sameStart(y)) return sameStart(x) ? -1 : 1;
    if (x.rank !== y.rank) return x.rank - y.rank;
    const a = near(x);
    const b = near(y);
    for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return a[i] ? -1 : 1;
    return 0;
  });
  const [best, ...rest] = found.map((f) => shaped(typed, f.word, f.own));
  return { best, alternatives: [...new Set(rest)].filter((w) => w !== best).slice(0, MAX_ALTERNATIVES) };
}

// The user's own replacements, "typed = replacement" lines, as typed (lowercase) -> replacement.
export function replacements(lines) {
  const out = new Map();
  for (const line of lines || []) {
    const at = String(line).indexOf('=');
    if (at < 1) continue;
    const typed = line.slice(0, at).trim().toLowerCase();
    const replacement = line.slice(at + 1).trim();
    if (typed && replacement) out.set(typed, replacement);
  }
  return out;
}

// The spellings the replacements give that are one word with a capital in it (OneDrive, DevOps):
// known spellings in their own right, so typing the word itself in any case takes them - no need to
// add it again as an own word. Multi-word ones (Key Vault) are not.
export function spellingsOf(replace) {
  return [...new Set([...(replace || new Map()).values()].filter((w) => /^[A-Za-z][A-Za-z'-]*$/.test(w) && /[A-Z]/.test(w)))];
}

// True when the word is already spelt exactly as a known spelling (a shipped term or one of these).
export function knownSpelling(typed, ownWords) {
  return [...(words.terms || []), ...(ownWords || [])].includes(typed);
}

// ---- the prompt ------------------------------------------------------------------------------------

function insertText(box, start, end, text) {
  box.focus();
  box.setSelectionRange(start, end);
  if (!document.execCommand('insertText', false, text)) {
    box.setRangeText(text, start, end, 'end');
    box.dispatchEvent(new Event('input', { bubbles: true }));
  }
}

// `opts.conversation()`: the conversation's text, for "words already used". Returns { toggle, stack,
// attach }: the button for beside the link indicator, the element to put where the box was, and
// attach(another box) -> its stack, for the prompt's other text parts (feature 118: the prompt is
// text boxes with snippets between them). One switch and one set of words; each box its own marks.
export function attachAutocorrect(box, opts = {}) {
  const shared = { on: false, skip: [], own: [], replace: new Map(), onSince: 0 };
  const mark = h('span', { class: 'ac-mark', 'aria-hidden': 'true' });
  const toggle = h('button', { type: 'button', class: 'ac-toggle', 'aria-pressed': 'false',
    title: 'Correct spelling as you type' }, 'Autocorrect ', mark);
  const wired = new Set();
  let openOne = null;                      // the box whose bubble is open: one bubble at a time

  const paintToggle = () => {
    toggle.setAttribute('aria-pressed', String(shared.on));
    mark.textContent = shared.on ? '✓' : '✗';
    toggle.classList.toggle('on', shared.on);
  };
  getJSON('/api/config').then((config) => {
    const s = config.settings || {};
    shared.on = Boolean(s['prompt.autocorrect']);
    shared.onSince = Date.now();           // a draft already in the box is left as it is
    shared.skip = s['prompt.autocorrect_skip'] || [];
    shared.own = s['prompt.autocorrect_words'] || [];
    shared.replace = replacements(s['prompt.autocorrect_replace']);
    paintToggle();
    if (shared.on) loadWords().catch(() => {});
  }).catch(() => paintToggle());
  toggle.addEventListener('click', async () => {
    shared.on = !shared.on;
    shared.onSince = Date.now();           // from now on, never what is already there
    paintToggle();
    if (shared.on) loadWords().catch(sayError);
    try { await postJSON('/api/config', { settings: { 'prompt.autocorrect': shared.on } }); } catch (error) { sayError(error); }
  });
  // Registered once, not per box: text parts come and go as snippets are added and removed.
  document.addEventListener('mousedown', (event) => {
    if (openOne && !openOne.owns(event.target)) openOne.closeBubble();
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && openOne) openOne.closeBubble(); });

  const first = wire(box);
  return {
    toggle,
    stack: first.stack,
    attach(other) { return wire(other).stack; },
    clear() { for (const one of [...wired]) { if (one.stack.isConnected || one === first) one.clear(); else wired.delete(one); } },
    refresh() { for (const one of wired) one.refresh(); },
  };

  // One box: its layer of marks, its corrections and its bubble. Reads of the switch and the word
  // lists fall through to `shared`; writes to them must name `shared`.
  function wire(box) {
    const state = Object.assign(Object.create(shared), { fixes: [], before: null, ours: false, lettersAt: 0 });
    const layer = h('div', { class: 'ac-layer', 'aria-hidden': 'true' });
    const stack = h('div', { class: 'ac-stack' }, layer, box);
    let bubble = null;

    // The layer mirrors the box so a corrected word's green sits exactly behind it.
    const mirror = () => {
      const s = getComputedStyle(box);
      for (const prop of ['font', 'letterSpacing', 'lineHeight', 'padding', 'borderWidth', 'whiteSpace', 'wordWrap', 'tabSize']) {
        layer.style[prop] = s[prop];
      }
      layer.style.width = `${box.clientWidth + parseFloat(s.borderLeftWidth) + parseFloat(s.borderRightWidth)}px`;
      layer.style.height = `${box.offsetHeight}px`;
    };
    const paint = () => {
      const text = box.value;
      const parts = [];
      let at = 0;
      for (const [i, fix] of state.fixes.entries()) {
        parts.push(document.createTextNode(text.slice(at, fix.start)));
        parts.push(h('span', { class: fix.unknown ? 'ac-unknown' : 'ac-fixed', dataset: { fix: String(i) } },
          text.slice(fix.start, fix.end)));
        at = fix.end;
      }
      parts.push(document.createTextNode(`${text.slice(at)}\n`));
      layer.replaceChildren(...parts);
      layer.scrollTop = box.scrollTop;
    };
    const refresh = () => { mirror(); paint(); };
    if (window.ResizeObserver) new ResizeObserver(refresh).observe(box);
    box.addEventListener('scroll', () => { layer.scrollTop = box.scrollTop; });

    const closeBubble = () => {
      if (bubble) bubble.remove();
      bubble = null;
      if (openOne === self) openOne = null;
    };
    const self = { stack, closeBubble, owns: (target) => target === box || Boolean(bubble && bubble.contains(target)) };
    // Below the word, or above it when below would leave the window; kept inside it side to side (D165).
    const placeBubble = (rect) => {
      if (openOne && openOne !== self) openOne.closeBubble();
      openOne = self;
      document.body.append(bubble);
      bubble.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - bubble.offsetWidth - 8))}px`;
      const below = rect.bottom + 6;
      const above = (rect.top ?? rect.bottom) - bubble.offsetHeight - 6;
      const fits = below + bubble.offsetHeight <= window.innerHeight - 8;
      bubble.style.top = `${Math.max(8, fits || above < 8 ? Math.min(below, window.innerHeight - bubble.offsetHeight - 8) : above)}px`;
    };
    // A word of the user's own, added only on purpose (D155): Revert alone never adds anything. Its
    // red underlines go with it (D164).
    const addOwnWord = async (word) => {
      if (state.own.some((w) => w.toLowerCase() === word.toLowerCase())) return;
      state.fixes = state.fixes.filter((f) => !(f.unknown && f.word.toLowerCase() === word.toLowerCase()));
      paint();
      shared.own = [...shared.own, word];
      try { await postJSON('/api/config', { settings: { 'prompt.autocorrect_words': shared.own } }); }
      catch (error) { shared.own = shared.own.filter((w) => w !== word); sayError(error); }
    };
    const ownWordShape = /^[A-Za-z][A-Za-z'-]{0,39}$/;
    const openBubble = (index) => {
      const fix = state.fixes[index];
      const span = layer.querySelector(`[data-fix="${index}"]`);
      if (!fix || !span) return;
      if (bubble && bubble.dataset.fix === String(index)) return;
      closeBubble();
      if (fix.unknown) {
        // A word it does not know and could not correct (D164): only the offer to keep it.
        const add = h('button', { type: 'button', class: 'ac-add' }, `Add "${fix.word}" to my words`);
        add.addEventListener('click', () => { closeBubble(); addOwnWord(fix.word); box.focus(); });
        bubble = h('div', { class: 'ac-bubble', role: 'dialog', dataset: { fix: String(index) } },
          h('div', { class: 'hint' }, `"${fix.word}" is not a word autocorrect knows`), h('div', { class: 'ac-choices' }, add));
        placeBubble(span.getBoundingClientRect());
        return;
      }
      const choose = (text) => {
        closeBubble();
        state.ours = true;
        insertText(box, fix.start, fix.end, text);
        state.ours = false;
        state.fixes.splice(index, 1);
        const delta = text.length - (fix.end - fix.start);
        for (const other of state.fixes) if (other.start > fix.start) { other.start += delta; other.end += delta; }
        paint();
      };
      const revert = h('button', { type: 'button', class: 'ac-revert' }, `Revert to "${fix.original}"`);
      revert.addEventListener('click', () => choose(fix.original));
      const others = fix.alternatives.map((word) => {
        const b = h('button', { type: 'button', class: 'ac-alt' }, word);
        b.addEventListener('click', () => choose(word));
        return b;
      });
      const keep = ownWordShape.test(fix.original)
        ? h('button', { type: 'button', class: 'ac-add' }, `Add "${fix.original}" to my words`) : null;
      if (keep) keep.addEventListener('click', () => { choose(fix.original); addOwnWord(fix.original); });
      bubble = h('div', { class: 'ac-bubble', role: 'dialog', dataset: { fix: String(index) } },
        h('div', { class: 'hint' }, `Corrected from "${fix.original}"`), h('div', { class: 'ac-choices' }, revert, ...others),
        keep ? h('div', { class: 'ac-choices' }, keep) : null);
      placeBubble(span.getBoundingClientRect());
    };
    // Opened by a click or a tap on a corrected word - never by the pointer passing over it (D156,
    // the user 2026-10-08: on hover it opened and then stayed). Typing, Escape or a click elsewhere
    // closes it.
    box.addEventListener('click', () => {
      if (box.selectionStart !== box.selectionEnd) return;
      const at = box.selectionStart;
      const index = state.fixes.findIndex((f) => at >= f.start && at <= f.end);
      if (index >= 0) openBubble(index); else closeBubble();
    });
    // Right-click a word, while autocorrect is on, to add it to your words (D157). Off - or with text
    // selected, or with Shift held - the browser's own menu, so copy and paste stay where they were.
    box.addEventListener('contextmenu', (event) => {
      if (!state.on || event.shiftKey || box.selectionStart !== box.selectionEnd) return;
      const text = box.value;
      let start = box.selectionStart;
      let end = start;
      while (start > 0 && /[A-Za-z'-]/.test(text[start - 1])) start -= 1;
      while (end < text.length && /[A-Za-z'-]/.test(text[end])) end += 1;
      // A word autocorrect changed: the same bubble as a left click (D161).
      const fixed = state.fixes.findIndex((f) => box.selectionStart >= f.start && box.selectionStart <= f.end);
      if (fixed >= 0) {
        event.preventDefault();
        closeBubble();
        openBubble(fixed);
        return;
      }
      // A word it knows - in the list, one of yours, or one of your replacements - or a word it cannot
      // judge yet (the list still loading): the browser's own menu (D161). Only an unknown word is
      // offered to your words.
      const word = text.slice(start, end).replace(/^['-]+|['-]+$/g, '');
      if (!ownWordShape.test(word) || word.length < 2 || !words.band) return;
      const lower = word.toLowerCase();
      if (words.band.has(lower) || isTerm(lower) || state.replace.has(lower) || state.own.some((w) => w.toLowerCase() === lower)) return;
      event.preventDefault();
      closeBubble();
      const add = h('button', { type: 'button', class: 'ac-add' }, `Add "${word}" to my words`);
      add.addEventListener('click', () => { closeBubble(); addOwnWord(word); box.focus(); });
      bubble = h('div', { class: 'ac-bubble', role: 'dialog' },
        h('div', { class: 'hint' }, `"${word}" is not a word autocorrect knows`),
        h('div', { class: 'ac-choices' }, add),
        h('div', { class: 'hint' }, 'Shift+right-click for the browser\'s menu'));
      placeBubble({ left: event.clientX, bottom: event.clientY });
    });

    // Edits move later corrections with the text; one whose word is edited away is forgotten.
    box.addEventListener('beforeinput', () => {
      state.before = { start: box.selectionStart, end: box.selectionEnd, length: box.value.length };
    });
    box.addEventListener('input', async (event) => {
      if (state.ours) return;
      closeBubble();
      const before = state.before;
      if (before && state.fixes.length) {
        const delta = box.value.length - before.length;
        state.fixes = state.fixes.filter((f) => {
          if (f.end <= before.start) return true;
          if (f.start >= before.end) { f.start += delta; f.end += delta; return true; }
          return false;
        }).filter((f) => box.value.slice(f.start, f.end) === f.word);
      }
      if (!box.value) state.fixes = [];
      paint();
      if (event.inputType === 'insertText' && event.data && /[A-Za-z']/.test(event.data)) state.lettersAt = Date.now();
      if (!state.on || event.inputType !== 'insertText' || !event.data || !BOUNDARY.test(event.data)) return;
      if (state.lettersAt <= state.onSince) return;   // the word was typed before autocorrect was on
      await check(box.selectionStart - 1);
    });

    // The word ending at `end` (the boundary just typed sits there).
    async function check(end) {
      const text = box.value;
      let start = end;
      while (start > 0 && /[A-Za-z']/.test(text[start - 1])) start -= 1;
      const typed = text.slice(start, end).replace(/'+$/, '');
      if (!typed) return;
      // One letter is checked only for a known spelling - i is I (the user, 2026-10-14) - below.
      // Code and paths, always: the word must stand alone between spaces (or punctuation at its end),
      // hold letters only, be lowercase after its first letter, and not sit inside backticks.
      let tokenStart = start;
      while (tokenStart > 0 && !/\s/.test(text[tokenStart - 1])) tokenStart -= 1;
      if (text.slice(tokenStart, start).replace(/^[("'[{]+/, '') !== '') return;
      // All capitals is mostly an acronym (NTFS, VNET); it is corrected only to the same letters in a
      // common word's order - TEH is THE (D162) - and never underlined.
      const allCaps = typed.length > 2 && typed === typed.toUpperCase();
      if (!allCaps && /[A-Z]/.test(typed.slice(1))) return;
      if ((text.slice(0, start).match(/`/g) || []).length % 2 === 1) return;
      // A replacement the person set up is asked for by name, so the skip options do not apply to it.
      const swap = state.replace.get(typed.toLowerCase());
      if (swap === typed) return;
      // A word whose spelling is known - one of yours, a shipped term (OneDrive, DevOps), or the
      // one-word spelling a replacement gives - always takes that spelling, whatever the skip options
      // say (the user, 2026-10-14: "onedrive" was left alone because OneDrive was already in the
      // conversation, and a replacement's spelling had to be added again as an own word). The terms
      // come with the list, so it is loaded first.
      const ownish = [...state.own, ...spellingsOf(state.replace)];
      if (!swap) {
        try { await loadWords(); } catch (error) { /* own words still apply */ }
      }
      const known = swap ? null : correctionFor(typed, ownish, true);
      if (typed.length < 2 && !known) return;                                // a, x: never guessed at
      if (!swap && !known && knownSpelling(typed, ownish)) return;          // already spelt that way
      if (!swap && !known && state.skip.includes('capitalised') && typed[0] !== typed[0].toLowerCase()) return;
      if (!swap && !known && state.skip.includes('clients') && (await clientWords()).has(typed.toLowerCase())) return;
      if (!swap && !known && state.skip.includes('used')) {
        const elsewhere = `${text.slice(0, start)} ${text.slice(end)} ${opts.conversation ? opts.conversation() : ''}`.toLowerCase();
        if (new RegExp(`\\b${typed.toLowerCase()}\\b`).test(elsewhere)) return;
      }
      // The word must still be where it was, though typing may have gone on after it while the list
      // loaded.
      const ownFirst = swap ? { best: swap, alternatives: [] } : known;
      if (!ownFirst && !words.band) return;                                 // the list could not load
      if (box.value.slice(start, start + typed.length) !== typed || /[A-Za-z']/.test(box.value[start + typed.length] || '')) return;
      let fix = ownFirst || correctionFor(typed, ownish);
      const sorted = (w) => [...w.toLowerCase()].sort().join('');
      if (fix && allCaps && !ownFirst
          && !(sorted(fix.best) === sorted(typed) && (words.band.get(fix.best.toLowerCase()) || 99) <= 20)) fix = null;
      if (!fix) {
        // Not known and nothing close (D164): underlined red; clicking it offers to add it. Not an
        // acronym, not a word it knows, not one already marked.
        const lower = typed.toLowerCase();
        if (allCaps || words.band.has(lower) || knownContraction(lower, state.own)
            || isTerm(lower) || state.own.some((w) => w.toLowerCase() === lower) || state.fixes.some((f) => f.start === start)) return;
        state.fixes.push({ start, end: start + typed.length, word: typed, original: typed, alternatives: [], unknown: true });
        state.fixes.sort((a, b) => a.start - b.start);
        paint();
        return;
      }
      const caret = box.selectionStart;
      const caretEnd = box.selectionEnd;
      state.ours = true;
      insertText(box, start, start + typed.length, fix.best);
      state.ours = false;
      const delta = fix.best.length - typed.length;
      box.setSelectionRange(caret + (caret > start ? delta : 0), caretEnd + (caretEnd > start ? delta : 0));
      for (const other of state.fixes) if (other.start > start) { other.start += delta; other.end += delta; }
      state.fixes.push({ start, end: start + fix.best.length, word: fix.best, original: typed, alternatives: fix.alternatives });
      state.fixes.sort((a, b) => a.start - b.start);
      paint();
    }

    const one = { stack, clear() { state.fixes = []; closeBubble(); paint(); }, refresh };
    wired.add(one);
    return one;
  }
}
