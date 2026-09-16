// Guards the Scout CSV download anchors so a refusal is READ, not swallowed.
//
// The links are plain <a download> anchors, so a non-2xx answer never reaches
// the user: Chrome writes no file and logs a failed download, Firefox saves the
// JSON error body to disk AS the .csv. Every refusal these routes compose --
// the job reaped (scout/jobs.py cleanup_old_jobs, 1 h), an unreadable file, a
// chain that no longer matches -- was invisible.
//
// So the click is probed first: show the server's own `error` string on a
// refusal, and let the real download run only on a clean response.
//
// Deleting the `download` attribute instead does NOT work and was reverted
// (97c275e): the HTML download algorithm checks Content-Disposition BEFORE the
// attribute, so removing it changes no filename, and it turns a refusal into a
// full navigation off the rendered report and the live 3Dmol viewer.
(function () {
  'use strict';

  var GENERIC = 'This download is no longer available. Please re-run the analysis.';
  var EXPIRED = 'Your session has expired. Please sign in again to download.';
  var OFFLINE = 'Could not reach the server. Please check your connection and try again.';

  // link: the anchor. showError: the page's own error surface, called with one
  // string. Assigned with onclick= rather than addEventListener because results
  // re-render calls this again and listeners would stack a probe per render.
  window.bindGuardedDownload = function (link, showError) {
    link.onclick = function (event) {
      // Our own click from the branch below — let the browser download it.
      // The flag lives on the ELEMENT, not in this closure: a render that
      // rebinds while a probe is in flight retires this handler, so a flag
      // kept here would be set on a function nothing calls again and the
      // click below would probe a second time.
      // tests/test_scout_download_refusals.py
      // test_a_rebind_mid_probe_still_downloads_once.
      if (link._scoutPassthrough) { link._scoutPassthrough = false; return; }
      event.preventDefault();
      // Retire the previous refusal before probing, so the surface always
      // describes THIS click. Without it the box only ever accumulates: sign
      // in again in a second tab, click, and the CSV arrives while the page
      // still reads 'Your session has expired.' Both pages hide the surface
      // on a falsy message -- tests/test_scout_download_refusals.py
      // test_an_empty_message_hides_the_surface.
      showError('');
      // The href THIS click meant. Analysing another chain repoints the
      // anchor (templates/scout/index.html:412 _handleAnalysisResult,
      // templates/scout/feasibility.html:572 renderFeasibilityResults), so by
      // the time a probe resolves the button can mean a different file.
      var url = link.href;
      fetch(url, { credentials: 'same-origin' }).then(function (r) {
        // Only the refusal branch at the bottom reads this body; the three
        // above it throw it away, and an unread body holds its transfer open.
        // On the r.ok path the passthrough click then fetches the SAME file,
        // so leaving it open sends the CSV twice -- worst on the all-patches
        // /scout/download/<id>?full=1. Guarded because a stubbed or bodyless
        // response has none. tests/test_scout_download_refusals.py
        // test_a_successful_probe_releases_the_body,
        // test_a_discarded_probe_body_is_released_too.
        function release() { if (r.body) { r.body.cancel(); } }
        // The anchor moved under this click: this answer is about a file the
        // button no longer offers. Silent on purpose -- the pane a message
        // would land in has just been re-rendered.
        if (link.href !== url) { release(); return; }
        // login_required (shared/auth.py:696) answers a dead session with a 302
        // to /login, which fetch FOLLOWS to a 200 HTML page. Without this
        // redirect check that reads as success and the browser saves the login
        // page as a .csv.
        if (r.redirected) { release(); showError(EXPIRED); return; }
        if (r.ok) {
          release();
          link._scoutPassthrough = true;
          link.click();
          return;
        }
        // Refusals are JSON {"error": ...}; anything else is not ours to quote.
        return r.json().then(
          function (body) { showError((body && body.error) || GENERIC); },
          function () { showError(GENERIC); }
        );
      }, function () { if (link.href === url) { showError(OFFLINE); } });
    };
  };
}());
