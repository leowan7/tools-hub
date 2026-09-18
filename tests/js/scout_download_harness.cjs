// Runs the REAL static/js/scout-download.js against a stubbed anchor + fetch.
//
// argv[2] is that shipped file. Nothing here re-implements the guard: the
// scenarios below drive it and report emitted behaviour (what the user was
// told, and whether a download actually ran), so deleting a rule shows up as a
// behaviour change rather than a string mismatch.
//
// .cjs, not .js — the directory ABOVE this repo carries a package.json with
// "type": "module", which would make node treat a bare .js as ESM. Same reason
// tests/js/scout_refusal_harness.cjs uses it.
const fs = require('fs');

global.window = {};
// eslint-disable-next-line no-eval
eval(fs.readFileSync(process.argv[2], 'utf8'));
const bind = global.window.bindGuardedDownload;

// The anchor stub. click() re-enters onclick exactly as a browser does, which
// is what makes the passthrough flag testable: if the guard did not let its own
// click through, _downloads stays 0 and the user gets nothing.
function makeLink(href) {
  return {
    href: href,
    onclick: null,
    _downloads: 0,
    _got: [],
    click() {
      const ev = {
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
      };
      this.onclick(ev);
      // preventDefault not called => the browser performs the download, of
      // whatever the anchor points at NOW -- which is the whole question when
      // a render moves it while a probe is in flight.
      if (!ev.defaultPrevented) { this._downloads++; this._got.push(this.href); }
    },
  };
}

const settle = () => new Promise((r) => setTimeout(r, 0));
// Deep enough to let a deferred probe resolve AND anything it starts settle --
// a guard that re-probed instead of passing through would otherwise look
// quiet because its second probe had not come back yet.
const drain = async () => { for (let i = 0; i < 4; i += 1) await settle(); };

async function scenario(response, opts) {
  opts = opts || {};
  let calls = 0;
  let bodyCancels = 0;
  global.fetch = function (url, init) {
    calls++;
    if (response === 'network') return Promise.reject(new TypeError('failed'));
    const r = Object.assign({ _url: url, _init: init }, response);
    // A real Response carries a ReadableStream the guard has to release. The
    // plain stubs have none, which is why the guard tests before cancelling.
    if (opts.withBody) r.body = { cancel() { bodyCancels++; } };
    // Resolve on a later TASK, not a microtask, so opts.midProbe runs while
    // the probe is genuinely in flight.
    if (opts.midProbe) return new Promise((k) => setTimeout(() => k(r), 0));
    return Promise.resolve(r);
  };
  // Models both pages' showDownloadError: a falsy message RETIRES the
  // surface. So `errors` is what the user was actually shown -- the
  // guard's clear does not read as a message -- and `visible` is what the
  // box still says once everything has settled.
  const errors = [];
  let visible = null;
  const show = (m) => { visible = m || null; if (m) errors.push(m); };
  const link = makeLink('/scout/download/j1');
  bind(link, show);
  if (opts.rebind) bind(link, show);
  link.click();
  // The results re-render under the click: both pages rebind, and a render
  // for another chain repoints the anchor as well. `true` rebinds only.
  if (opts.midProbe) {
    bind(link, show);
    if (opts.midProbe !== true) link.href = opts.midProbe;
  }
  await drain();
  // A second click once the cause of the refusal is gone.
  if (opts.retry) {
    global.fetch = function (url, init) {
      calls++;
      return Promise.resolve(
        Object.assign({ _url: url, _init: init }, opts.retry));
    };
    link.click();
    await drain();
  }
  return {
    errors,
    visible,
    downloads: link._downloads,
    downloaded: link._got,
    fetches: calls,
    bodyCancels,
  };
}

const json = (body) => ({ json: () => Promise.resolve(body) });
const notJson = () => ({ json: () => Promise.reject(new SyntaxError('not json')) });
// A Headers-alike. The JSON stubs above deliberately have none, which is what
// keeps them on the guard's json() branch -- an absent content-type reads as
// ''. Only the text cases below declare one.
const hdr = (v) => ({
  get: (k) => (String(k).toLowerCase() === 'content-type' ? v : null),
});
// The AF2 shape: text/plain, and a body that is a shell comment. .json() is
// present AND rejecting so a guard that ignored the content-type would fall
// to GENERIC rather than crash -- the assertion then names the missing
// message, not a TypeError.
const text = (ctype, body) => Object.assign(
  { headers: hdr(ctype), text: () => Promise.resolve(body) }, notJson());

