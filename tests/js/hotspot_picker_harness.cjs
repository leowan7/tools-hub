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
 * TWO FIDELITY RULES, both of them scars.
 *
 * 1. THE HARNESS NEVER DECIDES ANYTHING THE PICKER DECIDES. An earlier version
 *    of this file carried its own copy of the NGL click chain-gate — it read
 *    `picker._chains()`, compared the clicked chain itself, and only then
 *    called `_toggleResidue`. That is a reimplementation, not a test: deleting
 *    the entire gate from hotspot_picker.js left 65 tests green, because the
 *    harness was supplying the behaviour the tests were reading back. So the
 *    real handler is now registered by the real code (inside `_loadFile`'s
 *    resolve callback) and clicks are delivered to it as NGL delivers them, as
 *    a pickingProxy. The harness OBSERVES the outcome — did the hotspot field
 *    move? — and never predicts it. Nothing below may know what the gate's
 *    rules are.
 *
 * 2. `document.getElementById` ANSWERS ONLY FOR IDS THE PAGE REALLY HAS. It
 *    used to auto-create any id it was asked for, so a form whose `viewerId`
 *    pointed at an element that does not exist still looked healthy — a
 *    structurally dead picker passed. The ids are now harvested from the
 *    rendered page HTML the scenario carries, and anything else answers null,
 *    exactly as a browser would.
 *
 * Because rule 1 needs a viewer, this file also stubs NGL: a Stage that records
 * the handlers the picker registers on `signals.clicked`, and a component that
 * records representations. `loadFile` resolves SYNCHRONOUSLY (a thenable, not a
 * Promise) so the scenario stays a straight line and so a throw inside the
 * picker's resolve callback surfaces as a harness error instead of being eaten
 * by the picker's own `.catch`. It REJECTS synchronously instead when the
 * scenario sets `loadFileRejects`, which is the only way anything here reaches
 * the picker's parse-failure branch. ResizeObserver is left undefined on
 * purpose, so the picker takes its degrade-silently path there.
 *
 * No jsdom, no npm, no package.json.
 *
 * Scenario:
 *   {
 *     "pickerJs":   "<absolute path to static/js/hotspot_picker.js>",
 *     "pageHtml":   "<the whole rendered form page>",   // supplies the real ids
 *     "formScript": "<the inline <script> body from that page>",
 *     "chain":      "A,B",            // value typed into #target_chain
 *     "hotspots":   "A296",           // pre-existing value in #hotspot_residues
 *     "clicks":     [{"resno": 264, "chain": "B"}],
 *     "loadFileRejects": false        // make NGL.loadFile REJECT, so the
 *                                     // picker takes its degrade path
 *   }
 *
 * Result:
 *   {
 *     "ok": true,
 *     "chainPrefixed": true,          // what the FORM actually asked for
 *     "field": "A296,B264",           // #hotspot_residues after the clicks
 *     "viewerHtml": "",               // what the picker wrote into the viewer
 *     "chains": ["A", "B"],
 *     "chainSel": "(:A or :B)",       // NGL selection for the target
 *     "hotspotSel": "...",            // NGL selection for the highlights
 *     "ignoredClicks": [],            // clicks that moved nothing (observed)
 *     "clickHandlers": 1,             // handlers the picker gave the viewer
 *     "stagesBuilt": 1,               // NGL stages the picker constructed
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

/** Every `id="..."` the rendered page carries. */
function idsIn(html) {
  const ids = Object.create(null);
  const re = /\sid\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const id = m[1] !== undefined ? m[1] : m[2];
    if (id) ids[id] = true;
  }
  return ids;
}

/**
 * A resolved thenable, not a Promise.
 *
 * The picker does `loadFile(...).then(cb).catch(cb2)`. Resolving synchronously
 * keeps the whole scenario on one stack, so the result JSON is written after
 * the click handler exists rather than racing it — and an exception thrown
 * inside `cb` propagates to this file's own try/catch and is reported, instead
 * of being swallowed by the picker's `.catch` and turning into an innerHTML
 * error message nothing asserts on.
 */
function resolvedWith(value) {
  return {
    then: function (onFulfilled) {
      onFulfilled(value);
      return { catch: function () { return this; } };
    },
  };
}

/**
 * A rejected thenable, for `scenario.loadFileRejects`.
 *
 * NGL rejects `loadFile` on a structure it cannot parse, and the picker answers
 * that with a `.catch` that writes "Could not parse this structure. Typed
 * hotspot entry still works." into the viewer (hotspot_picker.js). Until this
 * existed nothing in the repo reached that branch — every scenario resolved —
 * so the degrade path could be deleted or broken with the whole suite green,
 * and the promise the copy makes (typed entry still works) was never checked.
 *
 * Symmetric with resolvedWith and synchronous for the same reason: `then` does
 * NOT call its callback, `catch` does, and both stay on one stack so the result
 * JSON is written after the picker has finished reacting.
 */
