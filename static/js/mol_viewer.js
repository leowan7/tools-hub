/**
 * Lazy-loads Mol* from CDN and initialises a structure viewer in the
 * target div.  The first call triggers the CDN load; subsequent calls
 * fire immediately after the script resolves.
 *
 * Two entry points:
 *   window.initMolViewer(containerId, pdbBase64)
 *     — render an inline base64-encoded PDB string. Used for legacy /
 *       webhook-tier jobs that embed the PDB in tool_jobs.result.
 *
 *   window.initMolViewerFromUrl(containerId, url)
 *     — fetch the PDB text from a same-origin URL and render. Used by
 *       the resolver path that serves /api/jobs/<id>/pdb/<filename>
 *       (Storage-backed or inline-b64 fallback, both transparent here).
 */
(function () {
  var MOLSTAR_JS  = 'https://cdn.jsdelivr.net/npm/molstar@4.9.0/build/viewer/molstar.js';
  var MOLSTAR_CSS = 'https://cdn.jsdelivr.net/npm/molstar@4.9.0/build/viewer/molstar.css';

  var _state   = 'idle'; // 'idle' | 'loading' | 'ready'
  var _pending = [];

  function loadMolstar(cb) {
    if (_state === 'ready')   { cb(); return; }
    _pending.push(cb);
    if (_state === 'loading') return;
    _state = 'loading';

    var link = document.createElement('link');
    link.rel  = 'stylesheet';
    link.href = MOLSTAR_CSS;
    document.head.appendChild(link);

    var script  = document.createElement('script');
    script.src  = MOLSTAR_JS;
    script.async = true;
    script.onload = function () {
      _state = 'ready';
      _pending.forEach(function (fn) { fn(); });
      _pending = [];
    };
    script.onerror = function () {
      _state = 'idle';
      _pending = [];
      console.error('[mol_viewer] Failed to load Mol* from CDN.');
    };
    document.head.appendChild(script);
  }

  function showLoading(container) {
    container.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;' +
      'height:100%;color:#6b7280;font-size:.85rem;">Loading 3D viewer…</div>';
  }

  function prepareContainer(containerId) {
    var container = document.getElementById(containerId);
    if (!container) return null;
    if (container.dataset.initialized) return null;
    container.dataset.initialized = 'true';
    container.style.position = 'relative';
    showLoading(container);
    return container;
  }

  // Force Mol* to re-measure its canvas. The viewer's own handleResize()
  // pushes a layout-updated event (the canonical remeasure path in
  // molstar 4.9). If a future build drops it, fall back to a window
  // resize event, which Mol*'s canvas3d also subscribes to.
  function resizeViewer(viewer) {
    if (!viewer) return;
    try {
      if (typeof viewer.handleResize === 'function') {
        viewer.handleResize();
        return;
      }
    } catch (e) { /* fall through to window resize */ }
    try { window.dispatchEvent(new Event('resize')); } catch (e) {}
  }

  function attachResizeHandlers(container, viewer) {
    // Mirror hotspot_picker.js: a viewer constructed while its container
    // was hidden (or resized later by responsive layout / panel collapse)
    // keeps a stale 0x0 canvas unless told to re-measure. Re-measure on
    // both window resize and container resize.
    var onResize = function () { resizeViewer(viewer); };
    window.addEventListener('resize', onResize);
    if (typeof ResizeObserver !== 'undefined') {
      try {
        var ro = new ResizeObserver(function () { resizeViewer(viewer); });
        ro.observe(container);
      } catch (e) { /* ResizeObserver unavailable or threw — degrade silently */ }
    }
  }

  function renderPdbText(container, pdbString) {
    loadMolstar(function () {
      container.innerHTML = '';
      molstar.Viewer.create(container, {
        layoutIsExpanded:       false,
        layoutShowControls:     false,
        layoutShowRemoteState:  false,
        layoutShowSequence:     false,
        layoutShowLog:          false,
        layoutShowLeftPanel:    false,
        viewportShowExpand:     true,
        viewportShowSelectionMode: false,
        viewportShowAnimation:  false,
      }).then(function (viewer) {
        // loadStructureFromData resolves once the structure is in the
        // scene. Re-measure THEN so the canvas matches the (now visible,
        // explicitly sized) container, and keep it correct on later
        // layout changes.
        attachResizeHandlers(container, viewer);
        return viewer.loadStructureFromData(pdbString, 'pdb', false)
          .then(function () { resizeViewer(viewer); });
      }).catch(function (err) {
        console.error('[mol_viewer] Viewer creation failed:', err);
        container.innerHTML =
          '<p style="color:#f87171;padding:1rem;">Viewer failed to initialise.</p>';
      });
    });
  }

  window.initMolViewer = function (containerId, pdbBase64) {
    var container = prepareContainer(containerId);
    if (!container) return;
    var pdbString = '';
    try {
      pdbString = atob(pdbBase64);
    } catch (e) {
      container.innerHTML =
        '<p style="color:#f87171;padding:1rem;">Could not decode PDB data.</p>';
      return;
    }
    renderPdbText(container, pdbString);
  };

  window.initMolViewerFromUrl = function (containerId, url) {
    var container = prepareContainer(containerId);
    if (!container) return;
    fetch(url, { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(function (pdbString) {
        renderPdbText(container, pdbString);
      })
      .catch(function (err) {
        console.error('[mol_viewer] PDB fetch failed for ' + url + ':', err);
        container.innerHTML =
          '<p style="color:#f87171;padding:1rem;">Could not load PDB from server.</p>';
      });
  };
})();
