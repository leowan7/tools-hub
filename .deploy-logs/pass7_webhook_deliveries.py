"""Inspect recent webhook deliveries to confirm signature verification works.

If the endpoint signing secret in Railway env matches the dashboard, every
delivery should land HTTP 2xx. Any 400/401 signature-mismatch is a smoking
gun that the env's whsec is stale.
"""
import os

import stripe

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
ENDPOINT_ID = "we_1TPPD4HK3YN42tFlJK8mQ6LS"

print(f"Recent events delivered to {ENDPOINT_ID}")
print("=" * 80)

# Stripe doesn't expose per-endpoint delivery history via API in a single
# call; we get it by listing recent events and checking endpoint targets.
# Simpler proxy: list recent checkout.session.completed events on this
# account and see how their pending_webhooks count looks. If
# pending_webhooks is 0 and the event is hours old, it delivered (success
# or perma-fail). If still pending after hours, something is wrong.
events = list(
    stripe.Event.list(
        limit=20,
        types=[
            "checkout.session.completed",
            "payment_intent.succeeded",
            "charge.dispute.created",
        ],
    ).auto_paging_iter()
)

if not events:
    print("  No recent qualifying events on the live account.")
    print("  This is fine if you have not run live transactions yet —")
    print("  it means no signature checks have been exercised either.")
else:
    print(f"  Found {len(events)} recent events.")
    print()
    for e in events[:10]:
        print(f"  id={e.id}  type={e.type}  created={e.created}  pending_webhooks={e.pending_webhooks}")
print()
print("Note: stripe.Event.list does not expose per-endpoint HTTP status.")
print("To prove signature verification works, send a test event from the")
print("dashboard: Developers > Webhooks > endpoint > 'Send test webhook'.")
print("A 200 there proves whsec_ matches between Stripe and Railway env.")
