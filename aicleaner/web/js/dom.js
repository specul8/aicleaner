// DOM helpers. Data only ever reaches the page through textContent or attributes set here, never
// through HTML strings, so client names cannot inject markup.

const SVG_NS = 'http://www.w3.org/2000/svg';

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (key === 'value') el.value = value;
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// After an inline form submits, the page rebuilds and the cursor is lost, so entering a list of
// identifiers meant clicking back into the same box for every one. Called once the rebuild has
// finished, this puts the cursor back in the form's leftmost field, ready for the next row - the
// way moving down a column of a spreadsheet works. Selecting the text as well means a correction
// is typed over rather than cleared first.
export function focusFirst(selector) {
  const form = typeof selector === 'string' ? document.querySelector(selector) : selector;
  if (!form) return;
  // The leftmost thing you would TYPE into, which is not always the leftmost control: the Review
  // form leads with the client dropdown, and a select that has the cursor swallows typing as
  // option-matching. The client rarely changes between rows; the identifier always does.
  const field = form.querySelector('input:not([type=hidden]):not([disabled]):not([type=checkbox]):not([type=radio]), textarea')
    || form.querySelector('select:not([disabled])');
  if (!field) return;
  field.focus({ preventScroll: true });
  if (typeof field.select === 'function') field.select();
}

