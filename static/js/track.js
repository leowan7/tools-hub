/* Lightweight in-house event tracking for tools.ranomics.com.
 *
 * No third-party analytics. Every event posts to /api/track on the
 * same origin, which appends a row to public.user_events. The Flask
 * session cookie carries the authenticated user_id; this script
 * supplies an anonymous session_id (localStorage) so logged-out
 * traffic can be stitched to the signup that follows.
 *
 * Auto-captures:
 *   - page_view        on DOMContentLoaded
 *   - pricing_view     when path === "/pricing"
 *   - tool_form_open   when path matches /tools/<slug>(/.*)?
 *   - tool_form_submit when a <form> inside /tools/<slug> submits
 *
 * Also exposes window.track(eventType, props) for ad-hoc events.
 *
 * Fail-open: all errors are swallowed. Tracking must never break the
 * page.
 */
(function () {
  "use strict";

  var SESSION_KEY = "ranomics_anon_session_id";
  var ENDPOINT = "/api/track";

  function getSessionId() {
    try {
      var existing = localStorage.getItem(SESSION_KEY);
      if (existing) return existing;
      var fresh =
        "s_" + Date.now().toString(36) + "_" +
        Math.random().toString(36).slice(2, 10);
      localStorage.setItem(SESSION_KEY, fresh);
      return fresh;
    } catch (e) {
      return null;
    }
  }

  function send(payload) {
    try {
      var body = JSON.stringify(payload);
      // sendBeacon survives page unload; fall back to fetch otherwise.
      if (
        navigator.sendBeacon &&
        typeof Blob !== "undefined"
      ) {
        var blob = new Blob([body], { type: "application/json" });
        if (navigator.sendBeacon(ENDPOINT, blob)) return;
      }
      fetch(ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body,
        credentials: "same-origin",
        keepalive: true,
      }).catch(function () { /* swallow */ });
    } catch (e) { /* swallow */ }
  }

  function track(eventType, props) {
    if (!eventType) return;
    send({
      event_type: eventType,
      path: location.pathname + location.search,
      props: props || {},
      session_id: getSessionId(),
    });
  }

  window.track = track;

  function parseTool(path) {
    var m = path.match(/^\/tools\/([a-z0-9_-]+)(?:\/|$)/i);
    return m ? m[1] : null;
  }

  function autoCapture() {
    var path = location.pathname;
    track("page_view");

    if (path === "/pricing" || path.indexOf("/pricing") === 0) {
      track("pricing_view");
    }

    var tool = parseTool(path);
    if (tool) {
      track("tool_form_open", { tool: tool });
      document.addEventListener(
        "submit",
        function (ev) {
          var form = ev.target;
          if (!form || form.tagName !== "FORM") return;
          var presetEl = form.querySelector('[name="preset"]');
          var preset = presetEl ? presetEl.value : null;
          track("tool_form_submit", { tool: tool, preset: preset });
        },
        true
      );
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoCapture);
  } else {
    autoCapture();
  }
})();
