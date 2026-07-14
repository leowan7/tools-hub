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

  // Sniff the structure format from the payload. Every tool's PDB
  // endpoint serves PDB text today, but a few upstream pipelines can
  // hand back mmCIF. Parsing mmCIF through the 'pdb' reader yields a
  // zero-atom structure — a blank canvas with only the corner axis
  // gizmo — so detect the format and give Mol* the right parser.
  // Conservative: default to 'pdb', switch only on an unambiguous mmCIF
  // signal (a leading ``data_`` block header or a line-anchored
  // ``_atom_site.`` loop tag), so a normal PDB is never misclassified.
  // Both signals are anchored to the start of a line: mmCIF emits them
  // there, and this rules out a false positive from the token appearing
  // mid-line inside a PDB REMARK/TITLE record.
  function detectFormat(text) {
    var head = (text || '').slice(0, 4000);
    if (/^\s*data_\S/.test(head) || /(^|\n)\s*_atom_site\./.test(head)) {
      return 'mmcif';
    }
    return 'pdb';
  }

  // Ask Mol* to re-frame the camera on the loaded structure. Mol* auto-
  // resets the camera once on load, but that reset can fire before the
  // structure geometry has committed to the scene (or while the row is
  // still un-hiding / the canvas resizing), leaving the camera radius at
  // 0: the structure is in the scene but out of view, so the viewport
  // renders nothing but the corner axis gizmo. requestCameraReset waits
  // for a valid bounding sphere before applying, so re-issuing it is safe.
  function focusCamera(viewer) {
    try {
      var c3d = viewer && viewer.plugin && viewer.plugin.canvas3d;
      if (c3d && typeof c3d.requestCameraReset === 'function') {
        c3d.requestCameraReset();
      }
    } catch (e) { /* best-effort; Mol* internals may shift across builds */ }
  }

  // Current camera radius, or -1 when it can't be read (unknown build).
  function cameraRadius(viewer) {
    try { return viewer.plugin.canvas3d.camera.state.radius || 0; }
    catch (e) { return -1; }
  }

  // Poll a camera reset until the structure bounds register (radius > 0)
  // or we hit the cap. This rides out the geometry-commit race without
  // betting on a single magic delay: as soon as the scene has committed
  // real bounds, the reset snaps the camera onto them and we stop.
  function focusUntilReady(viewer, tries) {
    tries = tries || 0;
    focusCamera(viewer);
    if (cameraRadius(viewer) > 0) return;   // focused on real bounds — done
    if (tries >= 16) return;                // ~2s cap — give up
    setTimeout(function () { focusUntilReady(viewer, tries + 1); }, 120);
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
        // Paint the WebGL background to the dark surface token (var(--bg-deep),
        // #08101B) so the viewport blends into the results panel instead of
        // showing Mol*'s default near-white canvas.
        try {
          viewer.plugin.canvas3d.setProps({ renderer: { backgroundColor: 0x08101B } });
        } catch (e) { /* older builds may nest renderer props differently */ }
        return viewer.loadStructureFromData(pdbString, detectFormat(pdbString), false)
          .then(function () {
            // Size the canvas to the now-visible container, then re-frame
            // the camera on a later frame once geometry has committed.
            // Without this the structure can load off-camera and the
            // viewport shows only the axis gizmo (blank-viewer bug).
            resizeViewer(viewer);
            requestAnimationFrame(function () {
              resizeViewer(viewer);
              focusUntilReady(viewer, 0);
            });
          });
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
