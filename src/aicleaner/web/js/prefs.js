/* Applies the saved theme and layout before first paint, so the page never flashes the wrong
   theme. Classic script (not a module) so it runs before the body renders. */
(function () {
  var theme = 'system';
  var layout = 'side';
  var nav = 'full';
  try {
    theme = localStorage.getItem('aicleaner.theme') || 'system';
    layout = localStorage.getItem('aicleaner.layout') || 'side';
    /* The nav rail (feature 55). Applied here with the rest so the page never flashes 228px of
       nav and then snaps to 48 - the same no-flash reason the layout is read before paint. */
    nav = localStorage.getItem('aicleaner.nav') || 'full';
  } catch (e) {
    /* storage unavailable (private window, policy): defaults apply */
  }
  var dark = theme === 'dark' ||
    (theme === 'system' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  var root = document.documentElement;
  root.setAttribute('data-theme', dark ? 'dark' : 'light');
  root.setAttribute('data-theme-choice', theme);
  /* On a phone the nav goes across the top whatever was chosen (D48, D49, D66). The side layout
     kept its 228px column on a 390px screen, leaving the page 162px, so every page overflowed
     and the browser zoomed the whole thing out - which is where a tap on a field became a zoom
     and a jump, and a swipe slid the rows about. The choice is kept, and applies on a wider
     screen. Same query as NARROW in app.js. */
  var narrow = window.matchMedia && window.matchMedia('(max-width: 40rem)').matches;
  var top = narrow || layout === 'top';
  root.setAttribute('data-layout', top ? 'top' : 'side');
  /* The rail is the side menu's (the user, 2026-10-16): across the top the menu is always full, and
     the saved choice applies again when the menu goes back to the side. */
  root.setAttribute('data-nav', !top && nav === 'rail' ? 'rail' : 'full');
})();
