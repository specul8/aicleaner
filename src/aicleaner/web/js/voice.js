// Voice to text in the Session page's prompt (feature 166, Brad 2026-11-07/08). A Microphone button between Code and
// Upload; with it on, Space talks, in either of two ways - each switchable in Settings > Prompt:
// - hold Space for a moment (prompt.voice_hold_tenths, 3 = a third of a second) and it listens until you let go; a
//   quick tap still types a space;
// - on an empty prompt, Space starts listening and Space again stops it.
// The words go in at the cursor. The browser's own speech recognition does the listening: in Edge and Chrome the
// audio goes to the browser maker's speech service (Microsoft's for Edge), outside AI Cleaner's filter - the text
// that results is filtered like anything typed. Firefox has none: the button is greyed there.

import { getJSON } from './api.js';
import { h } from './dom.js';

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition || null;

export function voiceSupported() { return Boolean(Recognition); }

// Text put in at the cursor of a prompt box, as typing would, so the prompt's own listeners hear of it.
function insertAt(box, words) {
  const start = box.selectionStart ?? box.value.length;
  const end = box.selectionEnd ?? start;
  const before = box.value.slice(0, start);
  const spaced = (before && !/\s$/.test(before) ? ' ' : '') + words.trim();
  box.setRangeText(spaced, start, end, 'end');
  box.dispatchEvent(new Event('input', { bubbles: true }));
}

// The Microphone for one prompt. `root` holds the prompt's text boxes; `isEmpty()` says whether the whole prompt is.
export function attachVoice(root, { isEmpty }) {
  const settings = { hold: true, toggle: true, tenths: 3 };
  const apply = (s) => {
    if ('prompt.voice_hold' in s) settings.hold = s['prompt.voice_hold'] !== false;
    if ('prompt.voice_toggle' in s) settings.toggle = s['prompt.voice_toggle'] !== false;
    if ('prompt.voice_hold_tenths' in s) settings.tenths = Math.min(20, Math.max(1, Number(s['prompt.voice_hold_tenths']) || 3));
  };
  getJSON('/api/config').then((config) => apply(config.settings || {})).catch(() => {});
  const onSettings = (event) => { if (button.isConnected) apply(event.detail || {}); else window.removeEventListener('aicleaner:settings', onSettings); };
  window.addEventListener('aicleaner:settings', onSettings);

  const label = 'Microphone: with it on, hold Space to talk (a tap still types a space), or press Space on an empty '
    + 'prompt to start and stop. Your browser\'s speech service turns the speech into text (Microsoft\'s, in Edge) - '
    + 'the audio does not go through AI Cleaner; the text does, filtered as typed.';
  const button = h('button', { type: 'button', id: 'session-voice', class: 'btn voice-button', 'aria-pressed': 'false',
    'aria-label': 'Microphone', title: Recognition ? label : 'This browser has no speech recognition (Firefox): use Edge or Chrome',
    disabled: Recognition ? null : true }, '\u{1F3A4}\u{FE0E} Mic');
  const state = { armed: false, listening: false, how: '', recognition: null, box: null, timer: 0, held: false };

  const show = () => {
    button.setAttribute('aria-pressed', String(state.armed));
    button.classList.toggle('armed', state.armed);
    button.classList.toggle('listening', state.listening);
    root.classList.toggle('voice-listening', state.listening);
    button.textContent = state.listening ? '\u{1F3A4}\u{FE0E} Listening...' : '\u{1F3A4}\u{FE0E} Mic';
  };

  const stop = () => {
    state.listening = false;
    state.how = '';
    if (state.recognition) { try { state.recognition.stop(); } catch (e) { /* already stopped */ } }
    state.recognition = null;
    show();
  };

  const start = (box, how) => {
    if (!Recognition || state.listening) return;
    const recognition = new Recognition();
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = navigator.language || 'en-AU';
    recognition.onresult = (event) => {
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        if (event.results[i].isFinal) insertAt(box, event.results[i][0].transcript);
      }
    };
    recognition.onend = () => { if (state.recognition === recognition) stop(); };
    recognition.onerror = () => { if (state.recognition === recognition) stop(); };
    state.recognition = recognition;
    state.box = box;
    state.listening = true;
    state.how = how;
    show();
    try { recognition.start(); } catch (e) { stop(); }
  };

  button.addEventListener('click', () => {
    state.armed = !state.armed;
    if (!state.armed) stop();
    show();
  });

  root.addEventListener('keydown', (event) => {
    const box = event.target;
    if (!state.armed || !(box instanceof HTMLTextAreaElement) || event.key !== ' ' || event.ctrlKey || event.altKey || event.metaKey) {
      if (event.key === 'Escape' && state.listening) stop();
      return;
    }
    if (event.repeat) { if (state.timer || state.held) event.preventDefault(); return; }
    if (state.listening && state.how === 'toggle') { event.preventDefault(); stop(); return; }
    if (settings.toggle && isEmpty()) { event.preventDefault(); start(box, 'toggle'); return; }
    if (!settings.hold) return;
    event.preventDefault();
    state.held = false;
    state.timer = window.setTimeout(() => { state.timer = 0; state.held = true; start(box, 'hold'); }, settings.tenths * 100);
  });

  root.addEventListener('keyup', (event) => {
    if (event.key !== ' ') return;
    if (state.timer) {                       // a tap: the space it would have typed
      window.clearTimeout(state.timer);
      state.timer = 0;
      const box = event.target;
      if (box instanceof HTMLTextAreaElement) {
        box.setRangeText(' ', box.selectionStart, box.selectionEnd, 'end');
        box.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return;
    }
    if (state.held) { state.held = false; if (state.how === 'hold') stop(); }
  });

  return { button, stop, state, settings };
}
