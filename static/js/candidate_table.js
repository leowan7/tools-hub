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
 *   data-campaign-id  - present only in campaign mode (drives the modal payload)
 * Each star button carries data-job (the candidate's SOURCE job) and
 * data-ref-idx (its index WITHIN that job); data-idx stays the row index used
 * for the 3D viewer rows.
 *
 * Exposes:
 *   window.getShortlist(scope)     → [{j,i}]
 *   window.openCampaignModal(scope)
 *   window.closeCampaignModal(scope)
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
    var sendBtn = document.getElementById('send-to-lab-btn-' + scope);
    if (countEl) countEl.textContent = sl.length;
    if (sendBtn) {
      var disabled = sl.length === 0;
      sendBtn.disabled = disabled;
      sendBtn.title    = disabled ? 'Star at least one candidate first' : '';
    }
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

    // Campaign mode has a candidate_refs field; single-job mode has
    // candidate_indices. Populate whichever the modal carries.
    var refsInput = modal.querySelector('[name="candidate_refs"]');
    var idxInput  = modal.querySelector('[name="candidate_indices"]');
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
      list.innerHTML = sl.map(function (r) {
        var label = 'Candidate ' + (r.i + 1);
        if (refsInput && r.j) label += ' · sub-job ' + String(r.j).slice(0, 8);
        return '<li>' + label + '</li>';
      }).join('');
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
})();
