/**
 * hotspot_picker.js — interactive 3D residue picker for binder-design
 * forms. Wave 4 per docs/PRODUCT-PLAN.md.
 *
 * Wires a hidden <input type="file"> (PDB upload) to a vendored NGL
 * viewer and a typed <input type="text"> (comma-separated residue
 * indices). Click a residue in the viewer to toggle it in the input;
 * type into the input and the viewer's highlights update to match.
 *
 * Contract:
 *   window.initHotspotPicker({
 *     pdbInputId:     'target_pdb',        // <input type="file">
 *     hotspotInputId: 'hotspot_residues',  // <input type="text">
 *     chainInputId:   'target_chain',      // <input type="text"> (optional)
 *     viewerId:       'hotspot-viewer',    // <div> container
 *     emptyMessageId: 'hotspot-empty',     // element shown pre-upload
 *   })
 *
 * Requirements:
 *   - /static/vendor/ngl.min.js must be loaded before this file.
 *   - Form submit behaviour is unchanged — the hotspot input still
 *     posts as comma-separated ints; the picker only mutates its value.
 */
(function () {
  'use strict';

  function parseHotspots(text) {
    if (!text) return [];
    var out = [];
    var seen = Object.create(null);
    var parts = String(text).split(',');
    for (var i = 0; i < parts.length; i++) {
      var tok = parts[i].trim();
      if (!tok) continue;
      var n = parseInt(tok, 10);
      if (isNaN(n)) continue;
      if (seen[n]) continue;
      seen[n] = true;
      out.push(n);
    }
    return out;
  }

  function formatHotspots(list) {
    return list.slice().sort(function (a, b) { return a - b; }).join(',');
  }

  function HotspotPicker(opts) {
    this.pdbInput = document.getElementById(opts.pdbInputId);
    this.hotspotInput = document.getElementById(opts.hotspotInputId);
    this.chainInput = opts.chainInputId ? document.getElementById(opts.chainInputId) : null;
    this.viewerEl = document.getElementById(opts.viewerId);
    this.emptyEl = opts.emptyMessageId ? document.getElementById(opts.emptyMessageId) : null;
    this.surfaceToggle = opts.surfaceToggleId ? document.getElementById(opts.surfaceToggleId) : null;
    this.clearBtn = opts.clearBtnId ? document.getElementById(opts.clearBtnId) : null;

    this.stage = null;
    this.component = null;
    this.cartoonRepr = null;
    this.surfaceRepr = null;
    this.hotspotRepr = null;
    this.currentChain = null;
  }

  HotspotPicker.prototype.init = function () {
    if (!this.pdbInput || !this.hotspotInput || !this.viewerEl) {
      return;
    }

    var self = this;
    this.pdbInput.addEventListener('change', function (e) {
      var file = e.target.files && e.target.files[0];
      if (!file) return;
      self._loadFile(file);
    });
    this.hotspotInput.addEventListener('input', function () {
      self._refreshHotspotRepr();
    });
    if (this.chainInput) {
      this.chainInput.addEventListener('input', function () {
        self.currentChain = self._chain();
        self._refreshHotspotRepr();
      });
    }
    if (this.surfaceToggle) {
      this.surfaceToggle.addEventListener('change', function () {
        self._toggleSurface(self.surfaceToggle.checked);
      });
    }
    if (this.clearBtn) {
      this.clearBtn.addEventListener('click', function (e) {
        e.preventDefault();
        self._setHotspots([]);
      });
    }
  };

  HotspotPicker.prototype._chain = function () {
    if (!this.chainInput) return null;
    var v = (this.chainInput.value || '').trim();
    return v || null;
  };

  HotspotPicker.prototype._loadFile = function (file) {
    var self = this;
    if (typeof NGL === 'undefined') {
      this.viewerEl.innerHTML =
        '<div class="hotspot-viewer-error">NGL viewer failed to load. ' +
        'Typed hotspot entry still works.</div>';
      return;
    }

    if (!this.stage) {
      this.stage = new NGL.Stage(this.viewerEl, {
        backgroundColor: '#0D1520',
      });
      window.addEventListener('resize', function () {
        if (self.stage) self.stage.handleResize();
      });
    } else if (this.component) {
      this.stage.removeComponent(this.component);
      this.component = null;
      this.cartoonRepr = null;
      this.surfaceRepr = null;
      this.hotspotRepr = null;
    }

    if (this.emptyEl) this.emptyEl.style.display = 'none';
    this.viewerEl.style.display = 'block';

    var ext = (file.name.split('.').pop() || 'pdb').toLowerCase();
    var fmt = ext === 'cif' || ext === 'mmcif' ? 'cif' : 'pdb';

    this.stage.loadFile(file, { ext: fmt }).then(function (comp) {
      self.component = comp;
      self.currentChain = self._chain();

      self.cartoonRepr = comp.addRepresentation('cartoon', {
        sele: self._chainSel(),
        colorScheme: 'chainname',
        smoothSheet: true,
      });
      if (self.surfaceToggle && self.surfaceToggle.checked) {
        self._toggleSurface(true);
      }
      comp.autoView(self._chainSel());

      // Click handler — map picked atom back to (chain, resno) and
      // toggle that residue number in the hotspot input.
      self.stage.signals.clicked.add(function (pickingProxy) {
        if (!pickingProxy) return;
        var atom = pickingProxy.atom || (pickingProxy.closestBondAtom && pickingProxy.closestBondAtom());
        if (!atom) return;
        var chain = atom.chainname || atom.chainid;
        var expected = self._chain();
        if (expected && chain && chain !== expected) {
          return;
        }
        self._toggleResidue(atom.resno);
      });

      self._refreshHotspotRepr();
    }).catch(function (err) {
      console.error('[hotspot_picker] NGL load failed:', err);
      self.viewerEl.innerHTML =
        '<div class="hotspot-viewer-error">Could not parse this ' +
        'structure. Typed hotspot entry still works.</div>';
    });
  };

  HotspotPicker.prototype._chainSel = function () {
    var chain = this._chain();
    return chain ? (':' + chain) : 'polymer';
  };

  HotspotPicker.prototype._hotspotSel = function () {
    var resnos = parseHotspots(this.hotspotInput.value);
    if (!resnos.length) return null;
    var chain = this._chain();
    var chainSuffix = chain ? (' and :' + chain) : '';
    // NGL selection language: "(10 or 12 or 54) and :A"
    return '(' + resnos.join(' or ') + ')' + chainSuffix;
  };

  HotspotPicker.prototype._refreshHotspotRepr = function () {
    if (!this.component) return;
    if (this.hotspotRepr) {
      this.component.removeRepresentation(this.hotspotRepr);
      this.hotspotRepr = null;
    }
    var sel = this._hotspotSel();
    if (!sel) return;
    this.hotspotRepr = this.component.addRepresentation('ball+stick', {
      sele: sel,
      color: '#2B9E7E',
      aspectRatio: 2.0,
      radiusScale: 1.4,
    });
  };

  HotspotPicker.prototype._toggleSurface = function (on) {
    if (!this.component) return;
    if (on && !this.surfaceRepr) {
      this.surfaceRepr = this.component.addRepresentation('surface', {
        sele: this._chainSel(),
        opacity: 0.25,
        colorScheme: 'uniform',
        colorValue: '#4F5B6B',
        surfaceType: 'av',
      });
    } else if (!on && this.surfaceRepr) {
      this.component.removeRepresentation(this.surfaceRepr);
      this.surfaceRepr = null;
    }
  };

  HotspotPicker.prototype._toggleResidue = function (resno) {
    var current = parseHotspots(this.hotspotInput.value);
    var idx = current.indexOf(resno);
    if (idx >= 0) {
      current.splice(idx, 1);
    } else {
      current.push(resno);
    }
    this._setHotspots(current);
  };

  HotspotPicker.prototype._setHotspots = function (list) {
    this.hotspotInput.value = formatHotspots(list);
    // Fire an input event so any other listeners stay in sync.
    var evt;
    try {
      evt = new Event('input', { bubbles: true });
    } catch (_) {
      evt = document.createEvent('Event');
      evt.initEvent('input', true, true);
    }
    this.hotspotInput.dispatchEvent(evt);
    this._refreshHotspotRepr();
  };

  window.initHotspotPicker = function (opts) {
    var picker = new HotspotPicker(opts);
    picker.init();
    return picker;
  };

  // Expose for unit tests.
  window.__hotspotPickerUtils = {
    parseHotspots: parseHotspots,
    formatHotspots: formatHotspots,
  };
})();
