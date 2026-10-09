// The wiki, inside aicleaner (2026-10-07): docs/wiki/<Page>.md from this install's own copy, drawn
// with the file preview's Markdown renderer. Links between wiki pages stay here. The registers are
// development records and stay out of the public repository (feature 102), so the wiki does not
// link to them.

import { getJSON, sayError } from '../api.js';
import { h } from '../dom.js';
import { renderMarkdown } from '../markdown.js';

// `Settings.md#card-<id>` -> that card on the Settings page itself (the user, 2026-10-09: every
// reference to a card links to it); on GitHub the same link opens the Settings page.
// `Review.md` or `Review.md#x` -> this page; anything else -> nothing.
export function wikiLink(url) {
  const card = /^Settings\.md#card-([a-z0-9-]+)$/.exec(url);
  if (card) return { href: `#/settings/${card[1]}` };
  const page = /^([A-Za-z0-9-]+)\.md(?:#[^\s]*)?$/.exec(url);
  if (page) return { href: `#/wiki/${page[1]}` };
  return null;
}

export async function render(main, ctx) {
  const page = (ctx && ctx.route && ctx.route.id) || 'Home';
  const doc = h('div', { class: 'md-doc' });
  main.append(h('section', { class: 'card', id: 'wiki' }, doc));
  try {
    const answer = await getJSON(`/api/wiki/${encodeURIComponent(page)}`);
    doc.append(renderMarkdown(answer.text, { localLink: wikiLink }));
    // The renderer's own h1 is the page title; the router wants one under main to know it drew.
    const title = doc.querySelector('h1');
    document.title = `${title ? title.textContent : page} - aicleaner`;
  } catch (error) {
    doc.append(h('h1', {}, 'Wiki'), h('p', { class: 'muted' }, `No page called ${page}.`),
      h('p', {}, h('a', { href: '#/wiki' }, 'Back to the wiki home page')));
    sayError(error);
  }
}
