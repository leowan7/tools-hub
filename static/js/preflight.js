// Preflight panel driver for binder design tool forms.
//
// Activates whenever a #preflight-panel element is present on the page.
// Listens for:
//   - change events on input[type="file"][name="target_pdb"] → POST upload to
//     /tools/<slug>/preflight, render the verdict, toggle the Run button.
//   - click events on .preflight-af-btn → POST { alphafold_accession } to the
//     same endpoint, render the verdict, and stash the reuse token on a
//     hidden input so the actual submit fetches the same AF model.
//   - change events on input[name="target_chain"] and
//     input[name="hotspot_residues"] → re-run preflight if a file is attached.
//
// Tool slug is read from the panel's data-tool attribute. The Run button
// must carry id="tool-submit-btn".
//
// We intentionally keep this dependency-free vanilla JS so it loads on the
// existing tools-hub layout without bundler changes.

(function () {
  "use strict";

  const panel = document.getElementById("preflight-panel");
  if (!panel) return;

  const toolSlug = panel.dataset.tool || "";
  if (!toolSlug) {
    console.warn("preflight.js: panel has no data-tool; bailing");
    return;
  }

  const form = panel.closest("form");
  if (!form) {
    console.warn("preflight.js: panel not inside a <form>; bailing");
    return;
  }

  const fileInput =
    form.querySelector('input[type="file"][name="target_pdb"]');
  const chainInput = form.querySelector('input[name="target_chain"]');
  const hotspotInput = form.querySelector('input[name="hotspot_residues"]');
  // Binder-size fields (optional, tool-dependent): pxdesign has a numeric
  // binder_length; boltz2 carries a binder_sequences textarea. Forwarding
  // these to the preflight endpoint makes the live combined / total-complex
  // size check match the submit-side gate, which sees the validated value.
  const binderLenInput = form.querySelector('input[name="binder_length"]');
  const binderSeqInput =
    form.querySelector('textarea[name="binder_sequences"]');
  const submitBtn = document.getElementById("tool-submit-btn") ||
                    form.querySelector('button[type="submit"]');

  // We expect a hidden input that the form already declares for reuse
  // tokens (example, job, handoff, resample). Re-use it for the
  // ``alphafold:<accession>`` token so the submit endpoint follows the
  // existing chain.
  let reuseTokenInput = form.querySelector('input[name="reuse_pdb_token"]');
  if (!reuseTokenInput) {
    reuseTokenInput = document.createElement("input");
    reuseTokenInput.type = "hidden";
    reuseTokenInput.name = "reuse_pdb_token";
    form.appendChild(reuseTokenInput);
  }

  function setSubmitEnabled(enabled) {
    if (!submitBtn) return;
    submitBtn.disabled = !enabled;
    submitBtn.setAttribute("aria-disabled", String(!enabled));
  }

  function showLoading(label) {
    panel.className = "preflight-panel preflight-panel--loading";
    panel.setAttribute("data-kind", "");
    panel.setAttribute("data-ok", "0");
    panel.textContent = label || "Checking your PDB…";
    setSubmitEnabled(false);
  }

  function renderVerdict(v) {
    panel.className =
      "preflight-panel preflight-panel--" + (v.kind || "");
    panel.setAttribute("data-kind", v.kind || "");
    panel.setAttribute("data-ok", v.ok ? "1" : "0");
    let html = "";
    if (v.kind === "ready" || v.kind === "ready_with_fallback") {
      html += `
        <div class="preflight-header preflight-header--ok">
          <span class="preflight-icon" aria-hidden="true">✓</span>
          <strong>Ready to run</strong>
        </div>
        <div class="preflight-body">`;
      if (v.source_label) {
        html += `<div class="preflight-meta">
          Target: <code>${escapeHtml(v.source_label)}</code>,
          chain ${escapeHtml(v.target_chain || "")}
          — ${v.residues_kept_on_target_chain} residues
        </div>`;
      }
      if (v.hotspots && v.hotspots.surviving && v.hotspots.surviving.length) {
        html += `<div class="preflight-meta">
          Hotspots ${v.hotspots.surviving.join(", ")} — all preserved
        </div>`;
      }
      if (v.cleanup_items && v.cleanup_items.length) {
        html += `<div class="preflight-cleanup">Cleanup applied:<ul>`;
        for (const item of v.cleanup_items) {
          html += `<li>${escapeHtml(item)}</li>`;
        }
        html += `</ul></div>`;
      } else {
        html += `<div class="preflight-cleanup preflight-cleanup--nothing">
          Nothing to clean.
        </div>`;
      }
      if (v.kind === "ready_with_fallback" && v.alphafold) {
        html += `<div class="preflight-af preflight-af--soft">
          <div class="preflight-af-icon" aria-hidden="true">💡</div>
          <div class="preflight-af-body">
            <p>
              We recognised this as UniProt
              <code>${escapeHtml(v.alphafold.accession)}</code>.
              The AlphaFold model
              <code>${escapeHtml(v.alphafold.display_id)}</code>
              is single-conformation and tends to give more
              predictable runs. Same hotspot numbering works.
            </p>
            <button type="button" class="preflight-af-btn"
                    data-reuse-token="${escapeHtml(v.alphafold.reuse_token)}"
                    data-accession="${escapeHtml(v.alphafold.accession)}">
              Use AlphaFold model instead
            </button>
          </div>
        </div>`;
      }
      html += `</div>`;
    } else if (v.kind === "needs_fix") {
      html += `
        <div class="preflight-header preflight-header--block">
          <span class="preflight-icon" aria-hidden="true">✗</span>
          <strong>Can't run this target as-is</strong>
        </div>
        <div class="preflight-body">`;
      if (v.reason) {
        html += `<p class="preflight-reason">${escapeHtml(v.reason)}</p>`;
      }
      if (v.suggested_fix) {
        html += `<p class="preflight-fix">
          <strong>Try one of these:</strong><br>
          ${escapeHtml(v.suggested_fix)}
        </p>`;
      }
      if (v.alphafold) {
        html += `<div class="preflight-af preflight-af--hard">
          <button type="button"
                  class="preflight-af-btn preflight-af-btn--hard"
                  data-reuse-token="${escapeHtml(v.alphafold.reuse_token)}"
                  data-accession="${escapeHtml(v.alphafold.accession)}">
            Use AlphaFold model
            <code>${escapeHtml(v.alphafold.display_id)}</code>
          </button>
        </div>`;
      }
      html += `</div>`;
    }
    panel.innerHTML = html;
    setSubmitEnabled(!!v.ok);
    wireAfButtons();
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function maxBinderLen() {
    // Longest binder sequence length, parsed from the boltz2 textarea.
    // FASTA (>name headers) accumulates lines per record; otherwise each
    // non-empty line is its own sequence. Whitespace is stripped. Returns
    // null when the field is absent or empty.
    if (!binderSeqInput || !binderSeqInput.value) return null;
    const lines = binderSeqInput.value.split(/\r?\n/);
    const hasHeaders = lines.some((l) => l.trim().charAt(0) === ">");
    let max = 0;
    if (hasHeaders) {
      let cur = 0;
      for (const raw of lines) {
        const ln = raw.trim();
        if (!ln) continue;
        if (ln.charAt(0) === ">") {
          if (cur > max) max = cur;
          cur = 0;
        } else {
          cur += ln.replace(/\s+/g, "").length;
        }
      }
      if (cur > max) max = cur;
    } else {
      for (const raw of lines) {
        const ln = raw.trim().replace(/\s+/g, "");
        if (ln.length > max) max = ln.length;
      }
    }
    return max > 0 ? max : null;
  }

  function appendBinderFields(fd) {
    // pxdesign: numeric binder_length, forwarded as-is.
    if (binderLenInput && binderLenInput.value) {
      fd.append("binder_length", binderLenInput.value);
    }
    // boltz2: forward the longest binder as binder_length_max, the field
    // _parse_preflight_size_params reads directly. The raw textarea string
    // is ignored server-side (the parser only handles the list shape).
    const bmax = maxBinderLen();
    if (bmax) fd.append("binder_length_max", String(bmax));
  }

  function postPreflight(formData) {
    showLoading();
    return fetch(`/tools/${encodeURIComponent(toolSlug)}/preflight`, {
      method: "POST",
      body: formData,
      credentials: "same-origin",
    })
      .then((r) => r.json())
      .then((v) => {
        renderVerdict(v);
        return v;
      })
      .catch((err) => {
        console.warn("preflight: network error", err);
        panel.className = "preflight-panel preflight-panel--needs_fix";
        panel.setAttribute("data-kind", "needs_fix");
        panel.setAttribute("data-ok", "0");
        panel.innerHTML = `
          <div class="preflight-header preflight-header--block">
            <span class="preflight-icon" aria-hidden="true">✗</span>
            <strong>Preflight failed</strong>
          </div>
          <div class="preflight-body">
            <p class="preflight-reason">
              We couldn't pre-check your PDB. Refresh and try again, or
              submit anyway — the server-side gate will catch real issues.
            </p>
          </div>`;
        // Don't block the user if the network ate our request — let the
        // server gate fire.
        setSubmitEnabled(true);
      });
  }

  function runPreflightFromUpload() {
    if (!fileInput || !fileInput.files || !fileInput.files.length) return;
    // Clear any AF reuse token — fresh file wins.
    if (reuseTokenInput.value.startsWith("alphafold:")) {
      reuseTokenInput.value = "";
    }
    const fd = new FormData();
    fd.append("target_pdb", fileInput.files[0]);
    if (chainInput) fd.append("target_chain", chainInput.value || "");
    if (hotspotInput)
      fd.append("hotspot_residues", hotspotInput.value || "");
    appendBinderFields(fd);
    postPreflight(fd);
  }

  function runPreflightFromAlphaFold(accession, reuseToken) {
    const fd = new FormData();
    fd.append("alphafold_accession", accession);
    if (chainInput) fd.append("target_chain", chainInput.value || "");
    if (hotspotInput)
      fd.append("hotspot_residues", hotspotInput.value || "");
    appendBinderFields(fd);
    return postPreflight(fd).then((v) => {
      // If the AF model passes preflight, stash the reuse token so the
      // form's submit endpoint fetches the same AF model on the server
      // side (no re-upload from the browser needed).
      if (v && v.ok) {
        reuseTokenInput.value = reuseToken;
        if (fileInput) fileInput.value = "";
      }
    });
  }

  function wireAfButtons() {
    panel.querySelectorAll(".preflight-af-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const accession = btn.dataset.accession;
        const token = btn.dataset.reuseToken;
        if (!accession || !token) return;
        runPreflightFromAlphaFold(accession, token);
      });
    });
  }

  // Wire core events.
  if (fileInput) {
    fileInput.addEventListener("change", runPreflightFromUpload);
  }
  // Re-run when chain / hotspots / binder size change AND a file is attached.
  for (const inp of [chainInput, hotspotInput, binderLenInput, binderSeqInput]) {
    if (!inp) continue;
    let t = 0;
    inp.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        if (reuseTokenInput.value.startsWith("alphafold:")) {
          // Re-run on the AF model so the hotspot/chain change reflects
          // immediately on the AF target the user already opted into.
          const accession = reuseTokenInput.value.split(":", 2)[1];
          runPreflightFromAlphaFold(
            accession, reuseTokenInput.value,
          );
        } else if (fileInput && fileInput.files && fileInput.files.length) {
          runPreflightFromUpload();
        }
      }, 350);
    });
  }

  // Wire any server-rendered AF buttons present on initial paint.
  wireAfButtons();
})();
