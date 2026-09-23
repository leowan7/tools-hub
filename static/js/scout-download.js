// Guards a download anchor so a refusal is READ, not swallowed.
//
// Used by the Scout CSV anchors (templates/scout/index.html,
// templates/scout/feasibility.html) and the AF2 PDB/PAE anchors
// (templates/tools/af2_results.html). The file keeps its scout- name because
// renaming it moves every template and test that names it, plus the
// passthrough flag below, for no change in behaviour.
//
// An unguarded anchor loses the answer in one of two ways. On Scout the links
// carry <a download>, so a non-2xx never reaches the user: Chrome writes no
// file and logs a failed download, Firefox saves the JSON error body to disk
// AS the .csv. Every refusal those routes compose -- the job reaped
// (scout/jobs.py cleanup_old_jobs, 1 h), an unreadable file, a chain that no
// longer matches -- was invisible. On AF2 the links carry no attribute, so a
// refusal NAVIGATES instead: the bare text/plain body replaces the results
// page and its NGL viewer. Either way the click stops being a download and
// nothing on the page says so.
//
// Both share one refusal that is invisible on every anchor: login_required
// (shared/auth.py) answers a dead session with a 302 to /login, which fetch
// FOLLOWS and a browser navigates to, so a stale tab's click just becomes a
// login form with no mention of the download.
//
// So the click is probed first: show the server's own message on a refusal,
// and let the real download run only on a clean response.
//
// Deleting the `download` attribute instead does NOT work and was reverted
// (97c275e): the HTML download algorithm checks Content-Disposition BEFORE the
// attribute, so removing it changes no filename, and it turns a refusal into a
// full navigation off the rendered report and the live 3Dmol viewer.
(function () {
  'use strict';

  // 'job', not 'analysis': this string is shared, and on af2_results.html the
  // work is a fold. Every use below is the same case -- a refusal the guard
  // has nothing quotable from -- e.g. the html 404 the af2_download_* routes
  // in blueprints/jobs.py return when the job is not yours or not an af2 job.
  var GENERIC = 'This download is no longer available. Please re-run the job.';
  var EXPIRED = 'Your session has expired. Please sign in again to download.';
  var OFFLINE = 'Could not reach the server. Please check your connection and try again.';

  // A text/plain refusal body as one line the page can show. These routes
  // write it as a shell-style comment ("# No PDB in this job's result."
  // then a newline), which suits a file saved to disk, not a sentence read
  // on a page. Returns '' -- and so GENERIC at the call site -- for a body
  // that is blank or nothing but the marker, because an empty surface is
  // the silence this guard exists to end.
  // tests/test_af2_download_refusals.py
  // test_a_body_with_no_words_left_is_never_shown_as_an_empty_box.
  // Capped because this branch quotes a whole response BODY; the JSON
  // branch below quotes one field the route composed. trim() alone, no
  // regex, because these bodies are one line.
  function plainMessage(body) {
    var s = String(body || '').trim();
    if (s.charAt(0) === '#') { s = s.slice(1).trim(); }
    return s.length > 200 ? s.slice(0, 200) + '...' : s;
  }

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
        // shared/auth.py::login_required answers a dead session with a 302 to
        // /login, which fetch FOLLOWS to a 200 HTML page. Without this redirect
        // check that reads as success and the browser saves the login page as a
        // .csv.
        if (r.redirected) { release(); showError(EXPIRED); return; }
        if (r.ok) {
          release();
          link._scoutPassthrough = true;
          link.click();
          return;
        }
        // Refusals come in two shapes. Scout's routes compose JSON
        // {"error": ...}; the AF2 routes answer text/plain (blueprints/jobs.py
        // af2_download_pdb, af2_download_pae). Anything else -- an HTML 500,
        // render_template("404.html") -- is not ours to quote and falls to
        // GENERIC, as does a response that declares no type at all, which
        // keeps every JSON caller on the branch it has always taken.
        // tests/test_af2_download_refusals.py.
        var ctype = (r.headers && r.headers.get
          && r.headers.get('content-type')) || '';
        if (ctype.indexOf('text/plain') === 0) {
          return r.text().then(
            function (body) { showError(plainMessage(body) || GENERIC); },
            function () { showError(GENERIC); }
          );
        }
        return r.json().then(
          function (body) { showError((body && body.error) || GENERIC); },
          function () { showError(GENERIC); }
        );
      }, function () { if (link.href === url) { showError(OFFLINE); } });
    };
  };
}());
