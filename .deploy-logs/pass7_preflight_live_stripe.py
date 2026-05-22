"""Pass 7 pre-flight: verify live Stripe account is ready.

Runs against whatever STRIPE_SECRET_KEY is in the env. Use:
    railway run python .deploy-logs/pass7_preflight_live_stripe.py

That injects the production env vars without leaving them on disk.

Checks:
  1. STRIPE_SECRET_KEY is a sk_live_ key (not accidentally sandbox)
  2. Stripe account activation (charges_enabled, payouts_enabled,
     details_submitted)
  3. Stripe Tax settings (enabled? automatic tax requires this)
  4. Webhook endpoints registered for the account, with URL +
     subscribed events, with a verdict on whether one points at
     tools.ranomics.com with the events the wallet code requires
"""
import os
import sys

import stripe

PROD_URL = "tools.ranomics.com"
REQUIRED_EVENTS = {
    "checkout.session.completed",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "charge.dispute.created",
}

key = os.environ.get("STRIPE_SECRET_KEY", "")
prod_whsec = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

print("=" * 72)
print("PASS 7 PRE-FLIGHT  /  live Stripe account audit")
print("=" * 72)

# --- 1. key mode -----------------------------------------------------------
print()
print("[1] STRIPE_SECRET_KEY mode")
if not key:
    print("  FAIL: no STRIPE_SECRET_KEY in env")
    sys.exit(1)
prefix = key[:8]
if key.startswith("sk_live_"):
    print(f"  PASS: key prefix is sk_live_ (full prefix: {prefix})")
elif key.startswith("sk_test_"):
    print(f"  FAIL: key prefix is sk_test_ — we are pointed at SANDBOX")
    sys.exit(1)
else:
    print(f"  FAIL: unrecognized key prefix: {prefix}")
    sys.exit(1)

stripe.api_key = key

# --- 2. account activation -------------------------------------------------
print()
print("[2] Stripe account activation")
try:
    acct = stripe.Account.retrieve()
except Exception as exc:
    print(f"  FAIL: stripe.Account.retrieve() raised: {exc}")
    sys.exit(1)
print(f"  account id:          {acct.id}")
print(f"  business_profile:    {acct.business_profile.get('name') if acct.business_profile else None}")
print(f"  charges_enabled:     {acct.charges_enabled}")
print(f"  payouts_enabled:     {acct.payouts_enabled}")
print(f"  details_submitted:   {acct.details_submitted}")
req = acct.get("requirements") if hasattr(acct, "get") else None
if req:
    currently_due = req.get("currently_due") or []
    past_due = req.get("past_due") or []
    disabled_reason = req.get("disabled_reason")
    print(f"  currently_due:       {currently_due if currently_due else '[]'}")
    print(f"  past_due:            {past_due if past_due else '[]'}")
    print(f"  disabled_reason:     {disabled_reason}")
else:
    print(f"  requirements:        n/a (standard account, no Connect requirements)")
if acct.charges_enabled and acct.payouts_enabled and acct.details_submitted:
    print("  PASS")
else:
    print("  FAIL: account is not fully activated")

# --- 3. Stripe Tax ---------------------------------------------------------
print()
print("[3] Stripe Tax settings")
try:
    # stripe-python exposes tax settings under stripe.tax.Settings
    settings = stripe.tax.Settings.retrieve()
    status = getattr(settings, "status", None)
    defaults = getattr(settings, "defaults", None) or {}
    print(f"  status:              {status}")
    if isinstance(defaults, dict):
        print(f"  tax_behavior:        {defaults.get('tax_behavior')}")
        print(f"  default_tax_code:    {defaults.get('tax_code')}")
    if status == "active":
        print("  PASS: tax is active")
    elif status == "pending":
        print("  WARN: tax status is 'pending' — automatic_tax may still work but verify in dashboard")
    else:
        print(f"  FAIL: tax status is '{status}'; create_topup_session sets automatic_tax.enabled=True so this will reject")
except AttributeError:
    print("  SKIP: stripe-python does not expose tax.Settings on this version")
except Exception as exc:
    print(f"  WARN: stripe.tax.Settings.retrieve() raised: {exc}")
    print("        (may mean Tax is not configured; check dashboard manually)")

# --- 4. webhook endpoints --------------------------------------------------
print()
print("[4] Webhook endpoints (live mode)")
try:
    endpoints = list(stripe.WebhookEndpoint.list(limit=20).auto_paging_iter())
except Exception as exc:
    print(f"  FAIL: stripe.WebhookEndpoint.list() raised: {exc}")
    sys.exit(1)

if not endpoints:
    print("  FAIL: no webhook endpoints configured on the live account")
    print("        Pass 7 would clear the charge but never credit the wallet.")
    sys.exit(1)

prod_endpoints = []
for ep in endpoints:
    url = ep.url or ""
    enabled = ep.status == "enabled"
    api_version = ep.api_version or "(default)"
    events = set(ep.enabled_events or [])
    matches_all = events == {"*"} or REQUIRED_EVENTS.issubset(events)
    missing = REQUIRED_EVENTS - events if events != {"*"} else set()
    is_prod = PROD_URL in url
    marker = "==>" if is_prod else "   "
    print(f"  {marker} id:                  {ep.id}")
    print(f"      url:                 {url}")
    print(f"      status:              {ep.status}")
    print(f"      api_version:         {api_version}")
    if events == {"*"}:
        print(f"      enabled_events:      ['*'] (all)")
    else:
        print(f"      enabled_events:      {sorted(events)}")
        if missing:
            print(f"      MISSING events:      {sorted(missing)}")
    if is_prod:
        prod_endpoints.append((ep, enabled, matches_all, missing))
    print()

if not prod_endpoints:
    print(f"  FAIL: no live webhook endpoint points at {PROD_URL}")
    print(f"        Pass 7 would clear the charge but the wallet would never be credited.")
    sys.exit(1)

print(f"  prod-endpoint count: {len(prod_endpoints)}")
ok = False
for ep, enabled, matches_all, missing in prod_endpoints:
    if not enabled:
        print(f"  FAIL: endpoint {ep.id} is disabled")
        continue
    if not matches_all:
        print(f"  FAIL: endpoint {ep.id} is missing required events: {sorted(missing)}")
        continue
    print(f"  PASS: endpoint {ep.id} is enabled and subscribed to all required events")
    ok = True

# --- 5. local whsec sanity (length only) -----------------------------------
print()
print("[5] STRIPE_WEBHOOK_SECRET sanity (does not validate against Stripe)")
if not prod_whsec:
    print("  WARN: no STRIPE_WEBHOOK_SECRET in env")
elif prod_whsec.startswith("whsec_") and len(prod_whsec) >= 30:
    print(f"  OK:   env has a whsec_ shaped value (len={len(prod_whsec)})")
    print("        NOTE: only a real webhook delivery can prove this matches the endpoint.")
    print("        If a delivery 400s with 'signature verification failed', rotate the secret in")
    print("        the dashboard and re-paste into Railway env.")
else:
    print(f"  WARN: env value is not whsec_-shaped (len={len(prod_whsec)})")

# --- summary ---------------------------------------------------------------
print()
print("=" * 72)
if ok:
    print("OVERALL: GREEN — safe to run the $20 live topup")
else:
    print("OVERALL: BLOCKED — fix the FAIL items above before running Pass 7")
print("=" * 72)
