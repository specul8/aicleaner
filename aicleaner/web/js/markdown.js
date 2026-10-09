// Markdown preview for a file tab (feature 54; DECISIONS 50).
//
// Builds DOM nodes with h(), never an HTML string, so nothing in the file can become markup: raw
// HTML is shown as the text it is, and only http, https and mailto addresses become links. An
// image is shown only when its path resolves to a file in the session's folders; a remote one is
// its description and a link, never loaded.
// Covers what notes and handoffs use - headings, paragraphs, emphasis, code, lists (nested),
// block quotes, tables, links and rules - not the whole of CommonMark.

import { h } from './dom.js';

const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+-]*)/;
const HEADING = /^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
const RULE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^ {0,3}>\s?/;
const LIST = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const LINK = /^\[([^\]]*)\]\(\s*([^\s)]+)(?:\s+"[^"]*")?\s*\)/;
const PUNCT = /[!-/:-@[-`{-~]/;
const SAFE_SCHEMES = new Set(['http:', 'https:', 'mailto:']);

function safeHref(url) {
  try { return SAFE_SCHEMES.has(new URL(url).protocol) ? url : null; } catch (error) { return null; }
}

// A heading's anchor, as GitHub and VS Code make it: lower case, punctuation dropped, spaces to
// hyphens - so "## 6. Auto-add versus a human decision" is #6-auto-add-versus-a-human-decision.
export function slug(text) {
  return String(text).trim().toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu, '').replace(/\s/g, '-');
}

// A link to a heading in the same document (2026-10-04: these were printed as written). Not a real
// #address: the app routes on the hash, so following one would leave the page. It scrolls to the
// heading inside the document it sits in instead.
function anchorLink(target, children) {
  const link = h('a', { href: `#${target}`, class: 'md-anchor', dataset: { anchor: target } }, children);
  link.addEventListener('click', (event) => {
    event.preventDefault();
    const doc = link.closest('.md-doc') || document;
    const heading = doc.querySelector(`[id="${CSS.escape(target)}"]`);
    if (heading) heading.scrollIntoView({ block: 'start' });
  });
  return link;
}

const isSpace = (c) => c === undefined || /\s/.test(c);
const isPunct = (c) => c !== undefined && PUNCT.test(c);
const isWord = (c) => c !== undefined && /[\p{L}\p{N}]/u.test(c);

// Where the emphasis run `mark` opened before `from` closes, or -1. A closer has no space before it,
// and after punctuation only closes at a word's end - so `window.__name` never closes a run.
function closer(text, from, mark) {
  for (let j = from; j < text.length; j++) {
    const c = text[j];
    if (c === '\\') { j++; continue; }
    if (c === '`') { const end = text.indexOf('`', j + 1); if (end > -1) { j = end; continue; } }
    if (mark.length === 1 && c === mark && text[j + 1] === mark) { j++; continue; }
    if (j === from || !text.startsWith(mark, j)) continue;
    const before = text[j - 1];
    const after = text[j + mark.length];
    if (isSpace(before)) continue;
    if (isPunct(before) && !(isSpace(after) || isPunct(after))) continue;
    if (mark[0] === '_' && isWord(after)) continue;
    return j;
  }
  return -1;
}

// What an image may load from (the user, 2026-09-30): only a path the caller resolves - a file
// tab resolves paths inside the session's folders. An address with a scheme (https, http, data,
// javascript) or a host is never loaded: a remote image tells its host the file was opened, when
// and from where. A safe one becomes a link to open by choice.
function imageSource(url, opts) {
  if (/^[a-z][a-z0-9+.-]*:|^\/\//i.test(url)) return null;
  return opts.localImage ? opts.localImage(url) : null;
}

// An image that is not shown: its description, and its address - a link when it is a safe one.
function described(alt, url) {
  const href = safeHref(url);
  return h('span', { class: 'md-image' }, `[image: ${alt || 'no description'}] `,
    href ? h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, url) : url);
}

// One that fails to load - a 404, a path the server refuses - becomes its description and address.
function image(alt, url, src) {
  const img = h('img', { src, alt, class: 'md-img', loading: 'lazy' });
  img.addEventListener('error', () => img.replaceWith(described(alt, url)), { once: true });
  return img;
}

export function inline(text, opts = {}) {
  const out = [];
  let plain = '';
  const flush = () => { if (plain) { out.push(plain); plain = ''; } };
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === '\\' && isPunct(text[i + 1])) { plain += text[i + 1]; i += 2; continue; }
    if (c === '`') {
      const run = /^`+/.exec(text.slice(i))[0];
      const end = text.indexOf(run, i + run.length);
      if (end > -1) {
        flush();
        out.push(h('code', null, text.slice(i + run.length, end).replace(/^ (.+) $/s, '$1')));
        i = end + run.length;
      } else { plain += run; i += run.length; }
      continue;
    }
    if (c === '!' && text[i + 1] === '[') {
      const found = LINK.exec(text.slice(i + 1));
      if (found) {
        flush();
        const src = imageSource(found[2], opts);
        out.push(src ? image(found[1], found[2], src) : described(found[1], found[2]));
        i += 1 + found[0].length;
        continue;
      }
    }
    if (c === '[') {
      const found = LINK.exec(text.slice(i));
      if (found) {
        const href = safeHref(found[2]);
        if (href) { flush(); out.push(h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, inline(found[1], opts))); }
        else if (/^#[^\s#]+$/.test(found[2])) { flush(); out.push(anchorLink(found[2].slice(1), inline(found[1], opts))); }
        // A relative link the caller knows how to open - the wiki's own pages (2026-10-07).
        else if (opts.localLink && opts.localLink(found[2])) {
          flush();
          const local = opts.localLink(found[2]);
          out.push(h('a', local.external ? { href: local.href, target: '_blank', rel: 'noopener noreferrer' }
            : { href: local.href }, inline(found[1], opts)));
        }
        else plain += found[0];
        i += found[0].length;
        continue;
      }
    }
    if (c === '*' || c === '_') {
      const mark = text[i + 1] === c ? c + c : c;
      const start = i + mark.length;
      const opens = !isSpace(text[start]) && !(c === '_' && isWord(text[i - 1]));
      const end = opens ? closer(text, start, mark) : -1;
      if (end > -1) {
        flush();
        out.push(h(mark.length === 2 ? 'strong' : 'em', null, inline(text.slice(start, end), opts)));
        i = end + mark.length;
        continue;
      }
      plain += mark; i += mark.length;
      continue;
    }
    plain += c; i++;
  }
  flush();
  return out;
}

function cells(line) {
  const row = line.trim().replace(/^\|/, '').replace(/(^|[^\\])\|$/, '$1');
  return row.split(/(?<!\\)\|/).map((cell) => cell.trim());
}

function isTableStart(lines, i) {
  return lines[i].includes('|') && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1]) && lines[i + 1].includes('-');
}

function startsBlock(lines, i) {
  const line = lines[i];
  return !line.trim() || FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line)
    || LIST.test(line) || isTableStart(lines, i);
}

function table(lines, i, opts) {
  const aligns = cells(lines[i + 1]).map((c) => (c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : ''));
  // A cell holding one short word ("On", "Built-in", "90") is never broken across lines: in a table
  // whose other columns hold sentences, the browser otherwise squeezes it to "Built-/in" (D139).
  const short = (text) => text.length <= 24 && !/\s/.test(text.trim());
  const cell = (tag, text, n) => h(tag, { class: [aligns[n] ? `md-${aligns[n]}` : '', short(text) ? 'md-nowrap' : '']
    .filter(Boolean).join(' ') || null }, inline(text, opts));
  const head = h('thead', null, h('tr', null, cells(lines[i]).map((text, n) => cell('th', text, n))));
  const body = h('tbody');
  let j = i + 2;
  for (; j < lines.length && lines[j].trim() && lines[j].includes('|'); j++) {
    body.append(h('tr', null, cells(lines[j]).map((text, n) => cell('td', text, n))));
  }
  return [h('div', { class: 'md-table' }, h('table', null, head, body)), j];
}

// A list and any list indented under its items; a blank line or a line that is not an item ends it.
function list(lines, i, opts) {
  const first = LIST.exec(lines[i]);
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const number = ordered ? parseInt(first[2], 10) : 1;
  const node = h(ordered ? 'ol' : 'ul', { start: ordered && number !== 1 ? number : null });
  while (i < lines.length) {
    const item = LIST.exec(lines[i]);
    if (!item || item[1].length < indent) break;
    if (item[1].length > indent) {
      const [nested, next] = list(lines, i, opts);
      if (!node.lastElementChild) node.append(h('li'));
      node.lastElementChild.append(nested);
      i = next;
      continue;
    }
    if (/\d/.test(item[2]) !== ordered) break;
    const parts = [item[3]];
    for (i++; i < lines.length && lines[i].trim() && /^\s/.test(lines[i]) && !LIST.test(lines[i]); i++) parts.push(lines[i].trim());
    node.append(h('li', null, inline(parts.join('\n'), opts)));
  }
  return [node, i];
}

// Each block made here carries the source lines it came from (data-line-from, data-line-to), so a
// double-click in a preview can find the same place in the source (feature 92). `base` is where
// `lines` starts in the whole document: a quote's lines are its own sub-list.
function blocks(lines, opts, base = 0) {
  const out = [];
  let i = 0;
  let start = 0;
  let tagged = 0;
  const tag = (end) => {
    if (out.length > tagged) {
      const node = out[out.length - 1];
      if (node && node.dataset) { node.dataset.lineFrom = String(base + start); node.dataset.lineTo = String(base + end); }
      tagged = out.length;
    }
  };
  while (i < lines.length) {
    tag(i - 1);
    start = i;
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const fence = FENCE.exec(line);
    if (fence) {
      const body = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith(fence[1]); i++) body.push(lines[i]);
      i++;
      out.push(h('pre', { class: 'md-code' }, h('code', { dataset: fence[2] ? { lang: fence[2] } : null }, body.join('\n'))));
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      // An id to link to; a second heading of the same text gets -1, -2, as on GitHub.
      const seen = opts.slugs || (opts.slugs = new Map());
      const base = slug(heading[2].replace(/[*_`]/g, ''));
      const count = seen.get(base) || 0;
      seen.set(base, count + 1);
      out.push(h(`h${heading[1].length}`, { id: count ? `${base}-${count}` : base }, inline(heading[2], opts)));
      i++;
      continue;
    }
    if (RULE.test(line)) { out.push(h('hr')); i++; continue; }
    if (QUOTE.test(line)) {
      const quoted = [];
      const from = i;
      for (; i < lines.length && QUOTE.test(lines[i]); i++) quoted.push(lines[i].replace(QUOTE, ''));
      out.push(h('blockquote', null, blocks(quoted, opts, base + from)));
      continue;
    }
    if (isTableStart(lines, i)) { const [node, next] = table(lines, i, opts); out.push(node); i = next; continue; }
    if (LIST.test(line)) { const [node, next] = list(lines, i, opts); out.push(node); i = next; continue; }
    const para = [line.trim()];
    for (i++; i < lines.length && !startsBlock(lines, i); i++) para.push(lines[i].trim());
    out.push(h('p', null, inline(para.join('\n'), opts)));
  }
  tag(i - 1);
  return out;
}

// `opts.localImage(path)`: the address to load an image path from, or null to describe it instead.
export function renderMarkdown(text, opts = {}) {
  opts = { ...opts, slugs: new Map() };             // heading anchors are per document
  const fragment = document.createDocumentFragment();
  fragment.append(...blocks(String(text || '').split(/\r?\n/), opts));
  return fragment;
}
