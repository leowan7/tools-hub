"""Pass 7: add the 3 missing wallet events to the live Stripe webhook.

The live endpoint we_1TPPD4HK3YN42tFlJK8mQ6LS at
tools.ranomics.com/webhooks/stripe is subscribed only to
checkout.session.completed (of the wallet-required set). The wallet code
also needs payment_intent.succeeded, payment_intent.payment_failed and
charge.dispute.created.

Dry run (shows the planned change, modifies nothing):
    railway run --service web --environment production -- \
        python .deploy-logs/pass7_fix_webhook_events.py

Apply:
    railway run --service web --environment production -- \
        python .deploy-logs/pass7_fix_webhook_events.py --apply

Notes:
  - The full union of current + new events is sent, so no existing
    subscription (other products' invoice/subscription events) is dropped.
  - An enabled_events update does NOT rotate the signing secret, so
    STRIPE_WEBHOOK_SECRET on Railway stays valid.
  - api_version is fixed at endpoint creation; modify cannot change it.
"""
import os
import sys

import stripe

ENDPOINT_ID = "we_1TPPD4HK3YN42tFlJK8mQ6LS"
EXPECTED_URL_FRAGMENT = "tools.ranomics.com/webhooks/stripe"
NEW_EVENTS = {
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "charge.dispute.created",
}

apply_change = "--apply" in sys.argv

print("=" * 72)
print("PASS 7  /  add missing wallet events to live webhook endpoint")
print("MODE: " + ("APPLY" if apply_change else "DRY RUN"))
print("=" * 72)

key = os.environ.get("STRIPE_SECRET_KEY", "")
if not key:
    print("FAIL: no STRIPE_SECRET_KEY in env")
    sys.exit(1)
if not key.startswith("sk_live_"):
    print(f"FAIL: STRIPE_SECRET_KEY is not a live key (prefix {key[:8]})")
    sys.exit(1)
stripe.api_key = key

# --- retrieve current state ------------------------------------------------
try:
    ep = stripe.WebhookEndpoint.retrieve(ENDPOINT_ID)
except Exception as exc:
    print(f"FAIL: could not retrieve {ENDPOINT_ID}: {exc}")
    sys.exit(1)

url = ep.url or ""
print()
print(f"endpoint id:   {ep.id}")
print(f"url:           {url}")
print(f"status:        {ep.status}")
print(f"api_version:   {ep.api_version or '(default)'}")

if EXPECTED_URL_FRAGMENT not in url:
    print(f"FAIL: URL does not contain '{EXPECTED_URL_FRAGMENT}' "
          "— wrong endpoint, aborting")
    sys.exit(1)

current = set(ep.enabled_events or [])
if current == {"*"}:
    print()
    print("enabled_events is ['*'] (all events) — nothing to add. Done.")
    sys.exit(0)

print()
print(f"current enabled_events ({len(current)}):")
for e in sorted(current):
    print(f"  - {e}")

already = sorted(NEW_EVENTS & current)
missing = NEW_EVENTS - current
if already:
    print()
    print(f"wallet events already present: {already}")

if not missing:
    print()
    print("All 3 wallet events already subscribed — nothing to do.")
    sys.exit(0)

union = sorted(current | NEW_EVENTS)
print()
print(f"events to ADD ({len(missing)}):")
for e in sorted(missing):
    print(f"  + {e}")
print()
print(f"resulting enabled_events ({len(union)}):")
for e in union:
    print(f"  - {e}")

if not apply_change:
    print()
    print("DRY RUN — nothing modified. Re-run with --apply to write it.")
    sys.exit(0)

# --- apply -----------------------------------------------------------------
print()
print("APPLYING ...")
try:
    updated = stripe.WebhookEndpoint.modify(ENDPOINT_ID, enabled_events=union)
except Exception as exc:
    print(f"FAIL: WebhookEndpoint.modify raised: {exc}")
    sys.exit(1)

final = sorted(set(updated.enabled_events or []))
print(f"updated enabled_events ({len(final)}):")
for e in final:
    mark = "NEW" if e in missing else "   "
    print(f"  {mark} {e}")

still_missing = sorted(NEW_EVENTS - set(final))
print()
if still_missing:
    print(f"FAIL: still missing after modify: {still_missing}")
    sys.exit(1)
print("PASS: all 3 wallet events now subscribed.")
print(f"api_version still: {updated.api_version or '(default)'} "
      "(modify cannot change it)")
print("signing secret NOT rotated by an enabled_events update.")