(async function () {
  const out = {};

  // A clean response downloads, and probes exactly once.
  out.ok = await scenario(Object.assign({ ok: true, redirected: false }, json({})));

  // Every refusal these routes compose must reach the user verbatim.
  out.job_gone = await scenario(Object.assign(
    { ok: false, redirected: false },
    json({ error: 'Results not found. Please run analysis first.' })));
  out.chain_mismatch = await scenario(Object.assign(
    { ok: false, redirected: false },
    json({ error: 'These feasibility results are for chain B.' })));

  // login_required 302s to /login and fetch FOLLOWS it to a 200 HTML page.
  // Treating that as success saves the login page as a .csv.
  // withBody: the login page is a body this branch discards unread too.
  out.session_expired = await scenario(
    Object.assign({ ok: true, redirected: true }, notJson()), { withBody: true });

  // A refusal that is not our JSON (500 page, staff 404.html) still says something.
  out.non_json_refusal = await scenario(Object.assign(
    { ok: false, redirected: false }, notJson()));
  // ...and so does an empty/!error JSON body.
  out.json_without_error = await scenario(Object.assign(
    { ok: false, redirected: false }, json({})));

  // ---- text/plain refusals: blueprints/jobs.py af2_download_pdb / _pae ----
  // Their bodies carry no Content-Disposition, so an unguarded click navigates
  // to them. Quoted verbatim, minus the '# ' that suits a file on disk.
  out.text_refusal = await scenario(Object.assign(
    { ok: false, redirected: false },
    text('text/plain; charset=utf-8', "# No PDB in this job's result.\n")));
  out.text_refusal_pae = await scenario(Object.assign(
    { ok: false, redirected: false },
    text('text/plain', '# Malformed PAE payload.\n')));
  // Nothing but the marker. Showing it would reopen an empty box, which is
  // the silence the guard exists to end, so it must fall to GENERIC.
  out.text_refusal_blank = await scenario(Object.assign(
    { ok: false, redirected: false }, text('text/plain', '#\n')));
  // Same routes, HTML refusal: render_template("404.html") on a job that is
  // not the caller's or not an af2 job. Not ours to quote.
  out.html_refusal = await scenario(Object.assign(
    { ok: false, redirected: false, headers: hdr('text/html; charset=utf-8') },
    notJson()));
  // A text/plain body on the OK path is a download, not a message: the guard
  // must not read the content-type before it has decided ok-ness.
  out.text_ok = await scenario(Object.assign(
    { ok: true, redirected: false }, text('text/plain', '# not a refusal\n')));

  out.network = await scenario('network');

  // The cause of a refusal can go away. Sign in again in a second tab and
  // click: the CSV must arrive WITHOUT the dead session still on screen.
  out.refusal_then_success = await scenario(
    Object.assign({ ok: true, redirected: true }, notJson()),
    { retry: Object.assign({ ok: true, redirected: false }, json({})) });

  // Re-rendering results rebinds. onclick= must replace, not stack.
  out.rebound = await scenario(Object.assign({ ok: true, redirected: false }, json({})),
    { rebind: true });

  // The probe fetches the whole CSV and reads none of it.
  out.ok_with_body = await scenario(
    Object.assign({ ok: true, redirected: false }, json({})), { withBody: true });

  // Analysing another chain re-renders mid-probe and repoints the anchor. The
  // answer in flight is about the file the user CLICKED, so it must not start
  // a download of the one the button means now.
  out.rebound_mid_probe = await scenario(
    Object.assign({ ok: true, redirected: false }, json({})),
    { midProbe: '/scout/download/j2', withBody: true });

  // Same render, same href: the rebind alone retires the handler the probe is
  // waiting in, which is what a closure-held passthrough flag cannot survive.
  out.rebound_mid_probe_same_href = await scenario(
    Object.assign({ ok: true, redirected: false }, json({})), { midProbe: true });

  // credentials must ride along or the session cookie is not sent.
  let seen = null;
  global.fetch = function (url, init) {
    seen = init;
    return Promise.resolve(Object.assign({ ok: true, redirected: false }, json({})));
  };
  const l = makeLink('/scout/feasibility/download/j1?chain=A');
  bind(l, () => {});
  l.click();
  await settle();
  out.init = seen;

  process.stdout.write(JSON.stringify(out));
}());
