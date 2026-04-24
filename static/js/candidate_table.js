/**
 * Candidate table behaviour: column sort, star/shortlist, sessionStorage
 * persistence, 3D viewer expand, and campaign modal population.
 *
 * Initialised automatically for every [data-cand-table-id] wrapper on
 * DOMContentLoaded.  Exposes:
 *   window.getShortlist(jobId)        → int[]
 *   window.openCampaignModal(jobId)
 *   window.closeCampaignModal(jobId)
 */
(function () {
  'use strict';

  // ─── sessionStorage helpers ──────────────────────────────────────────────

  function storageKey(jobId) { return 'shortlist_' + jobId; }

  function loadShortlist(jobId) {
    try {
      var raw = sessionStorage.getItem(storageKey(jobId));
      return raw ? JSON.parse(raw) : [];
    } catch (_) { return []; }
  }

  function saveShortlist(jobId, indices) {
    try { sessionStorage.setItem(storageKey(jobId), JSON.stringify(indices)); }
    catch (_) {}
  }

  // ─── UI helpers ──────────────────────────────────────────────────────────

  function updateShortlistUI(jobId) {
    var sl       = loadShortlist(jobId);
    var countEl  = document.getElementById('shortlist-count-' + jobId);
    var sendBtn  = document.getElementById('send-to-lab-btn-' + jobId);
    if (countEl) countEl.textContent = sl.length;
    if (sendBtn) {
      var disabled = sl.length === 0;
      sendBtn.disabled = disabled;
      sendBtn.title    = disabled ? 'Star at least one candidate first' : '';
    }
  }

  function restoreStarState(table, jobId) {
    var sl = loadShortlist(jobId);
    table.querySelectorAll('.star-btn').forEach(function (btn) {
      var idx = parseInt(btn.dataset.idx, 10);
      var on  = sl.indexOf(idx) !== -1;
      btn.classList.toggle('starred', on);
      btn.textContent = on ? '\u2605' : '\u2606';
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
    var jobId   = wrapEl.dataset.jobId;
    var table   = document.getElementById(tableId);
    if (!table) return;

    restoreStarState(table, jobId);
    updateShortlistUI(jobId);

    // Star toggle
    table.addEventListener('click', function (e) {
      var btn = e.target.closest('.star-btn');
      if (!btn) return;
      var idx = parseInt(btn.dataset.idx, 10);
      var sl  = loadShortlist(jobId);
      var pos = sl.indexOf(idx);
      if (pos === -1) {
        sl.push(idx);
        btn.classList.add('starred');
        btn.textContent = '\u2605';
      } else {
        sl.splice(pos, 1);
        btn.classList.remove('starred');
        btn.textContent = '\u2606';
      }
      saveShortlist(jobId, sl);
      updateShortlistUI(jobId);
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
      if (opening && window.initMolViewer) {
        window.initMolViewer('mol-viewer-' + idx, btn.dataset.pdb64 || '');
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

  window.getShortlist = function (jobId) { return loadShortlist(jobId); };

  window.openCampaignModal = function (jobId) {
    var sl    = loadShortlist(jobId);
    var modal = document.getElementById('campaign-modal-' + jobId);
    if (!modal) return;

    // Populate hidden indices field.
    var inp = modal.querySelector('[name="candidate_indices"]');
    if (inp) inp.value = JSON.stringify(sl);

    // Update review list.
    var list = modal.querySelector('.shortlist-review');
    if (list) {
      list.innerHTML = sl.map(function (i) {
        return '<li>Candidate ' + (i + 1) + '</li>';
      }).join('');
    }

    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
  };

  window.closeCampaignModal = function (jobId) {
    var modal = document.getElementById('campaign-modal-' + jobId);
    if (modal) modal.style.display = 'none';
    document.body.style.overflow = '';
  };

  // ─── Boot ────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-cand-table-id]').forEach(initTable);
  });
})();
