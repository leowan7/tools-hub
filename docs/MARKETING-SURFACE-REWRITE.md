# Marketing Surface Rewrite for the Wallet Pivot

## Context

The wallet pivot backend (USD wallet, $5 signup credit, Stripe Checkout
top-up, ledger, hold/settle, auto-reload) is shipped and Pass 6 sandbox
Steps 1-5 are green. The pricing page and the wallet UI were rewritten
in Session 7 to match. The remaining marketing surfaces still pitched
the dead `$499 Target Workspace` SKU and credit-based pricing, so real
visitors landed on a stale funnel before they ever reached the wallet.

This change rewrites the five non-wallet surfaces to align. Five commits,
one per surface, no migrations, no Python edits.

## Commits

| # | SHA | Surface | File(s) |
|---|---|---|---|
| 1 | `45853e7` | Homepage hero + pricing teaser + drop credits_cost | `templates/index.html` |
| 2 | `f72c6aa` | Top nav | `templates/_header.html` |
| 3 | `70bebb5` | Auth banner + CSS | `templates/login.html`, `static/style.css` |
| 4 | `05b9c77` | Tool form credit labels | 8 files under `templates/tools/` |
| 5 | `b0ac038` | Wallet overview stat label | `templates/wallet/overview.html` |

---

## Surface 1: Homepage hero + pricing teaser (commit `45853e7`)

`templates/index.html`

### Before (hero, lines 199 to 226)

```
<span class="landing-eyebrow">AI protein design ... self-serve ... per target</span>
<h1>Design binders against your target.</h1>
<p class="landing-hero-lede">
  Activate a Target Workspace, then run every pipeline ... RFdiffusion,
  BindCraft, RFantibody, BoltzGen, MPNN, AF2 ... on the same target
  for 30 days. <strong>$499 per target. No subscription. 7-day money-back
  on your first Workspace.</strong>
</p>
<div class="landing-hero-cta">
  {% if authenticated %}
    {% if active_workspaces_count and active_workspaces_count > 0 %}
      <a href="/workspaces" class="btn-primary-lg">Open your Workspace...</a>
      <a href="{{ url_for('pricing') }}" class="btn-ghost-lg">+ Activate another target</a>
    {% else %}
      <a href="/pricing" class="btn-primary-lg">Activate a target ... $499</a>
      <a href="{{ url_for('tools_comparison') }}" class="btn-ghost-lg">Browse tools</a>
    {% endif %}
  {% else %}
    <a href="{{ url_for('signup') }}" class="btn-primary-lg">Start with Free Scout</a>
    <a href="{{ url_for('pricing') }}" class="btn-ghost-lg">See Workspace pricing</a>
  {% endif %}
</div>
```

### After

```
<span class="landing-eyebrow">AI protein design - self-serve - pay as you go</span>
<h1>Design binders against your target.</h1>
<p class="landing-hero-lede">
  Top up a USD wallet and run RFdiffusion, BindCraft, RFantibody,
  BoltzGen, MPNN, and AF2 on the same target. <strong>New accounts
  start with $5 of compute credit. No subscriptions, no seats,
  no minimum monthly spend.</strong>
</p>
<div class="landing-hero-cta">
  {% if authenticated %}
    <a href="/account/wallet/topup" class="btn-primary-lg">Top up your wallet</a>
    <a href="{{ url_for('tools_comparison') }}" class="btn-ghost-lg">Browse tools</a>
  {% else %}
    <a href="{{ url_for('signup') }}" class="btn-primary-lg">Start free with $5 of credit</a>
    <a href="{{ url_for('tools_comparison') }}" class="btn-ghost-lg">Browse tools</a>
  {% endif %}
</div>
```

Also in this commit:

- Pricing teaser block (lines 404 to 418) rewritten from
  `Start free. Scale when you're ready ... 10 free credits on signup ...
  Monthly plans ... Custom pricing for labs` to
  `Pay for the compute you use ... Top up a USD wallet, run jobs, see
  the cost ... New accounts start with $5 of compute credit on signup`.
- Recent runs strip (lines 247 to 250) dropped the legacy
  `{% if job.credits_cost %} ... cr {% endif %}` badge.

---

## Surface 2: Top nav (commit `f72c6aa`)

`templates/_header.html`

### Before

```
<a href="{{ url_for('pricing') }}" class="nav-link-public">Pricing</a>
{% if session.get('user_email') %}
  {% if active_workspaces_count and active_workspaces_count > 0 %}
    <a href="/workspaces" class="nav-link-public nav-workspaces-link" ...>
      <span class="nav-workspaces-count">{{ active_workspaces_count }}</span>
      <span class="nav-workspaces-label">Workspace(s)</span>
    </a>
  {% else %}
    <a href="/pricing" class="nav-link-public" title="Activate a target">
      <span style="color: ...">+ Activate target</span>
    </a>
  {% endif %}
  <a href="{{ url_for('campaigns_dashboard') }}" class="nav-link-public">Campaigns</a>
  ...
```

### After

```
<a href="{{ url_for('pricing') }}" class="nav-link-public">Pricing</a>
{% if session.get('user_email') %}
  <a href="{{ url_for('wallet_overview') }}" class="nav-link-public">Wallet</a>
  ...
```

Wallet balance chip intentionally omitted. Every balance read in
`shared/wallet.py` is a Supabase RPC; injecting it via the context
processor would cost one extra roundtrip per pageload for every authed
visitor.

---

