// Fills each <details class="form-section"> summary from its data-summary
// template (templates/components/form_section.html), and opens a section
// when the browser's required-field check lands on a field hidden inside it.
(function () {
  if (window.formSectionsBound) return;  // the macro emits this tag once per section
  window.formSectionsBound = true;

  function fieldText(form, name, noun) {
    var el = form.elements[name];
    if (!el) return "";
    var value = el.value == null ? "" : String(el.value).trim();
    if (noun) {
      var n = el.type === "number"
        ? Number(value) || 0
        : value.split(/[\s,;]+/).filter(Boolean).length;
      return n + " " + noun + (n === 1 ? "" : "s");
    }
    if (el.type === "file") {
      if (el.files && el.files.length) return el.files[0].name;
      return form.elements.reuse_pdb_token && form.elements.reuse_pdb_token.value
        ? "reused structure" : "no file yet";
    }
    if (el.tagName === "SELECT" && el.selectedIndex >= 0) {
      return el.options[el.selectedIndex].text.trim();
    }
    return value || "—";
  }

  function render(section) {
    var form = section.closest("form");
    var out = section.querySelector(".form-section-summary");
    if (!form || !out) return;
    out.textContent = section.getAttribute("data-summary").replace(
      /\{(\w+)(?:#(\w+))?\}/g,
      function (_, name, noun) { return fieldText(form, name, noun); }
    );
  }

  function renderAll() {
    document.querySelectorAll("details.form-section").forEach(render);
  }

  document.addEventListener("input", renderAll);
  document.addEventListener("change", renderAll);
  document.addEventListener("toggle", renderAll, true);
  document.addEventListener("invalid", function (e) {
    var section = e.target.closest && e.target.closest("details.form-section");
    if (section) section.open = true;
  }, true);
  renderAll();
  window.addEventListener("load", function () {
    renderAll();
    // A refused over-ceiling submit re-renders with this notice shown (_campaign_reroute.html).
    document.querySelectorAll(".campaign-reroute-notice:not([hidden])").forEach(function (n) {
      var section = n.closest("details.form-section");
      if (section) section.open = true;
    });
  });
})();
