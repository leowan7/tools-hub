/**
 * Stub-DOM runner for static/js/hotspot_picker.js.
 *
 * WHY THIS EXISTS. tests/test_candidate_table_js_contract.py:4 states the
 * house position: "There is no JS test harness in this repo, so nothing
 * executes that file." Every JS assertion here has therefore been a Python
 * substring search over the source, and the record of those is bad — four of
 * thirteen hooks in that file were held up by CSS rules and template comments
 * rather than by the code they claimed to pin. A substring search also cannot
 * see the one thing that matters for the picker: whether a given FORM passes
 * `chainPrefixed`, because that lives in an object literal, not a token.
 *
 * So this runs the real picker, against the real opts object, out of the real
 * RENDERED form page. It reads a JSON scenario on stdin and writes a JSON
 * result on stdout. Driven from tests/test_hotspot_picker_runtime.py.
 *
 * No jsdom, no npm, no package.json. The picker only touches the DOM through
 * getElementById / addEventListener / dispatchEvent, and the NGL half is
 * unreachable without a file upload: `_loadFile` is never called, so
 * `_refreshHotspotRepr` returns at hotspot_picker.js:286 on a null component.
 * That leaves `_chains`, `_chainSel`, `_hotspotSel`, `_toggleResidue`,
 * `_setHotspots` and both token parsers fully exercised.
 *
 * Scenario:
 *   {
 *     "pickerJs":   "<absolute path to static/js/hotspot_picker.js>",
 *     "formScript": "<the inline <script> body from the rendered form>",
 *     "chain":      "A,B",            // value typed into #target_chain
 *     "hotspots":   "A296",           // pre-existing value in #hotspot_residues
 *     "clicks":     [{"resno": 264, "chain": "B"}]
 *   }
 *
 * Result:
 *   {
 *     "ok": true,
 *     "chainPrefixed": true,          // what the FORM actually asked for
 *     "field": "A296,B264",           // #hotspot_residues after the clicks
 *     "chains": ["A", "B"],
 *     "chainSel": "(:A or :B)",       // NGL selection for the target
 *     "hotspotSel": "...",            // NGL selection for the highlights
 *     "ignoredClicks": [],            // clicks the chain gate threw away
 *     "opts": { ... }                 // the literal the form passed
 *   }
 */
'use strict';

const fs = require('fs');

function makeEl(id) {
  return {
    id: id,
    value: '',
    checked: false,
    disabled: false,
    innerHTML: '',
    style: {},
    files: [],
    _listeners: {},
    addEventListener: function (type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    removeEventListener: function () {},
    dispatchEvent: function (evt) {
      const fns = this._listeners[(evt && evt.type) || ''] || [];
      for (const fn of fns) fn.call(this, evt);
      return true;
    },
    // Enough for the sibling logic that shares a DOMContentLoaded handler with
    // the picker — proteina's curated-vs-custom refresh() does
    // `custom.querySelectorAll('input').forEach(...)` at proteina_form.html:294
    // BEFORE it calls initHotspotPicker, so a missing method there would stop
    // the picker being constructed at all and the whole form would look
    // untested rather than broken.
    querySelectorAll: function () { return []; },
    querySelector: function () { return null; },
  };
}

function main() {
  const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));

  const els = Object.create(null);
  const windowListeners = Object.create(null);

  global.document = {
    getElementById: function (id) {
      if (!els[id]) els[id] = makeEl(id);
      return els[id];
    },
    createEvent: function () {
      return { initEvent: function (t) { this.type = t; } };
    },
    addEventListener: function () {},
    querySelectorAll: function () { return []; },
    querySelector: function () { return null; },
    body: makeEl('body'),
  };
  global.window = {
    addEventListener: function (type, fn) {
      (windowListeners[type] = windowListeners[type] || []).push(fn);
    },
    removeEventListener: function () {},
  };
  global.Event = function (type, opts) {
    this.type = type;
    this.bubbles = !!(opts && opts.bubbles);
  };
  // Deliberately NOT defined, so the picker takes its no-viewer path if any
  // scenario ever reaches _loadFile: NGL, ResizeObserver.

  // 1. The real picker.
  eval(fs.readFileSync(scenario.pickerJs, 'utf8'));

  // 2. Capture the instance the FORM builds, without replacing the real
  //    constructor — the opts object under test has to be the form's own.
  const realInit = global.window.initHotspotPicker;
  if (typeof realInit !== 'function') {
    throw new Error('hotspot_picker.js did not export window.initHotspotPicker');
  }
  let picker = null;
  let opts = null;
  global.window.initHotspotPicker = function (o) {
    opts = o;
    picker = realInit(o);
    return picker;
  };

  // 3. The form's own inline script, then the event it waits on.
  eval(scenario.formScript);
  for (const fn of windowListeners['DOMContentLoaded'] || []) fn();

  if (!picker) {
    throw new Error(
      'the form script never called initHotspotPicker (DOMContentLoaded fired)'
    );
  }

  // 4. Drive it.
  const chainEl = global.document.getElementById(opts.chainInputId);
  const hotspotEl = global.document.getElementById(opts.hotspotInputId);
  if (scenario.chain !== undefined) chainEl.value = scenario.chain;
  if (scenario.hotspots !== undefined) hotspotEl.value = scenario.hotspots;

  const ignoredClicks = [];
  for (const click of scenario.clicks || []) {
    // Mirrors the real NGL click handler at hotspot_picker.js:239-244 — the
    // chain gate and the toggle, in that order. Reproducing the gate is the
    // point: on a multi-chain target with the flag off it rejects EVERY pick,
    // which is the "picker is inert" bug and is invisible if you only call
    // _toggleResidue directly.
    const expected = picker._chains();
    if (expected.length && click.chain && expected.indexOf(click.chain) === -1) {
      ignoredClicks.push(click);
      continue;
    }
    picker._toggleResidue(click.resno, click.chain || null);
  }

  process.stdout.write(JSON.stringify({
    ok: true,
    chainPrefixed: !!picker.chainPrefixed,
    field: hotspotEl.value,
    chains: picker._chains(),
    chainSel: picker._chainSel(),
    hotspotSel: picker._hotspotSel(),
    ignoredClicks: ignoredClicks,
    opts: opts,
  }));
}

try {
  main();
} catch (err) {
  process.stdout.write(JSON.stringify({
    ok: false, error: String((err && err.stack) || err),
  }));
  process.exitCode = 1;
}