// Element.replaceChildren() takes anything and turns a non-Node into a text node, so a `cond ? x :
// null` argument writes the word "null" onto the page - which is how a stray "null" appeared at the
// bottom of the session file list. This drops what h() drops, so the same conditional is safe
// either side of the call.
export function setChildren(el, ...children) {
  el.replaceChildren();
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// Stroke icons (24x24 grid, drawn for this app).
const ICONS = {
  edit: ['M12 20h9', 'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z'],
  save: ['M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z', 'M17 21v-8H7v8', 'M7 3v5h8'],
  trash: ['M3 6h18', 'M8 6V4h8v2', 'M19 6l-1 14H6L5 6', 'M10 11v6', 'M14 11v6'],
  plus: ['M12 5v14', 'M5 12h14'],
  check: ['M20 6L9 17l-5-5'],
  cross: ['M18 6L6 18', 'M6 6l12 12'],
  alert: ['M12 3l10 18H2z', 'M12 10v5', 'M12 18h.01'],
  chevron: ['M9 6l6 6-6 6'],
  cog: ['M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
    'M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z'],
  restore: ['M3 12a9 9 0 1 0 3-6.7', 'M3 4v5h5'],
  // The tunnel's lock (D166): drawn, not emoji - a touch screen's emoji font drew the open lock so
  // like the closed one that "Traffic can be Observed" showed a locked padlock. The open shackle is
  // swung up and well clear of the body, so the two cannot be confused at any size.
  padlock: ['M5 11h14v10H5z', 'M8 11V7a4 4 0 0 1 8 0v4'],
  'padlock-open': ['M5 11h14v10H5z', 'M8 11V6a4 4 0 0 1 7.8-1.3'],
  download: ['M12 3v12', 'M7 10l5 5 5-5', 'M5 21h14'],
  archive: ['M3 4h18v4H3z', 'M5 8v12h14V8', 'M10 12h4'],
  // Copy to Global: a globe with an arrow rising beside it.
  globeUp: ['M15 20a7 7 0 1 0 0-14 7 7 0 0 0 0 14z', 'M8 13h14', 'M15 6c2 2 2 12 0 14', 'M15 6c-2 2-2 12 0 14', 'M4 17V4', 'M1 7l3-3 3 3'],
  move: ['M5 12h14', 'M13 6l6 6-6 6'],
  // Ignore instead: an eye, struck through.
  ignore: ['M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6', 'M3 3l18 18'],
  lock: ['M6 11h12v10H6z', 'M8 11V7a4 4 0 0 1 8 0v4'],
  // Log out: a door with an arrow leaving it (the user chose it, 2026-10-15).
  signout: ['M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4', 'M16 17l5-5-5-5', 'M21 12H9'],
  // The theme button (2026-10-15): the sun for light, the moon for dark, half of each for "as the
  // system does".
  sun: ['M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M12 2v2', 'M12 20v2', 'M4.9 4.9l1.4 1.4', 'M17.7 17.7l1.4 1.4',
    'M2 12h2', 'M20 12h2', 'M4.9 19.1l1.4-1.4', 'M17.7 6.3l1.4-1.4'],
  moon: ['M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z'],
  'sun-moon': ['M12 3a9 9 0 1 0 0 18z', 'M12 3a9 9 0 0 1 0 18', 'M12 8a4 4 0 0 1 0 8'],
};

// One emoji per DISCRETE ACTION (defect D21, feature 39, decided 2026-09-27).
//
// Five stroke glyphs were doing nine jobs. `globeUp` meant both "promote to Global" and
// "tokenise this ignored value" - two opposite things; `move` covered three destinations;
// `restore` covered re-derive, restore-last-good and salvage; and `trash` covered both an
// irreversible delete and Review's reversible dismiss. Only the `title` told them apart, so the
// distinction existed on hover and not at all on a touch screen - which is the device the tunnel
// exists to serve.
//
// The rule is the hard part, not the pictures: no emoji here stands for more than one kind of
// action. Where the same action appears at two scales - a row's Delete and the bulk Delete -
// they share, because that is the same action.
//
// Emoji carry their own colour, so `stroke: currentColor` stops applying and a glyph no longer
// takes `--bad-text` on hover the way the SVGs did. The BUTTON still carries colour - border,
// background, text - so "colour marks the action buttons" survives; recolouring the glyph itself
// is the later refinement this was chosen to move towards.
//
// This is the one place the house rule against emoji does not apply: it is a user interface, not
// a script, and that carve-out was made deliberately.
const EMOJI = {
  edit: '\u270F\uFE0F',        // pencil
  save: '\uD83D\uDCBE',        // floppy disk
  // Wastebasket - "take this away". One glyph for deleting a client entry and for clearing a row
  // from Review, because to the person doing it they are the same gesture.
  //
  // An earlier note here called this "an irreversible delete, and only that", and split Review's
  // clear onto a second glyph on the strength of it. That was the wrong distinction. Undo is a
  // mechanism we built (D42), not a property of the action - so whether a thing can be taken back
  // depends on whether that mechanism is wired to it, and it is not the icon's job to say.
  // Review still has no undo at all: that is D45, and it is a gap to close rather than a
  // difference to draw a picture of.
  trash: '\uD83D\uDDD1\uFE0F',
  plus: '\u2795',
  check: '\u2705',
  cross: '\u2716\uFE0F',       // a cancel, deliberately not the same mark as a delete
  alert: '\u26A0\uFE0F',
  cog: '\u2699\uFE0F',
  rederive: '\uD83D\uDD01',    // repeat - derive this token again under today's rules
  rewind: '\u23EA',            // restore the last good copy of a file
  patch: '\uD83E\uDE79',       // adhesive bandage - keep the valid entries of a broken file
  download: '\u2B07\uFE0F',
  archive: '\uD83D\uDCE6',
  global: '\uD83C\uDF10',      // globe - make this everyone's
  tokenise: '\uD83C\uDFF7\uFE0F', // label - give this ignored value a token instead
  move: '\u27A1\uFE0F',        // to another client
  give: '\uD83D\uDC64',        // one person - hand a Global entry to a single client
  shuffle: '\uD83D\uDD00',     // move an ignore between lists
  ignore: '\uD83D\uDE48',      // see-no-evil - stop watching for this
  lock: '\uD83D\uDD12',
};

export function icon(name, label) {
  // An emoji where one is defined; the drawn glyph otherwise. `chevron` stays a path on purpose:
  // it is structural rather than an action, CSS rotates it to show open and closed, and
  // clients.js reaches for `querySelector('svg')` to mark it.
  const glyph = EMOJI[name];
  if (glyph) {
    const span = document.createElement('span');
    span.className = 'emoji';
    span.textContent = glyph;
    if (label) {
      span.setAttribute('role', 'img');
      span.setAttribute('aria-label', label);
    } else {
      // Inside a button that already has an aria-label, the picture must not be read out too.
      span.setAttribute('aria-hidden', 'true');
    }
    return span;
  }
  return svgIcon(name, label);
}

function svgIcon(name, label) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  if (label) {
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', label);
    const title = document.createElementNS(SVG_NS, 'title');
    title.textContent = label;
    svg.append(title);
  } else {
    svg.setAttribute('aria-hidden', 'true');
  }
  for (const d of ICONS[name] || []) {
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

// A select box in a list, and the cell that holds it. The checkbox is wrapped in a label that
// fills the whole cell, so the target is the cell rather than the box: on a list of a thousand
// entries, aiming at a 13px square is the slowest part of the job. The label does the toggling
// natively - no click handler, and nothing to go wrong when a row is re-rendered under it.
export function pickBox(label, attrs) {
  return h('label', { class: 'pick-hit' },
    h('input', { type: 'checkbox', 'aria-label': label, ...(attrs || {}) }));
}

export function pickCell(label, attrs) {
  return h('td', { class: 'pick' }, pickBox(label, { class: 'pick', ...(attrs || {}) }));
}

// One group of a bulk bar: a heading that names the outcome, and the controls that reach it
// (feature 38, option C). Shared by the client pages and Review (D45), because the grouping is
// the safety property - two unrelated outcomes never sit adjacent - and not a page's decoration.
export function bulkGroup(heading, ...controls) {
  return h('div', { class: 'bulk-group' },
    h('div', { class: 'bulk-group-head' }, heading),
    h('div', { class: 'bulk-group-row' }, ...controls.filter(Boolean)));
}

const SAVED_FOR_MS = 5000;

// A Save button that says what happened, where it happened. The page-level message sits in the
// toolbar at the very top: press Save at the bottom of a long Settings page and the confirmation
// appears somewhere you cannot see, so the only way to know it worked was to scroll up and hope you
// were quick enough. The button reports instead - Saving..., then Saved for a few seconds, then
// back to Save - and an error lands in a note beside it, because an error needs words.
// Any action control, made to report on itself: disabled and labelled while its work is in
// flight, back to itself afterwards (D28). Without it a click produced no visible response, so
// it got clicked again - and the second call acted on something the first had already moved,
// failing with a message that described neither.
//
// The disabled period is the operation, not a timer: a fixed delay does not bound the window it
// exists to close, and a slow call would outlast it. `settle` holds the control a little longer
// after the label has changed into something destructive, which is the only case where a stray
// second click costs anything.
export function busyButton(button, run, { busyLabel = null, settle = 0, timeout = 20000 } = {}) {
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    const was = button.textContent;
    button.disabled = true;
    if (busyLabel) button.textContent = busyLabel;
    // fetch has no timeout of its own, so a dropped connection would leave the promise pending
    // and the control dead. This is the backstop, not the mechanism.
    const guard = setTimeout(() => { button.disabled = false; button.textContent = was; }, timeout);
    try {
      await run();
    } finally {
      clearTimeout(guard);
      if (busyLabel) button.textContent = was;
      if (settle) setTimeout(() => { button.disabled = false; }, settle);
      else button.disabled = false;
    }
  });
  return button;
}

