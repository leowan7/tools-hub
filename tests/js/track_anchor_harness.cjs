// Runs the REAL static/js/track.js against a stubbed DOM and clicks anchors.
//
// argv[2] is that shipped file. Nothing here re-implements the href rules:
// the scenarios drive the delegated listener the script itself registers and
// report the event types it actually posted, so deleting a branch or loosening
// DOWNLOAD_RE shows up as a behaviour change rather than a string mismatch.
//
// .cjs, not .js -- same reason as tests/js/scout_download_harness.cjs: the
// directory ABOVE this repo carries a package.json with "type": "module".
const fs = require('fs');

const posted = [];

const body = { tagName: 'BODY', parentNode: null };
let clickListener = null;

global.window = {};
global.location = { pathname: '/jobs/abc', search: '' };
global.localStorage = {
  _v: {},
  getItem(k) { return this._v[k] || null; },
  setItem(k, v) { this._v[k] = v; },
};
// node >= 21 exposes `navigator` as a read-only accessor, so a plain
// assignment is silently dropped and the script takes its fetch fallback.
// defineProperty makes the beacon path the one under test; the fetch stub
// below records too, so the script is measured whichever branch it takes.
Object.defineProperty(global, 'navigator', {
  value: { sendBeacon(url, blob) { posted.push(JSON.parse(blob._text)); return true; } },
  configurable: true,
});
global.Blob = function (parts) { this._text = parts.join(''); };
global.fetch = function (url, opts) {
  posted.push(JSON.parse(opts.body));
  return { catch() {} };
};
global.document = {
  readyState: 'complete',
  body: body,
  addEventListener(type, fn) { if (type === 'click') clickListener = fn; },
};

// eslint-disable-next-line no-eval
eval(fs.readFileSync(process.argv[2], 'utf8'));

if (!clickListener) {
  console.log(JSON.stringify({ error: 'no click listener registered' }));
  process.exit(0);
}

// A click on a <span> inside the anchor, which is what the export buttons
// actually contain, so the walk up to the anchor is exercised too.
function click(href) {
  posted.length = 0;
  const anchor = href === null
    ? { tagName: 'DIV', parentNode: body, getAttribute() { return null; } }
    : { tagName: 'A', parentNode: body, getAttribute(n) { return n === 'href' ? href : null; } };
  const span = { tagName: 'SPAN', parentNode: anchor };
  clickListener({ target: span });
  return posted.map(function (p) { return p.event_type; });
}

const cases = {
  csv: '/jobs/abc/export.csv',
  fasta: '/jobs/abc/export.fasta',
  zip: '/jobs/abc/export.zip',
  pdb: '/jobs/abc/af2.pdb',
  npz: '/jobs/abc/af2_pae.npz',
  api_pdb: '/api/jobs/abc/pdb/design_1.pdb',
  inline_pdb: 'data:chemical/x-pdb;base64,QUJD',
  outbound: 'https://ranomics.com/ai-binder-sprint',
  outbound_www: 'https://www.ranomics.com/contact',
  internal: '/tools/af2',
  scale_up_form: '#',
  not_an_anchor: null,
};

const out = {};
for (const name of Object.keys(cases)) out[name] = click(cases[name]);
console.log(JSON.stringify(out));