function rejectedWith(err) {
  return {
    then: function () {
      return {
        catch: function (onRejected) {
          onRejected(err);
          return this;
        },
      };
    },
  };
}

function makeComponent() {
  return {
    representations: [],
    addRepresentation: function (type, params) {
      const repr = { type: type, params: params };
      this.representations.push(repr);
      return repr;
    },
    removeRepresentation: function (repr) {
      const i = this.representations.indexOf(repr);
      if (i >= 0) this.representations.splice(i, 1);
    },
    autoView: function () {},
  };
}

function main() {
  const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));

  if (typeof scenario.pageHtml !== 'string' || !scenario.pageHtml) {
    throw new Error(
      'scenario.pageHtml is required — getElementById answers only for ids ' +
      'the rendered page really carries'
    );
  }
  const realIds = idsIn(scenario.pageHtml);

  const els = Object.create(null);
  const windowListeners = Object.create(null);

  global.document = {
    // A browser answers null for an id that is not in the document, and so
    // does this. Auto-creating instead made a picker mounted on a nonexistent
    // element indistinguishable from a working one.
    getElementById: function (id) {
      if (!realIds[id]) return null;
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

  // The viewer. Its only job is to hand the picker somewhere to register its
  // click handler and to record what got registered; every decision the
  // handler then makes is the picker's own.
  const clickHandlers = [];
  let stagesBuilt = 0;
  global.NGL = {
    Stage: function (container, params) {
      stagesBuilt += 1;
      this.container = container;
      this.params = params;
      this.signals = {
        clicked: { add: function (fn) { clickHandlers.push(fn); } },
      };
      this.handleResize = function () {};
      this.removeComponent = function () {};
      this.loadFile = function () {
        return scenario.loadFileRejects
          ? rejectedWith(new Error('NGL: could not parse structure'))
          : resolvedWith(makeComponent());
      };
    },
  };
  // Deliberately NOT defined, so the picker takes its degrade-silently path:
  // ResizeObserver.

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

  // 4. Type into the fields the form named.
  const hotspotEl = global.document.getElementById(opts.hotspotInputId);
  if (!hotspotEl) {
    throw new Error(
      'hotspotInputId is #' + opts.hotspotInputId + ', which the rendered page '
      + 'does not contain'
    );
  }
  const chainEl = opts.chainInputId
    ? global.document.getElementById(opts.chainInputId) : null;
  if (opts.chainInputId && !chainEl) {
    throw new Error(
      'chainInputId is #' + opts.chainInputId + ', which the rendered page does '
      + 'not contain'
    );
  }
  if (chainEl && scenario.chain !== undefined) chainEl.value = scenario.chain;
  if (scenario.hotspots !== undefined) hotspotEl.value = scenario.hotspots;

  // 5. Upload a structure. This is what makes the REAL click handler exist:
  //    the picker registers it inside _loadFile's resolve callback. If the
  //    picker never mounted — a viewerId that names nothing, say — nothing
  //    below this line registers anything and `clickHandlers` stays empty,
  //    which is the observation that tells a live picker from a dead one.
  const pdbEl = global.document.getElementById(opts.pdbInputId);
  if (!pdbEl) {
    throw new Error(
      'pdbInputId is #' + opts.pdbInputId + ', which the rendered page does not '
      + 'contain'
    );
  }
  pdbEl.files = [{ name: 'target.pdb' }];
  const changeEvt = new global.Event('change');
  changeEvt.target = pdbEl;
  pdbEl.dispatchEvent(changeEvt);

  // 6. Click, through the picker's own handler.
  //
  //    A click is "ignored" here purely because the hotspot field did not
  //    move. That is an OBSERVATION, not the gate's rule restated: whatever
  //    the picker accepts reaches _setHotspots, and _setHotspots always adds
  //    or removes a token, so the field always changes. The harness is not
  //    allowed to know why a click was refused, only that it was.
  const ignoredClicks = [];
  for (const click of scenario.clicks || []) {
    const before = hotspotEl.value;
    const pickingProxy = {
      atom: { resno: click.resno, chainname: click.chain },
    };
    for (const fn of clickHandlers) fn(pickingProxy);
    if (hotspotEl.value === before) ignoredClicks.push(click);
  }

  const viewerEl = global.document.getElementById(opts.viewerId);

  process.stdout.write(JSON.stringify({
    ok: true,
    chainPrefixed: !!picker.chainPrefixed,
    field: hotspotEl.value,
    viewerHtml: viewerEl ? viewerEl.innerHTML : '',
    chains: picker._chains(),
    chainSel: picker._chainSel(),
    hotspotSel: picker._hotspotSel(),
    ignoredClicks: ignoredClicks,
    clickHandlers: clickHandlers.length,
    stagesBuilt: stagesBuilt,
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
