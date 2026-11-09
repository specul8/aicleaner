// Remote desktops in the menu (feature 158, aicleaner-rdp): one link per host in `remote_desktop.hosts`, each opening
// that machine's aicleaner-rdp page in a new tab. Nothing is embedded here - the desktop runs on its own page, under
// its own address and sign-in, and this page's security policy is unchanged. No hosts, no entries.
//
// Feature 164 (Brad, 2026-11-05): the link carries the entry's name (#name=<name>), so the tab reads "AI Cleaner RDP -
// <name>"; the entry for the device this browser is on is greyed out - a remote desktop to the machine you are sitting
// at would only take over its own screen.
//
// Feature 168 (Brad, 2026-11-08): which device that is comes from the question device.js asks once on a remote page,
// or, on the page at this machine, from this machine's own name. The probe of http://127.0.0.1:8790/here is gone: it
// raised the browsers' "access the local network" prompt, unexplained, on devices with nothing there to answer it.

import { getJSON } from './api.js';
import { askDevice, deviceName, deviceUrl, forgetDevice, shouldAsk } from './device.js';
import { h } from './dom.js';

const MARK = '\u{1F5A5}️';                       // a desktop computer, as the other menu entries have marks

const state = { hosts: [], remote: false, machine: '', asked: false };

function same(a, b) {
  return Boolean(a) && Boolean(b) && String(a).toLowerCase() === String(b).toLowerCase();
}

// The entry for the device this browser is on, or null.
function here() {
  if (!state.remote) return state.hosts.find((host) => same(host.device, state.machine) || same(host.name, state.machine)) || null;
  const url = deviceUrl();
  const name = deviceName();
  return state.hosts.find((host) => (url && host.url === url) || same(host.device, name) || same(host.name, name)) || null;
}

function linkFor(host) {
  return host.url.includes('#') ? host.url : `${host.url}#name=${encodeURIComponent(host.name)}`;
}

function render() {
  const list = document.getElementById('nav-remote');
  if (!list) return;
  const mine = here();
  list.replaceChildren(...state.hosts.map((host) => {
    const mark = h('span', { class: 'nav-mark', 'aria-hidden': 'true' }, MARK);
    const text = h('span', { class: 'nav-text' }, host.name);
    if (mine && mine === host) {
      return h('a', { class: 'nav-remote nav-disabled', 'aria-disabled': 'true', 'data-here': 'true',
        title: `Remote desktop: ${host.name} - the device you are on` }, mark, text);
    }
    return h('a', {
      class: 'nav-remote', href: linkFor(host), target: '_blank', rel: 'noopener noreferrer',
      title: `Remote desktop: ${host.name} (${host.url}) - opens in a new tab`,
    }, mark, text);
  }));
}

// Settings > Remote desktops: ask again now (this browser).
export async function askAgainWhichDevice() {
  forgetDevice();
  await askDevice(state.hosts);
  render();
}

export function renderRemoteDesktops(settings) {
  state.hosts = (settings && settings['remote_desktop.hosts']) || [];
  render();
  // The first page opened through remote access asks, once its list of entries is known (f168).
  if (state.remote && !state.asked && shouldAsk()) {
    state.asked = true;
    askDevice(state.hosts).then(render);
  }
}

export function setupRemoteDesktops(status) {
  state.remote = Boolean(status && status.remote);
  state.machine = (status && status.host) || '';
  getJSON('/api/config').then((config) => renderRemoteDesktops(config.settings || {})).catch(() => {});
  window.addEventListener('aicleaner:settings', (event) => {
    if (event.detail && 'remote_desktop.hosts' in event.detail) renderRemoteDesktops(event.detail);
  });
  window.addEventListener('aicleaner:device', render);
}
