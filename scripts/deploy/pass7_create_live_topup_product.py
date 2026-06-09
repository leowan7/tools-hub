"""Pass 7: create the wallet top up Product in the LIVE Stripe account.

billing/checkout.py:create_topup_session builds the Checkout Session
line item as price_data referencing a Stripe Product id. That id comes
from the STRIPE_WALLET_TOPUP_PRODUCT_ID env var. The product was created
in the Stripe SANDBOX during Pass 2 but never in LIVE mode, so on
production create_topup_session returns:

    "Wallet top up product is not configured.
     Set STRIPE_WALLET_TOPUP_PRODUCT_ID in the environment."

This script creates (or reuses) that product on whatever Stripe account
STRIPE_SECRET_KEY points at. Run it with the production env so it hits
the live account:

Dry run (shows what it would create, writes nothing):
    railway run --service web --environment production -- \
        venv/Scripts/python.exe scripts/deploy/pass7_create_live_topup_product.py

Apply:
    railway run --service web --environment production -- \
        venv/Scripts/python.exe scripts/deploy/pass7_create_live_topup_product.py --apply

Idempotent: a product is tagged metadata.tools_hub_role=wallet_topup.
A second run finds that tag and reuses the existing product instead of
creating a duplicate.

After --apply, set the printed prod_... id as STRIPE_WALLET_TOPUP_PRODUCT_ID
in the Railway production environment (the script prints the exact CLI
command). That env change triggers a redeploy; the top up button works
once it is live.
"""
import os
import sys

import stripe

ROLE_TAG = "wallet_topup"  # metadata marker for idempotent lookup
PRODUCT_NAME = "Ranomics Tools wallet top up"
PRODUCT_DESCRIPTION = "Prepaid USD balance for running tools on tools.ranomics.com."

apply_change = "--apply" in sys.argv

print("=" * 72)
print("PASS 7  /  create live Stripe wallet top up product")
print("MODE: " + ("APPLY" if apply_change else "DRY RUN"))
print("=" * 72)

# --- key mode --------------------------------------------------------------
key = os.environ.get("STRIPE_SECRET_KEY", "")
if not key:
    print("FAIL: no STRIPE_SECRET_KEY in env")
    sys.exit(1)
if not key.startswith("sk_live_"):
    print(f"FAIL: STRIPE_SECRET_KEY is not a live key (prefix {key[:8]}). "
          "Run this under the production env so it hits the live account.")
    sys.exit(1)
stripe.api_key = key
print()
print(f"stripe key:  sk_live_ (prefix {key[:11]})")

# --- account default tax code (best effort) --------------------------------
# create_topup_session sets automatic_tax.enabled=True. A product with no
# tax_code inherits the account default tax code; we read and set it
# explicitly so the product is unambiguous. Pass 3 configured account tax.
default_tax_code = None
try:
    settings = stripe.tax.Settings.retrieve()
    defaults = getattr(settings, "defaults", None) or {}
    if isinstance(defaults, dict):
        default_tax_code = defaults.get("tax_code")
    tax_status = getattr(settings, "status", None)
    print(f"tax status:  {tax_status}")
    print(f"tax code:    {default_tax_code or '(none set on account)'}")
    if tax_status != "active":
        print("  WARN: account tax status is not 'active'; automatic_tax on "
              "the Checkout Session may reject. Verify in the dashboard.")
except Exception as exc:  # AttributeError on old SDKs, API errors
    print(f"tax code:    (could not read tax settings: {exc})")
    print("  product will be created without an explicit tax_code; it then "
          "inherits whatever default the account has.")

# --- idempotent lookup -----------------------------------------------------
existing = None
try:
    for prod in stripe.Product.list(limit=100, active=True).auto_paging_iter():
        meta = prod.get("metadata") or {}
        if meta.get("tools_hub_role") == ROLE_TAG:
            existing = prod
            break
except Exception as exc:
    print(f"FAIL: stripe.Product.list raised: {exc}")
    sys.exit(1)

if existing is not None:
    print()
    print(f"A live wallet top up product already exists:")
    print(f"  id:    {existing.id}")
    print(f"  name:  {existing.get('name')}")
    print()
    print("Nothing to create. Set this id as STRIPE_WALLET_TOPUP_PRODUCT_ID:")
    print()
    print(f'  railway variables --service web --environment production \\')
    print(f'    --set "STRIPE_WALLET_TOPUP_PRODUCT_ID={existing.id}"')
    sys.exit(0)

# --- nothing exists: create it ---------------------------------------------
create_args = {
    "name": PRODUCT_NAME,
    "description": PRODUCT_DESCRIPTION,
    "metadata": {"tools_hub_role": ROLE_TAG},
}
if default_tax_code:
    create_args["tax_code"] = default_tax_code

print()
print("product to create:")
print(f"  name:        {create_args['name']}")
print(f"  description: {create_args['description']}")
print(f"  tax_code:    {create_args.get('tax_code', '(inherit account default)')}")
print(f"  metadata:    {create_args['metadata']}")

if not apply_change:
    print()
    print("DRY RUN - nothing created. Re-run with --apply to create it.")
    sys.exit(0)

# --- apply -----------------------------------------------------------------
print()
print("APPLYING ...")
try:
    product = stripe.Product.create(**create_args)
except Exception as exc:
    print(f"FAIL: stripe.Product.create raised: {exc}")
    sys.exit(1)

print(f"  created product id={product.id}  name={product.get('name')!r}")
print()
print("=" * 72)
print("NEXT: set this id in the Railway production environment.")
print("=" * 72)
print()
print(f'  railway variables --service web --environment production \\')
print(f'    --set "STRIPE_WALLET_TOPUP_PRODUCT_ID={product.id}"')
print()
print("That env change triggers a redeploy. Once it is live, the top up")
print("button at /account/wallet/topup will reach Stripe Checkout.")
