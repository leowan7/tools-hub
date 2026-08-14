/**
 * Candidate table behaviour: column sort, star/shortlist, sessionStorage
 * persistence, 3D viewer expand, and lab-submit modal population.
 *
 * Works for a single job's table AND for a merged campaign table whose
 * candidates come from many sub-jobs. Shortlist entries are {j: jobId, i: idx}
 * refs so a campaign shortlist can span sub-jobs; a single-job table just has
 * one jobId across every row. The wrapper carries:
 *   data-scope        - the sessionStorage key + element-id suffix (campaign id
 *                       in campaign mode, else the job id)
 *   data-campaign-id  - emitted by the macro in campaign mode. NOT read here,
 *                       by this file or any other: it does not drive the modal
 *                       payload, as this comment used to claim. openCampaignModal
 *                       fills whichever of candidate_refs / candidate_indices
 *                       the modal it found actually carries, and that is the
 *                       only thing that selects the shape.
 * Each star button carries data-job (the candidate's SOURCE job) and
 * data-ref-idx (its index WITHIN that job); data-idx stays the row index used
 * for the 3D viewer rows.
 *
 * Exposes:
 *   window.getShortlist(scope)     → [{j,i}]  DEAD. No caller anywhere in
 *                                   templates/ or static/. Kept as the read
 *                                   side of the sessionStorage format for
 *                                   console use; delete it and nothing breaks.
 *   window.openCampaignModal(scope)
 *   window.closeCampaignModal(scope)
 *     Both called ONLY from inline onclick in components/candidate_table.html
 *     -- the shortlist button, and the modal's ×, Cancel and overlay. Renaming
 *     either is a silent break in this repo: nothing here calls them, and
 *     tests/test_candidate_table_js_contract.py is the only thing that looks.
 *   window.dropShortlistRefs(scope, refs)
 *     Called from templates/campaigns/detail.html, which loads this file for
 *     that call alone. `refs` is the [{job_id, index}] list a submitted request
 *     COVERED, and this removes exactly those from the scope's shortlist and
 *     keeps every other star. It is the write side of the same storageKey() and
 *     refKey() the star toggle uses, which is why it lives here rather than as
 *     an inline one-liner on that page: a second spelling of either can drift
 *     and then silently matches nothing. See the definition for why it removes
 *     named refs instead of dropping the key.
 *     tests/test_lab_project_confirmation.py pins both ends.
 */
