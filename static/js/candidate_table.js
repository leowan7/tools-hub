/**
 * Candidate table behaviour: column sort, star/shortlist, sessionStorage
 * persistence, 3D viewer expand, lab-submit modal population, and the metric
 * header tooltip.
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

  // ─── Metric tooltip ──────────────────────────────────────────────────────
  //
  // ONE element, appended to <body>, positioned from here rather than by CSS.
  //
  // It used to be a ::after on the icon, which put its containing block inside
  // .cand-table-scroll. That div sets overflow-x: auto, and per spec a
  // non-visible overflow on one axis makes the other axis clip too, so the
  // tooltip was cut at the scroller's bottom edge -- while .panel above it is
  // `overflow: hidden` outright, so anchoring to the top edge instead (which
  // is what the previous attempt did) only changed which end was lost. How
  // much was lost depended on the room the table left below its own header,
  // so a short run cut most of the text and a long one cut none: the defect
  // hid on exactly the pages people read for longest.
  //
  // An overflow ancestor clips a descendant only when it also contains that
  // descendant's containing block, so what the box is ANCHORED to is what
  // decides. Anchoring to the viewport puts it outside both clippers, which
  // means position: fixed -- and a fixed box has to be told where to go and
  // kept there as things scroll under it. That is the whole reason the
  // placement is here rather than in CSS. See the .mtt-pop rule in
  // components/candidate_table.html.

  var popEl  = null;   // the shared tooltip element, created on first use
  var popFor = null;   // the icon it currently describes, for aria-describedby

  function tooltipEl() {
    if (!popEl) {
      popEl = document.createElement('div');
      popEl.className = 'mtt-pop';
      popEl.id        = 'mtt-pop';
      popEl.setAttribute('role', 'tooltip');
      document.body.appendChild(popEl);
    }
    return popEl;
  }

  function placeTooltip(icon, el) {
    var r    = icon.getBoundingClientRect();
    var w    = el.offsetWidth;
    var h    = el.offsetHeight;
    var edge = 8;   // keep clear of the viewport edges
    var gap  = 7;   // the .35rem the old ::after used, at a 20px root

    // ``* 0.5`` rather than ``/ 2``: the comment stripper in
    // tests/test_candidate_table_js_contract.py cannot tell a division slash
    // from the start of a regex literal, and refuses a file containing either.
    var left = r.left + (r.width * 0.5) - (w * 0.5);
    var max  = window.innerWidth - w - edge;
    if (left > max)  left = max;
    if (left < edge) left = edge;

    // Below the icon by default, which is where it has always sat. Flip above
    // only when below overflows the viewport AND above genuinely fits.
    var top = r.bottom + gap;
    if (top + h > window.innerHeight - edge && r.top - h - gap > edge) {
      top = r.top - h - gap;
    }
    // THEN CLAMP, because neither side may fit. Preferring below and flipping
    // above covers only two of three cases, and the third is common: a 480px
    // tooltip in a 720px window with its icon mid-page overflows below and is
    // too tall to go above, so the flip does not fire and the box ran off the
    // bottom of the screen -- 282px of it, on the widest glossary entry. That
    // is worse than the scroller clip this whole change exists to remove.
    // Overlapping its own icon is the ordinary tradeoff; being unreadable is
    // not one. Same two-sided clamp `left` gets above.
    var lowest = window.innerHeight - h - edge;
    if (top > lowest) top = lowest;
    if (top < edge)   top = edge;
    // A tooltip taller than the viewport still overflows the bottom, but now
    // starts at the top edge, so the definition -- the sentence that opens
    // the text -- is the part that survives.

    el.style.left = left + 'px';
    el.style.top  = top + 'px';
  }

  function showTooltip(icon) {
    var text = icon.getAttribute('data-tooltip');
    if (!text) return;
    var el = tooltipEl();
    // RELEASE THE PREVIOUS ICON before re-pointing the box at this one. There
    // is a single tooltip element and a single id, so the icon it used to
    // describe must give up `aria-describedby` or two icons claim the same
    // description. Focusing one icon and then hovering another did exactly
    // that: the focused icon went on advertising an id whose text was now the
    // other column's, so a screen reader announced the wrong metric for the
    // control the user was actually on -- and the attribute then outlived the
    // tooltip, because hideTooltip only ever clears the LAST icon.
    if (popFor && popFor !== icon) popFor.removeAttribute('aria-describedby');
    el.textContent = text;
    // Placed BEFORE it is shown. The box is measurable while visibility:hidden,
    // so moving to a second icon never flashes at the first one's coordinates.
    placeTooltip(icon, el);
    el.classList.add('is-open');
    icon.setAttribute('aria-describedby', el.id);
    popFor = icon;
  }

  function hideTooltip() {
    if (popEl)  popEl.classList.remove('is-open');
    if (popFor) popFor.removeAttribute('aria-describedby');
    popFor = null;
  }

  function tooltipIcon(e) {
    var t = e.target;
    return (t && t.closest) ? t.closest('.mtt[data-tooltip]') : null;
  }

  // The icon holding focus, if any. Focus outlives a hover, so leaving a
  // hovered icon returns to this one instead of closing.
  function focusedIcon() {
    var a = document.activeElement;
    return (a && a.closest) ? a.closest('.mtt[data-tooltip]') : null;
  }

  // Keep the box on its icon while anything scrolls under it, rather than
  // closing. Closing on scroll defeats the keyboard path this change added:
  // tabbing to an icon in an off-screen column makes the browser scroll it
  // into view, and that scroll event is dispatched from "update the
  // rendering" -- AFTER the synchronous focusin -- so the tooltip the focus
  // had just opened was closed by the scroll the focus itself caused.
  function trackTooltip() {
    if (!popFor || !popEl || !popEl.classList.contains('is-open')) return;
    if (!popFor.isConnected) { hideTooltip(); return; }
    var r = popFor.getBoundingClientRect();
    // Its icon has scrolled out of the viewport. A box still pinned to the
    // edge would be describing a column nobody can see.
    if (r.bottom < 0 || r.top > window.innerHeight) { hideTooltip(); return; }
    placeTooltip(popFor, popEl);
  }

  // Delegated on `document`, not bound per table: the listeners then cover
  // every table on the page, survive a bfcache restore without being rebound
  // (which is what would double them up), and cost one handler either way.
  function initTooltips() {
    document.addEventListener('pointerover', function (e) {
      var icon = tooltipIcon(e);
      if (icon) showTooltip(icon);
    });
    document.addEventListener('pointerout', function (e) {
      if (!tooltipIcon(e)) return;
      // A touch pointer is destroyed after pointerup and the UA then fires
      // pointerout, so honouring it on touch would close the tooltip the tap
      // had just opened -- and the old `:hover` rule did NOT do that, because
      // mobile browsers make hover sticky after a tap. Touch closes on the
      // next pointerdown elsewhere instead.
      if (e.pointerType === 'touch') return;
      // Never close a tooltip the keyboard is holding open. Without this, a
      // user reading a focused icon's tooltip lost it to any stray mouse
      // movement across the table, with no way back but blur and refocus.
      var focused = focusedIcon();
      if (focused) { showTooltip(focused); return; }
      hideTooltip();
    });
    document.addEventListener('pointerdown', function (e) {
      if (!tooltipIcon(e)) hideTooltip();
    });
    // Reached without a mouse through tabindex="0" on the span.
    document.addEventListener('focusin', function (e) {
      var icon = tooltipIcon(e);
      if (icon) showTooltip(icon);
    });
    document.addEventListener('focusout', function (e) {
      if (tooltipIcon(e)) hideTooltip();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') hideTooltip();
    });
    // A fixed box does not travel with the page, and this table carries a
    // scroller of its own. Capture phase so a scroll of .cand-table-scroll is
    // caught as well as the window's.
    window.addEventListener('scroll', trackTooltip, true);
    window.addEventListener('resize', trackTooltip);
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
    // Once, not per table: the tooltip listeners are delegated on `document`
    // and one set already covers every table on the page.
    initTooltips();
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
