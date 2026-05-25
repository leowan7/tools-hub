// Refreshes the navbar wallet chip without a full page reload.
//
// Triggers:
//   1. window.focus  — picks up any topup that landed while the user was
//                       in the Stripe checkout tab.
//   2. ?topup=success in the URL — polls every 2s for up to 30s so the
//                       webhook credit lands while the user is still on
//                       the success page.
//
// Read-only; calls GET /api/wallet/balance. No mutation, no
// notifications — just keeps the chip honest.

(function () {
  "use strict";

  var chip = document.getElementById("nav-wallet-chip");
  if (!chip) return;

  var topupBtn = document.getElementById("nav-wallet-topup");
  var LOW_THRESHOLD = 5;

  function applyBalance(usd) {
    if (typeof usd !== "number" || !isFinite(usd)) return;
    var prev = parseFloat(chip.getAttribute("data-wallet-usd") || "0");
    chip.setAttribute("data-wallet-usd", String(usd));
    chip.textContent = " $" + usd.toFixed(2) + " ";
    var low = usd < LOW_THRESHOLD;
    chip.classList.toggle("nav-wallet-low", low);
    if (topupBtn) {
      topupBtn.style.display = low ? "" : "none";
    }
    return usd !== prev;
  }

  function refreshOnce() {
    return fetch("/api/wallet/balance", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (r) {
        if (!r.ok) throw new Error("balance fetch failed: " + r.status);
        return r.json();
      })
      .then(function (data) {
        var usd = parseFloat(data && data.balance_usd);
        return applyBalance(usd);
      })
      .catch(function () { /* swallow — best effort */ });
  }

  // Window focus → opportunistic refresh.
  window.addEventListener("focus", refreshOnce);

  // ?topup=success → poll for up to 30s while the webhook flies.
  try {
    var params = new URLSearchParams(window.location.search);
    if (params.get("topup") === "success") {
      var elapsed = 0;
      var poll = setInterval(function () {
        refreshOnce().then(function (changed) {
          if (changed) clearInterval(poll);
        });
        elapsed += 2000;
        if (elapsed >= 30000) clearInterval(poll);
      }, 2000);
    }
  } catch (e) { /* URLSearchParams unsupported — skip polling */ }
})();
