// Executes the REAL renumberRows lifted from static/js/candidate_table.js.
//
// argv[2] is a file holding that function, sliced out of the shipped script by
// tests/test_candidate_table_renumber.py. Nothing here re-implements the
// logic: this file is a DOM stub and a scenario runner, so changing the rule
// in the script shows up as a behaviour change rather than a string mismatch.
//
// .cjs, not .js — the directory ABOVE this repo carries a package.json with
// "type": "module", which would make node treat a bare .js as ESM. Same reason
// tests/js/scout_refusal_harness.cjs uses it.
const fs = require('fs');

function makeCell(text) {
  return {
    _text: text,
    get textContent() { return this._text; },
    set textContent(v) { this._text = v; },
  };
}

// ``rankText: null`` means the row carries no .cand-rank-n at all, which is
// what a viewer row looks like and what any future row without the span would
// look like. renumberRows must not throw on one.
function makeRow(cls, rankText) {
  const rankCell = rankText === null ? null : makeCell(rankText);
  const classes = cls.split(' ');
  return {
    _cls: cls,
    _rank: rankCell,
    classList: { contains: (c) => classes.indexOf(c) !== -1 },
    querySelector: (sel) => (sel === '.cand-rank-n' ? rankCell : null),
  };
}

function makeTbody(rows, grouped) {
  return {
    children: rows,
    querySelector: (sel) =>
      (sel === '.cand-group-row' && grouped ? {} : null),
  };
}

const src = fs.readFileSync(process.argv[2], 'utf8');
const renumberRows = new Function(src + '\nreturn renumberRows;')();

const numbers = (rows) =>
  rows.filter((r) => r._rank).map((r) => r._rank.textContent);

const out = {};

// 1. The defect this exists for: rows re-appended in sorted order carry their
//    PRE-SORT numbers. After renumbering, the column reads 1..n downward.
{
  const rows = [
    makeRow('cand-row', '10'),
    makeRow('cand-row', '9'),
    makeRow('cand-row', '8'),
  ];
  renumberRows(makeTbody(rows, false));
  out.descending_is_renumbered = numbers(rows);
}

// 2. Viewer rows are interleaved with data rows and must neither be numbered
//    nor consume a number — otherwise every open 3D panel shifts the count.
{
  const rows = [
    makeRow('cand-row', '5'),
    makeRow('viewer-row', null),
    makeRow('cand-row', '6'),
    makeRow('viewer-row', null),
    makeRow('cand-row', '7'),
  ];
  renumberRows(makeTbody(rows, false));
  out.viewer_rows_skipped = numbers(rows);
}

// 3. A grouped table restarts its counter per tool block (grp.n), so a flat
//    1..n would assert a single cross-tool ordering the table refuses to
//    make. Left completely alone.
{
  const rows = [
    makeRow('cand-row', '1'),
    makeRow('cand-row', '2'),
    makeRow('cand-row', '1'),
  ];
  renumberRows(makeTbody(rows, true));
  out.grouped_untouched = numbers(rows);
}

// 4. A data row with no .cand-rank-n must not throw, and must still consume
//    its position — the rows after it keep counting.
{
  const rows = [
    makeRow('cand-row', '1'),
    makeRow('cand-row', null),
    makeRow('cand-row', '3'),
  ];
  renumberRows(makeTbody(rows, false));
  out.missing_span_still_counts = numbers(rows);
}

process.stdout.write(JSON.stringify(out));
