/**
 * Lazy-loads Mol* from CDN and initialises a structure viewer in the
 * target div.  The first call triggers the CDN load; subsequent calls
 * fire immediately after the script resolves.
 *
 * Usage:
 *   window.initMolViewer('mol-viewer-0', '<base64-encoded PDB string>');
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

  window.initMolViewer = function (containerId, pdbBase64) {
    var container = document.getElementById(containerId);
    if (!container) return;
    if (container.dataset.initialized) return;
    container.dataset.initialized = 'true';
    container.style.position = 'relative';

    // Show a loading indicator while the CDN script fetches.
    container.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;' +
      'height:100%;color:#6b7280;font-size:.85rem;">Loading 3D viewer…</div>';

    loadMolstar(function () {
      container.innerHTML = '';
      var pdbString = '';
      try {
        pdbString = atob(pdbBase64);
      } catch (e) {
        container.innerHTML =
          '<p style="color:#f87171;padding:1rem;">Could not decode PDB data.</p>';
        return;
      }

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
        viewer.loadStructureFromData(pdbString, 'pdb', false);
      }).catch(function (err) {
        console.error('[mol_viewer] Viewer creation failed:', err);
        container.innerHTML =
          '<p style="color:#f87171;padding:1rem;">Viewer failed to initialise.</p>';
      });
    });
  };
})();
