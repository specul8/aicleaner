// Titles written in the background (2026-10-17): one toast that stays while they are written and is
// rewritten as each lands - the total and what remains, both of which grow if more are added - then
// says "Title updates completed - n from n updated" and goes 20 seconds later.
import { getJSON, stickyToast } from './api.js';

const POLL_MS = 2000;
const CLEAR_MS = 20000;
let timer = 0;
let toast = null;
let seenDone = -1;
let refresh = () => {};

function words(p) {
  const failed = p.failed ? `, ${p.failed} could not be titled` : '';
  return `Writing titles: ${p.done} of ${p.total} updated, ${p.remaining} remaining${failed}`;
}

async function look() {
  let p;
  try { p = await getJSON('/api/session/conversations/titles-progress'); } catch (error) { return; }
  if (p.done !== seenDone) {
    if (seenDone >= 0) refresh();           // a title landed: Continue shows it
    seenDone = p.done;
  }
  if (p.running) {
    toast = toast || stickyToast('title-progress');
    toast.update(words(p));
    return;
  }
  clearInterval(timer);
  timer = 0;
  if (toast) {
    toast.finish(`Title updates completed - ${p.done} from ${p.total} updated`, CLEAR_MS);
    toast = null;
    refresh();
  }
  seenDone = -1;
}

// Starts watching (or keeps watching) the background titles; onTitle() is called as each lands.
// Called when Suggest titles is pressed and when the Session page opens, so a run still going after
// a reload is shown too.
// `started`: titles were just added, so the toast shows at once - and still says it finished when the
// whole run ends before the first look.
export function watchTitles(onTitle, started = false) {
  if (onTitle) refresh = onTitle;
  if (started && !toast) {
    toast = stickyToast('title-progress');
    toast.update('Writing titles...');
  }
  if (timer) return;
  timer = setInterval(look, POLL_MS);
  look();
}
