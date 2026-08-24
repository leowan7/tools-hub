// Executes the REAL refusal block lifted from templates/scout/index.html.
//
// argv[2] is a file holding that block, sliced out of the shipped template by
// tests/test_scout_refusal_cta.py. Nothing here re-implements the logic: this
// file is a DOM stub and a scenario runner, so deleting a rule in the template
// shows up as a behaviour change rather than a string mismatch.
//
// .cjs, not .js — the directory ABOVE this repo carries a package.json with
// "type": "module", which would make node treat a bare .js as ESM. Same reason
// tests/js/hotspot_picker_harness.cjs uses it.
const fs = require('fs');

function makeEl(tag) {
  const el = {
    __tag: tag,
    _children: [],
    _options: [],
    textContent: '',
    value: '',
    hidden: true,
    href: '',
    style: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(child) {
      this._children.push(child);
      if (child.__tag === 'option') this._options.push(child);
      return child;
    },
  };
  Object.defineProperty(el, 'options', { get() { return this._options; } });
  Object.defineProperty(el, 'innerHTML', {
    get() { return ''; },
    set(v) { if (v === '') { this._children = []; this._options = []; } },
  });
  return el;
}

// Exactly the ids the extracted block reads. It listed nine until QC round 9
// measured it: seven were scaffolding for a deleted helper, and analyze-btn,
// which the block DOES read, was missing and only worked because the stub
// auto-creates. Vestigial in both directions.
const IDS = ['error-message', 'analyze-error', 'analyze-btn'];

function resetDom() {
  const els = {};
  IDS.forEach(function (id) { els[id] = makeEl('div'); });
  global.document = {
    // Auto-create on demand rather than policing a whitelist. The block under
    // test is REAL shipped code and upstream keeps adding to the functions it
    // contains — #174 added an #analyze-btn reset inside showAnalyzeError, and
    // a whitelist turned that into 21 ERRORs in this file that looked like the
    // feature was broken when it was not. The assertions below name the
    // elements they care about; anything else just needs to exist.
    getElementById(id) {
      if (!(id in els)) els[id] = makeEl('div');
      return els[id];
    },
    createElement(tag) { return makeEl(tag); },
    createTextNode(t) { return { __tag: '#text', textContent: t }; },
  };
  return els;
}

global.location = { pathname: '/scout/', search: '' };
// node 21+ ships its own `navigator` as a GETTER on globalThis, so a plain
// assignment is silently ignored and every cookie-off case would read as
// cookie-on — a harness that always passes. defineProperty is what actually
// replaces it. (Found by this file's own tests going red, not by inspection.)
function setCookies(enabled) {
  Object.defineProperty(globalThis, 'navigator', {
    value: { cookieEnabled: enabled }, configurable: true, writable: true,
  });
}
function removeNavigator() {
  Object.defineProperty(globalThis, 'navigator', {
    value: undefined, configurable: true, writable: true,
  });
}
setCookies(true);


const SRC = fs.readFileSync(process.argv[2], 'utf8');
let els = resetDom();
// eslint-disable-next-line no-eval
eval(SRC);

// A link is the thing with an href; the CTA is appended after the message.
function ctaOf(el) {
  const links = el._children.filter(function (c) { return c.__tag === 'a'; });
  return links.length ? { href: links[0].href, text: links[0].textContent } : null;
}

const out = {};

// --- CTA, per reason x cookie state ----------------------------------------
// Both axes matter and an earlier version tested only one. The reason alone
// cannot decide this: /upload, /fetch-pdb and /example have no session tier, so
// a cookies-blocked visitor is refused there as rate_limited, not no_session.
[true, false].forEach(function (cookiesOn) {
  const suffix = cookiesOn ? '_cookies_on' : '_cookies_off';
  ['rate_limited', 'session_rate_limited', 'no_session', undefined, 'busy', 'at_capacity']
    .forEach(function (reason) {
      setCookies(cookiesOn);
      els = resetDom();
      showError('refused', reason);
      out[String(reason) + suffix] = ctaOf(els['error-message']);
    });
});
// A browser with no navigator at all must not throw and must not lose the CTA.
removeNavigator();
els = resetDom();
showError('refused', 'rate_limited');
out.no_navigator = ctaOf(els['error-message']);
setCookies(true);

els = resetDom();
showAnalyzeError('refused', 'session_rate_limited');
out.analyze_error_element = ctaOf(els['analyze-error']);

// The message must survive verbatim next to the link.
els = resetDom();
showError('Too many requests from this network.', 'rate_limited');
out.message_preserved = els['error-message'].textContent;

// --- next= carries the current location ------------------------------------
els = resetDom();
global.location = { pathname: '/scout/', search: '?ref=email' };
showError('refused', 'rate_limited');
out.next_with_search = ctaOf(els['error-message']);
global.location = { pathname: '/scout/', search: '' };

process.stdout.write(JSON.stringify(out));