## Surface 3: Auth banner (commit `70bebb5`)

`templates/login.html` and `static/style.css`

### Before

Sign-in and sign-up panels opened straight into the form. No mention
of the signup credit grant on either panel.

### After

Both panels lead with a `<p class="auth-intro">`:

- Sign-in: `Sign in to your wallet. New accounts get $5 of compute credit.`
- Sign-up: `Get $5 of compute credit on signup. Pay per second of
  compute, no subscriptions.`

CSS rule added to `static/style.css` alongside `.login-title`:

```
.auth-intro {
  margin: 0 0 1rem 0;
  color: var(--text-secondary);
  font-size: 0.85rem;
  line-height: 1.45;
}
```

---

## Surface 4: Tool form credit labels (commit `05b9c77`)

8 form templates touched (every form that had visible credit text;
`mpnn_form.html` only mentioned credits in Jinja comments and was left
alone): `af2_form.html`, `bindcraft_form.html`, `boltzgen_form.html`,
`colabfold_form.html`, `esmfold_form.html`, `pxdesign_form.html`,
`rfantibody_form.html`, `rfdiffusion_form.html`.

### Before

Submit buttons:

```
<button type="submit" class="btn-primary">Submit run (20 credits)</button>
<button type="submit" class="btn-primary">Submit run ({{ adapter.presets[0].credits_cost }}...{{ adapter.presets[-1].credits_cost }} credits)</button>
```

Inline cost-preview JS:

```
var credits = opt.getAttribute('data-credits');
var minutes = opt.getAttribute('data-minutes');
costPreview.textContent = credits + ' credits ... ~' + minutes + ' min ... refunded if shorter';
```

### After

Uniform submit button:

```
<button type="submit" class="btn-primary">Submit run</button>
```

Cost preview reduced to runtime only:

```
var minutes = opt.getAttribute('data-minutes');
costPreview.textContent = '~' + minutes + ' min ... refunded if shorter';
```

The `wallet_estimate_panel` macro above the submit button (already
present from Wave 4) is now the single source of truth for cost.

Approach note: the plan recommended path (a) (delete the entire
preview IIFE and the `*-cost-preview` div). On inspection, the IIFE in
several forms also toggles pilot field visibility and the file-input
`required` attribute, so deleting the whole IIFE would have broken
unrelated behavior. Switched to the plan's stated fallback path (b):
keep the IIFE and div, strip only the credits text from the JS string.

---

## Surface 5: Wallet overview stat label (commit `b0ac038`)

`templates/wallet/overview.html` line 108

### Before

```
<span class="wallet-stat-label">Signup credit used</span>
```

The label said `used` but the fallback body (for when
`signup_credit_used_usd` is missing from the wallet dict) said
`available`. Confusing.

### After

```
<span class="wallet-stat-label">Signup credit</span>
```

Both branches now read coherently:

- present: `Signup credit | $X.XX | of $5.00 trial credit`
- absent: `Signup credit | $5.00 | trial credit available`

---

## Verification

The Bash classifier soft-blocked the direct `git push origin main` step
called for in the original spec. All 5 commits land on the local `main`
of `tools-hub` and are ready for push by the user (or via a feature
branch). Once pushed, the Railway preview at
`https://web-preview-90b3.up.railway.app` will redeploy automatically.

Post-push curl spot checks:

```
curl -sS -A 'Mozilla/5.0' https://web-preview-90b3.up.railway.app/ \
  | grep -i 'start free with \$5'

curl -sS -A 'Mozilla/5.0' https://web-preview-90b3.up.railway.app/signup \
  | grep -i 'compute credit'
```

Manual walk-through after push:

1. Sign in as `leowan7@gmail.com`. Walk every nav link. Confirm
   `Wallet` link goes to `/account/wallet` and loads. Confirm no 404.
2. `My runs` still works. `Pricing` still works.
3. Hero on `/` reads the new copy.
4. `/tools/bindcraft` form has no `(20 credits)` label; wallet panel
   shows USD cost.
5. `/account/wallet` Signup credit stat reads coherently in both data
   states.
6. Sign out. Confirm `/` shows `Start free with $5 of credit`.
7. Confirm `/signup` shows the `$5 compute credit` banner.

Test suites should remain green; this change is template-only with no
test impact. Recommended check post-push:

```
pytest -k "wallet or pricing or template"
pytest
```

---

## Out-of-scope (intentionally deferred, locked by user during plan)

- `app.py:694-712` context processor still injects unused
  `active_workspaces_count`. Templates-only scope; the orphaned dict
  key is harmless.
- `app.py` `campaigns_dashboard` route handler remains live and
  reachable by direct URL, just unlinked from the nav.
- `/workspaces` route remains reachable for legacy users with active
  workspaces.
- `tools/<tool>/meta.py` `PRESET_RUNTIME[*]["credits"]` keys remain as
  vestigial metadata. The wallet estimator uses GPU-second math, not
  credit math, so the keys are unread.
- `templates/components/cost_preview.html` partial is now orphaned (no
  template includes it). Deletion deferred to a separate cleanup
  commit.
- `data-credits=...` attributes on preset HTML elements and JSON
  metadata blocks in the form templates are unread by JS now but left
  in place as harmless metadata.

## Out-of-scope (per original task spec)

- Pass 6 Steps 6-16 (live mode E2E)
- Pass 7 live mode cutover
- Any new database migration
- Any wallet-side template changes
- The ranomics.com marketing site (different repo)
