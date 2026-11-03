// D216 (spec 2026-10-28-d216-session-pictures.md): pictures attached to the Session page's prompt, as chips above
// it - a thumbnail, the name and an x. Sent with the prompt as image blocks; the proxy then redacts them, sends them
// as they are, or puts their text in their place, per Pictures (feature 114). Each session has its own, as it has
// its own draft: kept for the session while the page is open (and across a reload while they fit in the tab's
// storage), shown only while that session is.
import { h } from './dom.js';

// The types the AI service takes, and its limits; the server checks the same (promptpictures.py).
const TYPES = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp' };
const READ_ONLY = { bmp: 'BMP', tif: 'TIFF', tiff: 'TIFF' };
const MAX_DATA = 5 * 1024 * 1024;           // one picture, as base64 - what the API counts
const MAX_COUNT = 20;
const MAX_TOTAL = 20 * 1024 * 1024;
const kept = new Map();                     // session name -> [{name, media_type, data}], for the page's life
const storeKey = (name) => `aicleaner.pictures.${name}`;

function extension(file) {
  const found = /\.([a-z0-9]+)$/i.exec(String(file.name || ''));
  return found ? found[1].toLowerCase() : '';
}

// 'attach' (a type the API takes), 'read' (a picture it does not: BMP, TIFF) or 'other' (a PDF): what may be done.
export function pictureKind(file) {
  const type = String(file.type || '').toLowerCase();
  if (Object.values(TYPES).includes(type) || TYPES[extension(file)]) return 'attach';
  if (type === 'image/bmp' || type === 'image/tiff' || READ_ONLY[extension(file)]) return 'read';
  return 'other';
}

export function readOnlyType(file) {
  return READ_ONLY[extension(file)] || (String(file.type || '').includes('bmp') ? 'BMP' : 'TIFF');
}

function mediaOf(file) {
  const type = String(file.type || '').toLowerCase();
  return Object.values(TYPES).includes(type) ? type : (TYPES[extension(file)] || 'image/png');
}

function base64Of(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ''));
    reader.onerror = () => reject(reader.error || new Error('the file could not be read'));
    reader.readAsDataURL(file);
  });
}

function sizeText(chars) {
  const bytes = Math.round(chars * 0.75);
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

// The chips in `holder`, for whichever session `session()` names. say(text, kind) is the page's toast.
export function attachments({ holder, session, say, note = () => '' }) {
  let pending = 0;
  const list = (name = session()) => {
    if (!kept.has(name)) {
      let saved = [];
      try { saved = JSON.parse(sessionStorage.getItem(storeKey(name)) || '[]'); } catch (error) { saved = []; }
      kept.set(name, Array.isArray(saved) ? saved.filter((p) => p && p.data && p.media_type) : []);
    }
    return kept.get(name);
  };
  const save = (name) => {
    try {
      const pictures = list(name);
      if (pictures.length) sessionStorage.setItem(storeKey(name), JSON.stringify(pictures));
      else sessionStorage.removeItem(storeKey(name));
    } catch (error) {
      // Over what the tab may store: kept in the page for this visit, gone on a reload.
      try { sessionStorage.removeItem(storeKey(name)); } catch (ignored) { /* storage refused */ }
    }
  };
  const paint = () => {
    const pictures = list();
    const said = pictures.length ? note() : '';
    holder.replaceChildren(said ? h('span', { class: 'picture-note' }, said) : '', ...pictures.map((picture, index) => {
      const remove = h('button', { type: 'button', class: 'picture-remove', 'aria-label': `Remove ${picture.name}`,
        title: `Remove ${picture.name}` }, 'x');
      remove.addEventListener('click', () => {
        const name = session();
        list(name).splice(index, 1);
        save(name);
        paint();
      });
      return h('span', { class: 'picture-chip', title: `${picture.name} (${sizeText(picture.data.length)}) - sent with the prompt` },
        h('img', { class: 'picture-thumb', src: `data:${picture.media_type};base64,${picture.data}`, alt: '' }),
        h('span', { class: 'picture-name' }, picture.name), remove);
    }));
    holder.hidden = !pictures.length;
  };
  const add = async (file) => {
    const name = session();
    const label = file.name || 'pasted picture';
    pending += 1;
    try {
      const data = await base64Of(file);
      const pictures = list(name);
      if (pictures.length >= MAX_COUNT) {
        say(`${label} was not attached: a prompt takes up to ${MAX_COUNT} pictures.`, 'warn');
        return false;
      }
      if (data.length > MAX_DATA) {
        say(`${label} was not attached: it is ${sizeText(data.length)}, over the 5 MB the AI service takes for one picture.`, 'warn');
        return false;
      }
      const total = pictures.reduce((sum, p) => sum + p.data.length, 0) + data.length;
      if (total > MAX_TOTAL) {
        say(`${label} was not attached: the pictures would come to over 20 MB. Send these first.`, 'warn');
        return false;
      }
      pictures.push({ name: label, media_type: mediaOf(file), data });
      save(name);
      if (name === session()) paint();
      return true;
    } catch (error) {
      say(`Could not attach ${label}: ${error.message}`, 'error');
      return false;
    } finally {
      pending -= 1;
    }
  };
  return {
    add,
    paint,
    pending: () => pending,
    // What goes with the prompt: [{name, media_type, data}].
    payload: (name = session()) => list(name).map(({ name: n, media_type: media, data }) => ({ name: n, media_type: media, data })),
    clear: (name = session()) => { kept.set(name, []); save(name); if (name === session()) paint(); },
    // Pictures handed back by a cancelled waiting prompt, ahead of any attached since; past the count, said.
    restore: (pictures, name = session()) => {
      const room = Math.max(0, MAX_COUNT - list(name).length);
      if (pictures.length > room) say(`${pictures.length - room} picture(s) of the cancelled prompt were not attached again: a prompt takes up to ${MAX_COUNT}.`, 'warn');
      kept.set(name, [...pictures.slice(0, room).map(({ name: n, media_type: media, data }) => ({ name: n, media_type: media, data })), ...list(name)]);
      save(name);
      if (name === session()) paint();
    },
  };
}
