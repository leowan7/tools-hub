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
 *     chainPrefixed:  false,               // opt-in, see below
 *   })
 *
 * Requirements:
 *   - /static/vendor/ngl.min.js must be loaded before this file.
 *   - Form submit behaviour is unchanged — the hotspot input still
 *     posts as comma-separated ints; the picker only mutates its value.
 *
 * chainPrefixed (default false):
 *   Off — the historical behaviour, unchanged: bare ints ("45,67"), one
 *   chain, clicks outside that chain ignored. Every tool but proteina.
 *   On  — tokens carry their chain ("A45,C73") and the chain field may name
 *   several ("A B C"), which proteina needs: upstream matches hotspots as
 *   chain+resnum with no separator, and a three-chain target is a validated
 *   upstream example. Without the flag a multi-chain field would produce the
 *   invalid NGL selection ":A B C" and reject every click.
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

  // --- chain-prefixed mode (opt-in, default OFF) -------------------------
  //
  // Proteina matches hotspots as chain id + author residue number with no
  // separator ("A45"), and supports multi-chain targets ("A12-157,B12-157").
  // The bare-int model above cannot express either: on a multi-chain target
  // it cannot say WHICH chain's residue 45, and _chain() would hand NGL the
  // invalid selection ":A B C" while the click filter rejected every pick.
  //
  // These helpers are only reached when opts.chainPrefixed is set. Every
  // existing caller (rfdiffusion, bindcraft, pxdesign, boltzgen, rfantibody)
  // leaves it unset and runs the identical code it always did — that is
  // deliberate, because this file is shared infrastructure and changing the
  // token format under those tools would break their adapters.

  function parseHotspotTokens(text) {
    if (!text) return [];
    var out = [];
    var seen = Object.create(null);
    var parts = String(text).replace(/;/g, ',').replace(/,/g, ' ').split(/\s+/);
    for (var i = 0; i < parts.length; i++) {
      var tok = parts[i].trim();
      if (!tok) continue;
      var m = /^([A-Za-z])?(-?\d+)$/.exec(tok);
      if (!m) continue;
      var chain = m[1] || null;
      var resno = parseInt(m[2], 10);
      if (isNaN(resno)) continue;
      var key = (chain || '') + ':' + resno;
      if (seen[key]) continue;
      seen[key] = true;
      out.push({ chain: chain, resno: resno });
    }
    return out;
  }

  function formatHotspotTokens(list) {
    return list.slice().sort(function (a, b) {
      var ac = a.chain || '', bc = b.chain || '';
      if (ac !== bc) return ac < bc ? -1 : 1;
      return a.resno - b.resno;
    }).map(function (t) {
      return (t.chain || '') + t.resno;
    }).join(',');
  }

  function HotspotPicker(opts) {
    this.pdbInput = document.getElementById(opts.pdbInputId);
    this.hotspotInput = document.getElementById(opts.hotspotInputId);
    this.chainInput = opts.chainInputId ? document.getElementById(opts.chainInputId) : null;
    this.viewerEl = document.getElementById(opts.viewerId);
    this.emptyEl = opts.emptyMessageId ? document.getElementById(opts.emptyMessageId) : null;
    this.surfaceToggle = opts.surfaceToggleId ? document.getElementById(opts.surfaceToggleId) : null;
    this.clearBtn = opts.clearBtnId ? document.getElementById(opts.clearBtnId) : null;
    // Opt-in: emit "A45" tokens and support a multi-chain target chain field
    // ("A B C"). Off by default so every existing caller is untouched.
    this.chainPrefixed = !!opts.chainPrefixed;

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

  // Every chain named by the chain field. Single-chain mode keeps returning
  // the raw value as one entry, so ":A" is unchanged; chain-prefixed mode
  // splits "A B C" into three, which is what makes a multi-chain target
  // selectable and clickable at all.
  HotspotPicker.prototype._chains = function () {
    var v = this._chain();
    if (!v) return [];
    if (!this.chainPrefixed) return [v];
    return v.split(/[\s,]+/).filter(function (c) { return !!c; });
  };

  HotspotPicker.prototype._loadFile = function (file) {
    var self = this;
    if (typeof NGL === 'undefined') {
      this.viewerEl.innerHTML =
        '<div class="hotspot-viewer-error">NGL viewer failed to load. ' +
        'Typed hotspot entry still works.</div>';
      return;
    }

    // The viewer div ships display:none. NGL.Stage captures container
    // dimensions at construction; a 0x0 container yields a 0x0 canvas
    // that never auto-recovers. Flip visibility BEFORE constructing.
    if (this.emptyEl) this.emptyEl.style.display = 'none';
    this.viewerEl.style.display = 'block';

    if (!this.stage) {
      this.stage = new NGL.Stage(this.viewerEl, {
        backgroundColor: '#0D1520',
      });
      window.addEventListener('resize', function () {
        if (self.stage) self.stage.handleResize();
      });
      // Defensive: also handle container resize (responsive layouts,
      // panels collapsing). ResizeObserver is supported in all modern
      // browsers; degrade silently if missing.
      if (typeof ResizeObserver !== 'undefined') {
        var ro = new ResizeObserver(function () {
          if (self.stage) self.stage.handleResize();
        });
        ro.observe(this.viewerEl);
      }
    } else if (this.component) {
      this.stage.removeComponent(this.component);
      this.component = null;
      this.cartoonRepr = null;
      this.surfaceRepr = null;
      this.hotspotRepr = null;
    }
    // Belt-and-suspenders: if the stage was somehow constructed against
    // a 0x0 container (race, browser quirk), force a resize now that
    // we've laid out at the real dimensions.
    this.stage.handleResize();

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
        var expected = self._chains();
        if (expected.length && chain && expected.indexOf(chain) === -1) {
          return;
        }
        self._toggleResidue(atom.resno, chain);
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
    var chains = this._chains();
    if (!chains.length) return 'polymer';
    if (chains.length === 1) return ':' + chains[0];
    // ":A B C" is not valid NGL; a multi-chain target has to be a disjunction.
    return '(' + chains.map(function (c) { return ':' + c; }).join(' or ') + ')';
  };

  HotspotPicker.prototype._hotspotSel = function () {
    if (!this.chainPrefixed) {
      var resnos = parseHotspots(this.hotspotInput.value);
      if (!resnos.length) return null;
      var chain = this._chain();
      var chainSuffix = chain ? (' and :' + chain) : '';
      // NGL selection language: "(10 or 12 or 54) and :A"
      return '(' + resnos.join(' or ') + ')' + chainSuffix;
    }
    var tokens = parseHotspotTokens(this.hotspotInput.value);
    if (!tokens.length) return null;
    var chains = this._chains();
    var fallback = chains.length ? chains[0] : null;
    // Each token carries its own chain, so they cannot share one suffix.
    var parts = tokens.map(function (t) {
      var c = t.chain || fallback;
      return c ? '(' + t.resno + ' and :' + c + ')' : String(t.resno);
    });
    return '(' + parts.join(' or ') + ')';
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

  HotspotPicker.prototype._toggleResidue = function (resno, chain) {
    if (!this.chainPrefixed) {
      var current = parseHotspots(this.hotspotInput.value);
      var idx = current.indexOf(resno);
      if (idx >= 0) {
        current.splice(idx, 1);
      } else {
        current.push(resno);
      }
      this._setHotspots(current);
      return;
    }
    var tokens = parseHotspotTokens(this.hotspotInput.value);
    var chains = this._chains();
    var c = chain || (chains.length ? chains[0] : null);
    var at = -1;
    for (var i = 0; i < tokens.length; i++) {
      // A bare token already in the field matches a click on the default
      // chain, so clicking a residue you typed as "45" removes it rather
      // than adding a duplicate "A45" beside it.
      var tc = tokens[i].chain || (chains.length ? chains[0] : null);
      if (tokens[i].resno === resno && tc === c) { at = i; break; }
    }
    if (at >= 0) {
      tokens.splice(at, 1);
    } else {
      tokens.push({ chain: c, resno: resno });
    }
    this._setHotspots(tokens);
  };

  HotspotPicker.prototype._setHotspots = function (list) {
    this.hotspotInput.value = this.chainPrefixed
      ? formatHotspotTokens(list)
      : formatHotspots(list);
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
    parseHotspotTokens: parseHotspotTokens,
    formatHotspotTokens: formatHotspotTokens,
  };
})();
