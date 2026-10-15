// Feature 113: the text in an image or PDF, read on this machine and put in the prompt box to check
// before sending. Pasted (an image on the clipboard), dropped, chosen with a button, named by a
// pasted path inside the session's folders, or picked in the file tree. The image itself is never
// sent to the model; the text is, when the person presses Send, filtered like anything typed.
import { postJSON } from './api.js';
import { h } from './dom.js';

const READABLE = /\.(png|jpe?g|gif|bmp|tiff?|webp|pdf)$/i;
const MAX_BYTES = 20 * 1024 * 1024;
let counter = 0;
let queue = Promise.resolve();

export function isReadable(name) {
  return READABLE.test(String(name || ''));
}

function base64Of(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ''));
    reader.onerror = () => reject(reader.error || new Error('the file could not be read'));
    reader.readAsDataURL(file);
  });
}

// A placeholder at the caret, replaced by the text when it comes back (or removed on failure), so
// typing can go on while the image is read and the text still lands where it was asked for.
function placeholderAt(box, label) {
  counter += 1;
  const mark = `[reading ${label}... #${counter}]`;
  const at = box.selectionStart === null ? box.value.length : box.selectionStart;
  const to = box.selectionEnd === null ? at : box.selectionEnd;
  const before = box.value.slice(0, at);
  const lead = before && !before.endsWith('\n') ? '\n' : '';
  box.value = before + lead + mark + '\n' + box.value.slice(to);
  const after = (before + lead + mark + '\n').length;
  box.setSelectionRange(after, after);
  box.dispatchEvent(new Event('input', { bubbles: true }));
  return mark;
}

// The prompt can be several text boxes (feature 118), and removing a snippet joins the box after it
// into the one before - so the mark is looked for in every box of the prompt it was put in.
function replaceMark(box, mark, text) {
  const scope = box.promptScope || null;
  const holder = [box, ...(scope ? scope.querySelectorAll('textarea') : [])].find((one) => one.value.includes(mark));
  if (!holder) return;                                 // deleted by hand meanwhile: nothing to replace
  const at = holder.value.indexOf(mark);
  holder.value = holder.value.slice(0, at) + text + holder.value.slice(at + mark.length);
  holder.dispatchEvent(new Event('input', { bubbles: true }));
}

// One read: { file } from the clipboard, a drop or the chooser; or { root, path, filename } for a
// file in the session's folders. format(text, filename): how the text goes in - a snippet on the Session
// page (feature 116), so it is read unwrapped and numbered; a marked heading when not given.
export async function readInto(box, what, sessionName, say, format = null) {
  const label = what.file ? (what.file.name || 'image') : what.filename;
  if (what.file && what.file.size > MAX_BYTES) {
    say(`${label} is over 20 MB, too big to read here.`, 'warn');
    return;
  }
  const mark = placeholderAt(box, label);
  box.promptScope = box.closest('.prompt-box-parts');  // kept: the box may be gone when the text arrives
  // One read at a time, in the order asked (the server reads one at a time per session and turns
  // a second away); each placeholder is in the box at once, so the order is visible.
  const turn = queue.then(() => readNow(box, what, sessionName, say, mark, label, format));
  queue = turn.catch(() => {});
  return turn;
}

async function readNow(box, what, sessionName, say, mark, label, format) {
  try {
    const body = what.file
      ? { name: sessionName, filename: what.file.name || 'pasted image', data: await base64Of(what.file) }
      : { name: sessionName, root: what.root, path: what.path };
    const answer = await postJSON('/api/session/read-image', body);
    const text = String(answer.text || '').replace(/^\n+|\s+$/g, '');
    const named = answer.filename || label;
    if (!text.trim()) replaceMark(box, mark, `[No text found in ${named}]`);
    else if (format) replaceMark(box, mark, format(text, named));
    else replaceMark(box, mark, `[Text read from ${named} on this machine - check it before sending]\n${text}`);
    // The Misreadings setting put a client name right (one letter out, as engines misread): said,
    // so a changed word in the snippet is not a surprise.
    if (text.trim() && answer.corrected) {
      say(`${named}: ${answer.corrected} likely misreading${answer.corrected === 1 ? '' : 's'} of a client name put right.`, 'ok');
    }
  } catch (error) {
    replaceMark(box, mark + '\n', '');
    replaceMark(box, mark, '');
    say(`Could not read ${label}: ${error.message}`, 'error');
  }
}

// A single pasted line that is a path to an image or PDF inside one of the session's folders:
// { root, path, filename }, or null. Paths anywhere else are not opened (the file tree's rule).
export function pathInFolders(text, roots) {
  const line = String(text || '').trim().replace(/^"(.*)"$/, '$1');
  if (!line || /[\r\n]/.test(line) || !isReadable(line)) return null;
  const wanted = line.replace(/\//g, '\\').toLowerCase();
  for (const root of roots || []) {
    const base = String(root).replace(/\//g, '\\').replace(/\\+$/, '');
    if (wanted.startsWith(base.toLowerCase() + '\\')) {
      const relative = line.replace(/\//g, '\\').slice(base.length + 1);
      return { root, path: relative, filename: relative.split('\\').pop() };
    }
  }
  return null;
}

// The prompt box reads what is pasted or dropped on it. A paste with text in it pastes the text as
// always (an image alongside is read too); a paste that is only an image is read instead.
export function attachImageReading(box, { sessionName, roots, say, chip, format = null }) {
  box.addEventListener('paste', (event) => {
    const items = [...(event.clipboardData ? event.clipboardData.items : [])];
    const images = items.filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item) => item.getAsFile()).filter(Boolean);
    const text = event.clipboardData ? event.clipboardData.getData('text/plain') : '';
    if (images.length && !text) event.preventDefault();
    for (const file of images) readInto(box, { file }, sessionName(), say, format);
    const found = !images.length && pathInFolders(text, roots());
    if (found) chip(found);
  });
  box.addEventListener('dragover', (event) => {
    if ([...event.dataTransfer.items].some((item) => item.kind === 'file')) event.preventDefault();
  });
  box.addEventListener('drop', (event) => {
    const files = [...(event.dataTransfer ? event.dataTransfer.files : [])].filter((file) => isReadable(file.name));
    if (!files.length) return;
    event.preventDefault();
    box.focus();
    for (const file of files) readInto(box, { file }, sessionName(), say, format);
  });
}

// "Read an image or PDF": the browser's own chooser, so it works through the tunnel and the server
// is never asked to open a path somebody typed.
export function imageButton(onFiles) {
  const input = h('input', { type: 'file', multiple: true, hidden: true,
    accept: 'image/*,.tif,.tiff,.pdf,application/pdf' });
  input.addEventListener('change', () => { onFiles([...input.files]); input.value = ''; });
  const button = h('button', { type: 'button', id: 'session-read-image', class: 'btn',
    title: 'Read the text in an image or PDF on this machine and put it in the prompt to check' }, 'Read an image or PDF');
  button.addEventListener('click', () => input.click());
  return h('span', { class: 'read-image' }, button, input);
}
