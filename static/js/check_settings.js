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
      fetch("/tools/" + encodeURIComponent(slug) + "/validate", {
        method: "POST",
        body: new FormData(form),
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
