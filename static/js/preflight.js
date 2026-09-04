// Preflight panel driver for binder design tool forms.
//
// Activates whenever a #preflight-panel element is present on the page.
// Listens for:
//   - change events on input[type="file"][name="target_pdb"] → POST upload to
//     /tools/<slug>/preflight, render the verdict, toggle the Run button.
//   - click events on .preflight-af-btn → POST { alphafold_accession } to the
//     same endpoint, render the verdict, and stash the reuse token on a
//     hidden input so the actual submit fetches the same AF model.
//   - change events on input[name="target_chain"],
//     input[name="hotspot_residues"] and input[name="target_input"] → re-run
//     preflight if a file is attached.
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
  // The contig ("A236-300,B236-300"). THE PANEL AND THE SUBMIT GATE MUST SIZE
  // THE SAME RUN. `preflight_target_segments` (shared/pdb_intake.py) reads this
  // exact form key and hands the parsed segments to the size envelope, which
  // then counts the SELECTION instead of the whole upload. Without this field
  // in the request the server saw no contig, sized the file, and greyed out the
  // Run button for a selection that was well inside the cap — whole 3S7G is
  // 830 aa, `A236-300,B236-300` is 130, and only the second one is the run.
  // Only proteina ships the field today. `querySelector` returns null on every
  // other tool's form, and a null appends nothing, so their requests are
  // unchanged byte for byte.
  const contigInput = form.querySelector('input[name="target_input"]');
  // boltz2 carries a binder_sequences textarea; we forward the longest
  // binder to the preflight endpoint so the live total-complex size check
  // matches the submit-side gate (which sees the validated sequences).
  // pxdesign's binder_length is deliberately NOT forwarded: its combined
  // cap sits behind a lower target cap and can never trip, so forwarding it
  // would be a no-op (see appendBinderFields).
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

  // TWO SCRIPTS OWN THIS BUTTON, so neither may assign `disabled`
  // outright. templates/wallet/_partials.html grabs the same element
  // (its button[type="submit"]:not([data-gate-button]) selector matches
  // #tool-submit-btn) and wrote `disabled = ceiling || hard` on every
  // debounced estimate. A FAILING preflight is the fastest response this
  // route can give -- inspect_pdb_bytes throws on the first token and the
  // route returns before preflight_for_tool -- so it landed first,
  // disabled the button, and the estimate 250ms later re-enabled it: a red
  // "Can't run this target as-is" panel sitting above a live Submit
  // button. It broke the other way too, a ready verdict re-enabling a
  // button the wallet had blocked.
  //
  // Each side now owns one flag and both recompute from the union, so
  // neither has to load first and neither can clear the other's refusal.
  // Keep the two attribute names in step with the wallet twin.
  function setSubmitEnabled(enabled) {
    if (!submitBtn) return;
    if (enabled) {
      delete submitBtn.dataset.blockPreflight;
    } else {
      submitBtn.dataset.blockPreflight = "1";
    }
    submitBtn.disabled = submitBtn.dataset.blockPreflight === "1" ||
                         submitBtn.dataset.blockWallet === "1";
    submitBtn.setAttribute("aria-disabled", String(submitBtn.disabled));
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
        // THE NUMBER ON SCREEN MUST BE THE NUMBER THE GATE JUDGED. This line
        // used to print residues_kept_on_target_chain unconditionally — the
        // whole of the NAMED chains, which equals the file only when the user
        // named every chain in it. Once the contig started reaching the server
        // that became a live contradiction. Take the upload the route tests
        // drive, tests/test_preflight_panel_contract.py::_BIG_UPLOAD — 600 aa
        // over chains A and B. With target_chain "A B" and the contig
        // "A100-164,B236-300" the run is admitted on 130 residues against
        // proteina's 500 cap, and the panel said "Ready to run — 600
        // residues" directly after having refused that same upload for being
        // over it. Nothing on screen reconciled the two.
        //
        // The "named chains" wording is load-bearing and the equation with
        // "the file" was not: name only chain A on the same upload and this
        // count is 300, not 600. There is then nothing to reconcile either,
        // because 300 fits the cap and the upload is never refused — which is
        // why the contradiction above is stated at "A B" specifically.
        //
        // Every figure here is a live /tools/proteina/preflight response, and
        // the pair the contradiction is made of is pinned on that route in
        // exactly this scenario — test_the_verdict_says_which_number_the_gate
        // _counted asserts residues_kept_on_target_chain == 600 alongside
        // size_envelope.residue_count == 130 — so neither can rot. The 300 is
        // _BIG_UPLOAD's chain A, range(1, 301). Do not restate this argument
        // against a structure no PDB fixture here builds — the version before
        // this one used 3S7G and quoted 830/415 for the same pair, which is
        // wrong on the repo's own 3S7G stand-in (_fc_pdb in
        // tests/test_pdb_preflight.py, four chains, 830 aa): there "A B" is
        // 415 and "A" is 208.
        //
        // When the envelope reports it sized a SELECTION, name the selection
        // and print its count.
        const env = v.size_envelope;
        const sized = env && env.size_basis === "selection" && env.selection_label;
        html += `<div class="preflight-meta">
          Target: <code>${escapeHtml(v.source_label)}</code>,
          ${sized
            ? `region <code>${escapeHtml(env.selection_label)}</code>
               — ${env.residue_count} residues`
            : `chain ${escapeHtml(v.target_chain || "")}
               — ${v.residues_kept_on_target_chain} residues`}
        </div>`;
      }
      // The envelope itself, mirroring the server-rendered twin in
      // templates/components/preflight_panel.html so the two panels cannot
      // describe the same verdict differently. The cap belongs on screen next
      // to the count: a bare residue number is not interpretable, and this is
      // the branch where the user is deciding whether to spend money.
      if (v.size_envelope) {
        html += `<div class="preflight-meta${
          v.size_envelope.over_soft_warn ? " preflight-meta--warn" : ""
        }">
          Size envelope: ${v.size_envelope.residue_count} aa target
          (cap ${v.size_envelope.hard_cap_target_aa} aa on
          <code>${escapeHtml(v.size_envelope.gpu || "")}</code>).
        </div>`;
        if (v.size_envelope.warn_message) {
          html += `<p class="preflight-warn">${
            escapeHtml(v.size_envelope.warn_message)
          }</p>`;
        }
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
      // Mirror the server parser (tools/boltz2 _parse_binder_text): a record
      // is committed only once a ">" header has been seen, so any orphan
      // sequence content before the first header is dropped rather than
      // counted. Without this the live panel could over-count and falsely
      // block a complex the submit gate would accept.
      let cur = 0;
      let seen = false;
      for (const raw of lines) {
        const ln = raw.trim();
        if (!ln) continue;
        if (ln.charAt(0) === ">") {
          if (seen && cur > max) max = cur;
          cur = 0;
          seen = true;
        } else if (seen) {
          cur += ln.replace(/\s+/g, "").length;
        }
      }
      if (seen && cur > max) max = cur;
    } else {
      for (const raw of lines) {
        const ln = raw.trim().replace(/\s+/g, "");
        if (ln.length > max) max = ln.length;
      }
    }
    return max > 0 ? max : null;
  }

  function appendTargetFields(fd) {
    // ONE body for both entry points. The upload path and the AlphaFold path
    // used to append the target fields with two copies of the same three
    // lines, which is how a field can end up on one and not the other — and a
    // panel that sizes a different run than submit does is worse than no panel,
    // because it refuses runs the server would accept.
    if (chainInput) fd.append("target_chain", chainInput.value || "");
    if (hotspotInput)
      fd.append("hotspot_residues", hotspotInput.value || "");
    if (contigInput) fd.append("target_input", contigInput.value || "");
  }

  function appendBinderFields(fd) {
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
    appendTargetFields(fd);
    appendBinderFields(fd);
    postPreflight(fd);
  }

  function runPreflightFromAlphaFold(accession, reuseToken) {
    const fd = new FormData();
    fd.append("alphafold_accession", accession);
    appendTargetFields(fd);
    appendBinderFields(fd);
    return postPreflight(fd).then((v) => {
      // If the AF model passes preflight, stash the reuse token so the
      // form's submit endpoint fetches the same AF model on the server
      // side (no re-upload from the browser needed).
      if (v && v.ok) {
        reuseTokenInput.value = reuseToken;
        if (fileInput) {
          fileInput.value = "";
          // The programmatic clear fires no native change event; dispatch one
          // so listeners (wallet estimate refresh, the campaign auto-route
          // which keys off a fresh upload) re-evaluate the now-empty file.
          fileInput.dispatchEvent(new Event("change", { bubbles: true }));
        }
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
  // Re-run when chain / hotspots / contig / binder sequences change AND a file
  // is attached. The contig belongs here for a reason of its own: it is
  // normally typed AFTER the file is picked, so the first verdict is always the
  // whole-upload one. Without a re-run the user reads a refusal for a run they
  // have since narrowed, and the Run button stays greyed out at the value it
  // was disabled on.
  for (const inp of [chainInput, hotspotInput, contigInput, binderSeqInput]) {
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
