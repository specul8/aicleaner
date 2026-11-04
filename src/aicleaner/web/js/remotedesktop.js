// Remote desktops in the menu (feature 158, aicleaner-rdp): one link per host in `remote_desktop.hosts`, each opening
// that machine's aicleaner-rdp page in a new tab. Nothing is embedded here - the desktop runs on its own page, under
// its own address and sign-in, and this page's security policy is unchanged. No hosts, no entries.

import { getJSON } from './api.js';
import { h } from './dom.js';

const MARK = '\u{1F5A5}️';                       // a desktop computer, as the other menu entries have marks

export function renderRemoteDesktops(settings) {
  const list = document.getElementById('nav-remote');
  if (!list) return;
  const hosts = (settings && settings['remote_desktop.hosts']) || [];
  list.replaceChildren(...hosts.map((host) => h('a', {
    class: 'nav-remote', href: host.url, target: '_blank', rel: 'noopener noreferrer',
    title: `Remote desktop: ${host.name} (${host.url}) - opens in a new tab`,
  }, h('span', { class: 'nav-mark', 'aria-hidden': 'true' }, MARK), h('span', { class: 'nav-text' }, host.name))));
}

export function setupRemoteDesktops() {
  getJSON('/api/config').then((config) => renderRemoteDesktops(config.settings || {})).catch(() => {});
  window.addEventListener('aicleaner:settings', (event) => {
    if (event.detail && 'remote_desktop.hosts' in event.detail) renderRemoteDesktops(event.detail);
  });
}