(function () {
  'use strict';

  // ─── sessionStorage helpers ──────────────────────────────────────────────

  function storageKey(scope) { return 'shortlist_' + scope; }

  function loadShortlist(scope) {
    try {
      var raw = sessionStorage.getItem(storageKey(scope));
      var arr = raw ? JSON.parse(raw) : [];
      // Coerce any legacy bare-int entries to {j,i} refs (j unknown → null).
      return arr.map(function (e) {
        if (e && typeof e === 'object') return { j: e.j != null ? String(e.j) : null, i: e.i };
        return { j: null, i: e };
      });
    } catch (_) { return []; }
  }

  function saveShortlist(scope, refs) {
    try { sessionStorage.setItem(storageKey(scope), JSON.stringify(refs)); }
    catch (_) {}
  }

  function refKey(j, i) { return String(j) + '#' + String(i); }

  // Remove the designs a request already covered from this scope's shortlist,
  // and nothing else. `refs` is the {job_id, index} list the confirmation page
  // of a submitted lab project was rendered from -- the same designs it prints
  // -- so what survives here is exactly the stars that request did NOT use,
  // which is what its truncation advice tells the customer to send in a second
  // one. Without this, `openCampaignModal` serialises the same stored list in
  // the same order on the next submit, so the second POST carries the same refs
  // and `_parse_candidate_refs_counted` cuts it in the same place.
  //
  // A REMOVAL BY REF, NOT A WIPE OF THE KEY, and that is the whole design.
  // `?submitted=1` is a permanent property of a URL rather than an event: a
  // reload, a bookmark, the omnibox, a history entry, a restored tab and a
  // brand-new tab session all reach it, and both the page's own copy and the
  // confirmation email invite the customer back to it. Removing NAMED refs is
  // idempotent, so every one of those is a no-op once the refs are gone -- no
  // marker, no token, and nothing that has to outlive the URL. An earlier
  // version wiped the whole key behind a sessionStorage marker, which destroyed
  // the never-read remainder the advice is ABOUT and kept its guard in a store
  // that dies with the tab while the URL survives in history.
  //
  // THE ONE CASE THAT IS NOT A NO-OP, and it is accepted deliberately: a
  // customer who re-stars a design this request already covered and then
  // returns to the confirmation URL loses that star again. Un-starring a design
  // already sent to the lab is the defensible reading of that, so it is allowed
  // rather than guarded (register item A102).
  //
  // ONE SPELLING OF THE IDENTITY. refKey is the star toggle's own comparison,
  // so a design is removed here exactly when clicking its star would have
  // matched it. refKey only concatenates, so a stored entry carrying no job id
  // keys as "null#N" or "#N" and WOULD match a server ref spelled the same
  // way. What makes that unreachable is on the server: `_covered_refs` drops
  // any ref it cannot name a job for, so no such ref is ever sent. Do not read
  // the empty case as harmless here; read it as never arriving.
  //
  // NOTHING REMOVED MEANS NOTHING WRITTEN, so a repeat visit does not rewrite
  // the customer's stored list at all. A falsy scope writes nothing rather than
  // writing `shortlist_undefined`: the scope is derived from a database row, and
  // a row with no parent id must not resolve to a key some other page owns.
  // saveShortlist swallows a throwing sessionStorage the way its neighbours do.
  window.dropShortlistRefs = function (scope, refs) {
    if (!scope || !refs || !refs.length) return;
    var drop = {};
    for (var n = 0; n < refs.length; n++) {
      drop[refKey(refs[n].job_id, refs[n].index)] = true;
    }
    var before = loadShortlist(scope);
    var kept = before.filter(function (r) {
      return drop[refKey(r.j, r.i)] !== true;
    });
    if (kept.length !== before.length) saveShortlist(scope, kept);
  };

  function starRef(btn) {
    // The index recorded is the candidate's index within its OWN job
    // (data-ref-idx); data-job is that source job. In single-job mode both
    // collapse to the table's job + row index.
    var i = btn.dataset.refIdx !== undefined ? btn.dataset.refIdx : btn.dataset.idx;
    return { j: btn.dataset.job, i: parseInt(i, 10) };
  }

  // ─── UI helpers ──────────────────────────────────────────────────────────

  function updateShortlistUI(scope) {
    var sl      = loadShortlist(scope);
    var countEl = document.getElementById('shortlist-count-' + scope);
    var hintEl  = document.getElementById('shortlist-hint-' + scope);
    var empty   = sl.length === 0;
    if (countEl) countEl.textContent = sl.length;
    // The zero-star state is an inline hint, not a disabled button. A
    // `disabled` control carrying only a `title` reads as broken software:
    // there is nothing to hover on a touch device and nothing to click on any
    // device. The button stays live and the hint says what to do.
    if (hintEl) hintEl.style.display = empty ? '' : 'none';
  }

  function restoreStarState(table, scope) {
    var sl   = loadShortlist(scope);
    var keys = {};
    sl.forEach(function (r) { keys[refKey(r.j, r.i)] = true; });
    table.querySelectorAll('.star-btn').forEach(function (btn) {
      var r  = starRef(btn);
      var on = keys[refKey(r.j, r.i)] === true;
      btn.classList.toggle('starred', on);
      btn.textContent = on ? '★' : '☆';
    });
  }

  // ─── Sort ────────────────────────────────────────────────────────────────

  function sortTable(table, col, dir) {
    var tbody    = table.querySelector('tbody');
    var children = Array.from(tbody.children);

    // Pair data rows with their optional viewer rows.
    var pairs = [];
    for (var i = 0; i < children.length; i++) {
      var row = children[i];
      if (!row.classList.contains('cand-row')) continue;
      var next   = children[i + 1];
      var viewer = (next && next.classList.contains('viewer-row')) ? next : null;
      pairs.push({ dr: row, vr: viewer });
    }

    pairs.sort(function (a, b) {
      var aCell = a.dr.querySelector('[data-col="' + col + '"]');
      var bCell = b.dr.querySelector('[data-col="' + col + '"]');
      var aRaw  = aCell ? aCell.dataset.val : '';
      var bRaw  = bCell ? bCell.dataset.val : '';
      var aNum  = parseFloat(aRaw);
      var bNum  = parseFloat(bRaw);
      var cmp;
      if (!isNaN(aNum) && !isNaN(bNum)) {
        cmp = aNum - bNum;
      } else {
        cmp = String(aRaw).localeCompare(String(bRaw));
      }
      return dir === 'asc' ? cmp : -cmp;
    });

    pairs.forEach(function (p) {
      tbody.appendChild(p.dr);
      if (p.vr) tbody.appendChild(p.vr);
    });
  }

  // ─── Table initialisation ────────────────────────────────────────────────

  function initTable(wrapEl) {
    var tableId = wrapEl.dataset.candTableId;
    var scope   = wrapEl.dataset.scope || wrapEl.dataset.jobId;
    var table   = document.getElementById(tableId);
    if (!table) return;

    restoreStarState(table, scope);
    updateShortlistUI(scope);

    // Star toggle
    table.addEventListener('click', function (e) {
      var btn = e.target.closest('.star-btn');
      if (!btn) return;
      var r   = starRef(btn);
      var k   = refKey(r.j, r.i);
      var sl  = loadShortlist(scope);
      var pos = -1;
      for (var n = 0; n < sl.length; n++) {
        if (refKey(sl[n].j, sl[n].i) === k) { pos = n; break; }
      }
      if (pos === -1) {
        sl.push(r);
        btn.classList.add('starred');
        btn.textContent = '★';
      } else {
        sl.splice(pos, 1);
        btn.classList.remove('starred');
        btn.textContent = '☆';
      }
      saveShortlist(scope, sl);
      updateShortlistUI(scope);
    });

    // 3D viewer expand
    table.addEventListener('click', function (e) {
      var btn = e.target.closest('.view3d-btn');
      if (!btn) return;
      var idx       = btn.dataset.idx;
      var viewerRow = document.getElementById('viewer-row-' + idx);
      if (!viewerRow) return;
      var opening = viewerRow.style.display === 'none';
      viewerRow.style.display = opening ? '' : 'none';
      btn.textContent = opening ? 'Hide 3D' : 'View 3D';
      if (opening) {
        var viewerId = 'mol-viewer-' + idx;
        // Defer one frame so the just-unhidden row has been laid out
        // before Mol* measures its container. Constructing against a
        // 0x0 (still-collapsing) container yields a blank canvas.
        requestAnimationFrame(function () {
          if (btn.dataset.pdb64 && window.initMolViewer) {
            window.initMolViewer(viewerId, btn.dataset.pdb64);
          } else if (btn.dataset.pdbUrl && window.initMolViewerFromUrl) {
            window.initMolViewerFromUrl(viewerId, btn.dataset.pdbUrl);
          }
        });
      }
    });

    // "Starred only (CSV)". The selection lives in sessionStorage, so the
    // hidden `refs` field is filled at submit time rather than at render time;
    // a value stamped into the HTML would be whatever was starred on the
    // PREVIOUS page load. Same {job_id, index} shape the lab-submit modal
    // posts, so the server has one ref format to parse.
    document.querySelectorAll('.cand-starred-export').forEach(function (form) {
      if (form.dataset.scope !== scope) return;
      form.addEventListener('submit', function () {
        var input = form.querySelector('[name="refs"]');
        if (!input) return;
        input.value = JSON.stringify(loadShortlist(scope).map(function (r) {
          return { job_id: r.j, index: r.i };
        }));
      });
    });

    // Column sort
    table.querySelectorAll('th[data-col]').forEach(function (th) {
      th.style.cursor = 'pointer';
      th.dataset.dir  = 'desc';
      th.addEventListener('click', function () {
        var col = th.dataset.col;
        var dir = th.dataset.dir === 'desc' ? 'asc' : 'desc';
        th.dataset.dir = dir;
        sortTable(table, col, dir);
        table.querySelectorAll('th[data-col]').forEach(function (h) {
          h.classList.remove('sort-asc', 'sort-desc');
        });
        th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
      });
    });
  }

  // ─── Modal ───────────────────────────────────────────────────────────────

  window.getShortlist = function (scope) { return loadShortlist(scope); };

  window.openCampaignModal = function (scope) {
    var sl    = loadShortlist(scope);
    var modal = document.getElementById('campaign-modal-' + scope);
    if (!modal) return;

    // Campaign and target mode carry candidate_refs. Single-job mode carries
    // BOTH since A91: refs, which the server prefers, and candidate_indices,
    // kept for one release so a page served before that deploy still submits
    // against the new server. Populate whichever the modal carries.
    var refsInput = modal.querySelector('[name="candidate_refs"]');
    var idxInput  = modal.querySelector('[name="candidate_indices"]');
    // The scope's own parent field, which is the thing that actually differs:
    // the macro emits exactly one of the three, and only job scope emits this.
    var singleJob = !!modal.querySelector('[name="source_job_id"]');
    if (refsInput) {
      refsInput.value = JSON.stringify(sl.map(function (r) {
        return { job_id: r.j, index: r.i };
      }));
    }
    if (idxInput) {
      idxInput.value = JSON.stringify(sl.map(function (r) { return r.i; }));
    }

    var list = modal.querySelector('.shortlist-review');
    if (list) {
      if (sl.length === 0) {
        // The button is no longer `disabled` (Phase 5.2), so the modal is
        // reachable with nothing starred. An empty <ul> under a
        // "Shortlisted candidates:" heading reads as a rendering fault; say
        // what happened instead.
        list.innerHTML =
          '<li>Nothing starred yet. Close this and star the designs you '
          + 'want to send.</li>';
      } else {
        list.innerHTML = sl.map(function (r) {
          var label = 'Candidate ' + (r.i + 1);
          // "sub-job" is a CAMPAIGN/TARGET word. Those tables interleave
          // rows from several jobs, so the id disambiguates; a single-job
          // table has exactly one, and calling it a sub-job of itself reads
          // as a rendering fault. Keyed on the parent field rather than on
          // `refsInput`, which stopped identifying scope the moment A91 gave
          // job mode a refs input of its own.
          if (!singleJob && r.j) label += ' · sub-job ' + String(r.j).slice(0, 8);
          return '<li>' + label + '</li>';
        }).join('');
      }
    }

    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
  };

  window.closeCampaignModal = function (scope) {
    var modal = document.getElementById('campaign-modal-' + scope);
    if (modal) modal.style.display = 'none';
    document.body.style.overflow = '';
  };

  // ─── Boot ────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-cand-table-id]').forEach(initTable);
  });

  // A back/forward-cached page is restored with its DOM exactly as it was left
  // and `DOMContentLoaded` does not fire again, so a results page restored that
  // way can show stars the store no longer holds. That divergence became
  // possible when the lab-project confirmation page started writing this key:
  // before it, nothing outside the results document ever did.
  //
  // Repaints from the store; deliberately NOT initTable, which would bind a
  // second copy of every listener and make one star click toggle twice. Reads
  // through the same loadShortlist the toggle writes with, so a restore cannot
  // disagree with a click.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    document.querySelectorAll('[data-cand-table-id]').forEach(function (wrapEl) {
      var scope = wrapEl.dataset.scope || wrapEl.dataset.jobId;
      var table = document.getElementById(wrapEl.dataset.candTableId);
      if (table) restoreStarState(table, scope);
      updateShortlistUI(scope);
    });
  });
})();
