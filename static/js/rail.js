/* Left rail: desktop collapse (persisted) and mobile drawer.
 *
 * The collapsed class is also applied before first paint by an inline
 * script in templates/base.html; this file owns the toggling and the
 * localStorage write. Loaded only for signed-in users.
 */
(function () {
  'use strict';

  var KEY = 'rail_collapsed';
  var root = document.documentElement;
  var rail = document.getElementById('app-rail');
  var collapseBtn = document.getElementById('rail-collapse');
  var drawerBtn = document.getElementById('rail-drawer-toggle');
  var backdrop = document.getElementById('rail-backdrop');

  if (!rail) return;

  function isCollapsed() {
    return root.getAttribute('data-rail') === 'collapsed';
  }

  function syncCollapseLabel() {
    if (!collapseBtn) return;
    var collapsed = isCollapsed();
    collapseBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    var sr = collapseBtn.querySelector('.rail-sr');
    if (sr) sr.textContent = collapsed ? 'Expand navigation' : 'Collapse navigation';
  }

  if (collapseBtn) {
    syncCollapseLabel();
    collapseBtn.addEventListener('click', function () {
      // In the drawer the same control is the only in-panel way out, so
      // it closes the drawer rather than writing a desktop-only state.
      if (root.getAttribute('data-rail-drawer') === 'open') {
        setDrawer(false);
        if (drawerBtn) drawerBtn.focus();
        return;
      }
      var next = !isCollapsed();
      if (next) {
        root.setAttribute('data-rail', 'collapsed');
      } else {
        root.removeAttribute('data-rail');
      }
      try { localStorage.setItem(KEY, next ? '1' : '0'); } catch (e) { /* ignore */ }
      syncCollapseLabel();
    });
  }

  function setDrawer(open) {
    if (open) {
      root.setAttribute('data-rail-drawer', 'open');
    } else {
      root.removeAttribute('data-rail-drawer');
    }
    if (backdrop) backdrop.hidden = !open;
    if (drawerBtn) {
      drawerBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      var sr = drawerBtn.querySelector('.rail-sr');
      if (sr) sr.textContent = open ? 'Close navigation' : 'Open navigation';
    }
    if (open) {
      var first = rail.querySelector('.rail-link');
      if (first) first.focus();
    }
  }

  if (drawerBtn) {
    drawerBtn.addEventListener('click', function () {
      setDrawer(root.getAttribute('data-rail-drawer') !== 'open');
    });
  }
  if (backdrop) {
    backdrop.addEventListener('click', function () {
      setDrawer(false);
      if (drawerBtn) drawerBtn.focus();
    });
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && root.getAttribute('data-rail-drawer') === 'open') {
      setDrawer(false);
      if (drawerBtn) drawerBtn.focus();
    }
  });
})();