export function savingButton(label, run, attrs) {
  const button = h('button', { type: 'button', class: 'primary save-card', ...(attrs || {}) }, label);
  const note = h('span', { class: 'save-note', role: 'status', 'aria-live': 'polite' });
  let timer = null;
  button.addEventListener('click', async () => {
    clearTimeout(timer);
    button.classList.remove('saved', 'failed');
    note.textContent = '';
    button.disabled = true;
    button.textContent = 'Saving...';
    try {
      await run();
    } catch (error) {
      button.textContent = label;
      button.classList.add('failed');
      button.disabled = false;
      note.textContent = error && error.message ? error.message : String(error);
      return;
    }
    // Usable again straight away: the label is a receipt, not a lock. Saving twice is allowed.
    button.textContent = 'Saved';
    button.classList.add('saved');
    button.disabled = false;
    timer = setTimeout(() => {
      button.textContent = label;
      button.classList.remove('saved');
    }, SAVED_FOR_MS);
  });
  return { button, note };
}

export function iconButton(name, label, attrs) {
  return h('button', { type: 'button', class: 'icon', title: label, 'aria-label': label, ...(attrs || {}) }, icon(name));
}

// Detector kind names as shown to people ("slug" meant nothing to anyone).
const KIND_LABELS = {
  slug: 'Resource identifier',
  'client-resource': 'Resource name (carries a client name)',
  resource: 'Resource name',
  email: 'Email',
  tenant: 'Tenant',
  sharepoint: 'SharePoint',
  domain: 'Domain',
  guid: 'GUID',
  ip: 'External IP address',
  'devops-org': 'DevOps organisation',
  'devops-project': 'DevOps project',
  name: 'Name',
};

export function kindLabel(kind) {
  return KIND_LABELS[kind] || kind || '';
}

// The shapes a token can be built from (mapping.TOKEN_SHAPES). Only these can be chosen when
// correcting a row: the detector kinds that are not in the list - client-resource, devops-org,
// name variants used only while detecting - describe how something was FOUND, not how its
// replacement should be spelled.
export const TOKEN_SHAPES = ['name', 'slug', 'domain', 'email', 'tenant', 'sharepoint', 'guid', 'ip'];

export function timeAgo(iso) {
  if (!iso) return '';
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

export function shortTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit' });
}
