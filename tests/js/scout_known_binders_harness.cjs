// Executes the REAL renderKnownBinders lifted from templates/scout/index.html.
//
// argv[2] is a file holding that function, sliced out of the shipped template
// by tests/test_scout_known_binder_table.py. Nothing here re-implements the
// logic: this file is a DOM stub and a scenario runner, so changing a rule in
// the template shows up as a behaviour change rather than a string mismatch.
//
// .cjs, not .js — the directory ABOVE this repo carries a package.json with
// "type": "module", which would make node treat a bare .js as ESM. Same reason
// tests/js/scout_refusal_harness.cjs uses it.
const fs = require('fs');

function makeEl(tag) {
  const el = {
    __tag: tag,
    _children: [],
    textContent: '',
    hidden: true,
    style: {},
    // Unlike the refusal harness's stub, innerHTML is STORED rather than
    // discarded. renderKnownBinders builds each row by assigning a full
    // <td> string to tr.innerHTML, so a write-only stub would make every
    // cell assertion below vacuously true.
    _html: '',
    appendChild(child) { this._children.push(child); return child; },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = v; if (v === '') this._children = []; },
  });
  return el;
}

const IDS = ['known-binders-section', 'known-binders-body', 'known-binders-count'];

function resetDom() {
  const els = {};
  IDS.forEach(function (id) { els[id] = makeEl('div'); });
  global.document = {
    // Auto-create on demand rather than policing a whitelist, matching the
    // sibling harness. The Python side separately asserts that every id the
    // block reads is actually defined in the page, which is what stops a
    // renamed element from staying green here while throwing in a browser.
    getElementById(id) {
      if (!(id in els)) els[id] = makeEl('div');
      return els[id];
    },
    createElement(tag) { return makeEl(tag); },
  };
  return els;
}

// The real template defines these; only the length matters to the block, which
// indexes by the epitope's index modulo the number of colours.
global.PATCH_COLORS = ['#c33', '#3c3', '#33c'];

const SRC = fs.readFileSync(process.argv[2], 'utf8');
// eslint-disable-next-line no-eval
eval(SRC);

// --- scenarios -------------------------------------------------------------

// THREE shapes, because the server gives three different answers and the
// table has to tell them apart:
//   'contacts' -> residue numbers were computed
//   'empty'    -> contact_residues is PRESENT and empty
//   'absent'   -> the key is missing
//
// What the server means by each is defined in scout/epitope_db.py's module
// docstring and deliberately not restated here.
//
// An earlier version of this harness emitted a present empty list for every
// row that had no contacts, and nothing else. That is why it certified the
// collapsed `b.contact_residues || []` label as correct: the shape that
// exposes the bug was never generated.
// `resolution` is null for NMR entries and for anything SAbDab reports
// unparseably (query_sabdab normalises those to None), and the template
// guards it before calling .toFixed. An earlier version of this harness
// hard-coded 2.0, so deleting that guard left this file green while the real
// page threw on the first NMR structure and rendered no table at all.
// `sparse` also empties the string fields that have fallbacks ('—' for
// species and affinity, 'Unknown' for binder_type).
function binder(i, shape, sparse) {
  const b = sparse ? {
    pdb_id: 'X' + String(i).padStart(3, '0'),
    binder_type: '',
    species: '',
    resolution: null,
    affinity: '',
  } : {
    pdb_id: 'X' + String(i).padStart(3, '0'),
    binder_type: 'Fab',
    species: 'homo sapiens',
    resolution: 2.0,
    affinity: '',
  };
  if (shape === 'contacts') { b.contact_residues = [10, 11, 12]; }
  else if (shape === 'empty') { b.contact_residues = []; }
  // 'absent' deliberately leaves the key off entirely.
  return b;
}

function run(nBinders, nWithContacts, tailShape, sparse) {
  const els = resetDom();
  const binders = [];
  for (let i = 0; i < nBinders; i++) {
    binders.push(
      binder(i, i < nWithContacts ? 'contacts' : (tailShape || 'absent'), sparse)
    );
  }
  // One epitope overlapping the contact residues, so the badge branch runs.
  const epitopes = [{ residue_numbers: [10, 11, 12] }];
  renderKnownBinders(binders, epitopes);

  const body = els['known-binders-body'];
  return {
    requested: nBinders,
    rendered: body._children.length,
    countText: els['known-binders-count'].textContent,
    sectionShown: els['known-binders-section'].hidden === false,
    firstRowHtml: body._children.length ? body._children[0].innerHTML : '',
    lastRowHtml: body._children.length
      ? body._children[body._children.length - 1].innerHTML : '',
    // Which pdb_ids survived, so a truncation that kept the WRONG end is
    // visible rather than merely counted.
    renderedIds: body._children.map(function (tr) {
      const m = /structure\/([A-Z0-9]+)"/.exec(tr.innerHTML);
      return m ? m[1] : null;
    }),
  };
}

process.stdout.write(JSON.stringify({
  huge: run(1340, 5, 'absent'),
  small: run(12, 5, 'absent'),
  exactly_cap: run(100, 5, 'absent'),
  one_over_cap: run(101, 5, 'absent'),
  single: run(1, 1, 'absent'),
  computed_empty: run(3, 0, 'empty'),
  never_computed: run(3, 0, 'absent'),
  sparse: run(2, 1, 'absent', true),
}));
