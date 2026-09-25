// "Check my settings": POSTs the whole form to /tools/<slug>/validate with
// fetch and shows the verdict inline.
(function () {
  document.querySelectorAll("[data-check-settings]").forEach(function (btn) {
    var form = btn.closest("form");
    var out = btn.parentNode.querySelector("[data-check-settings-result]");
    if (!form || !out) return;
    var slug = form.getAttribute("data-tool-slug");
    btn.addEventListener("click", function () {
      btn.disabled = true;
      out.className = "check-settings-result";
      out.textContent = "Checking…";
      var body = new FormData(form);
      // The route only checks that a file was chosen (blueprints/tools.py::_has_pdb_source).
      Array.prototype.forEach.call(form.querySelectorAll("input[type=file]"), function (el) {
        if (el.files && el.files.length) body.set(el.name, new Blob([]), el.files[0].name);
      });
      // _campaign_reroute.html repoints the form at campaigns when it goes over the ceiling.
      if ((form.getAttribute("action") || "").indexOf("/tools/" + slug + "/submit") === -1) {
        body.set("_campaign", "1");
      }
      fetch("/tools/" + encodeURIComponent(slug) + "/validate", {
        method: "POST",
        body: body,
        credentials: "same-origin",
      })
        .then(function (r) { return r.json(); })
        .then(function (v) {
          out.className = "check-settings-result " + (v.ok ? "is-ok" : "is-error");
          out.textContent = v.ok
            ? "Settings look good. Nothing was charged and no run was started."
            : v.error;
        })
        .catch(function () {
          out.className = "check-settings-result is-error";
          out.textContent = "Couldn't check right now. Submit still re-checks everything.";
        })
        .finally(function () { btn.disabled = false; });
    });
  });
})();
