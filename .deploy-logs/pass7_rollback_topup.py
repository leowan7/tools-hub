"""Pass 7: reverse a test topup credit after a Stripe refund.

tools-hub has no user-facing refund policy. The only refund is Leo's own
Pass 7 test topup. After refunding that payment in the Stripe dashboard,
run this to take the matching credit back out of the wallet so the
ledger reflects reality.

This script touches ONLY Supabase. Refund the payment in Stripe first.

Dry run (shows the row it would reverse, writes nothing):
    .\\venv\\Scripts\\python.exe .deploy-logs\\pass7_rollback_topup.py

Apply:
    .\\venv\\Scripts\\python.exe .deploy-logs\\pass7_rollback_topup.py --apply

Target a specific topup row instead of the latest:
    ... pass7_rollback_topup.py --topup-id 123 --apply

What it does: takes the most recent (or --topup-id) `topup` ledger row
for the test user, and if it is not already reversed, inserts a
compensating `adjustment` row of the negative amount (linked by
parent_tx_id) and rewrites user_wallets.balance_usd from the ledger sum.
The topup row stays as history; SUM(amount_usd) = balance_usd holds.
"""
import os
import sys
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()
from supabase import create_client

USER = "03e51184-4d04-4acd-ab22-0cbd7fa08c77"  # leowan7@gmail.com test user

# The Pass 6 sandbox topup. It is not a real refundable charge, so the
# script refuses to reverse it unless the caller names it via --topup-id.
SANDBOX_TOPUP_PI = "pi_3TXNqcHK3YN42tFl1i7ZwBV1"

apply_change = "--apply" in sys.argv
topup_id_arg = None
if "--topup-id" in sys.argv:
    try:
        topup_id_arg = int(sys.argv[sys.argv.index("--topup-id") + 1])
    except (IndexError, ValueError):
        print("FAIL: --topup-id needs an integer row id")
        sys.exit(1)

s = create_client(
    os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
)

print("=" * 72)
print("PASS 7  /  reverse test topup credit  (Supabase only, no Stripe)")
print("MODE: " + ("APPLY" if apply_change else "DRY RUN"))
print("=" * 72)

# --- pick the topup row to reverse -----------------------------------------
q = (
    s.table("wallet_transactions")
    .select("id,kind,amount_usd,created_at,stripe_payment_intent_id")
    .eq("user_id", USER)
    .eq("kind", "topup")
)
if topup_id_arg is not None:
    q = q.eq("id", topup_id_arg)
topups = q.order("created_at", desc=True).limit(1).execute().data

if not topups:
    where = f"id={topup_id_arg}" if topup_id_arg else "any"
    print(f"\nNo topup row found ({where}). Nothing to reverse.")
    sys.exit(0)

topup = topups[0]
topup_id = topup["id"]
topup_amount = Decimal(str(topup["amount_usd"]))
print()
print("topup row to reverse:")
print(f"  id:              {topup_id}")
print(f"  amount:          +${topup_amount}")
print(f"  created:         {topup['created_at'][:19]}")
print(f"  payment_intent:  {topup.get('stripe_payment_intent_id')}")

# --- guard: do not reverse the known sandbox topup by accident -------------
if topup.get("stripe_payment_intent_id") == SANDBOX_TOPUP_PI and topup_id_arg is None:
    print()
    print("REFUSING: the latest topup is the Pass 6 SANDBOX topup, not a")
    print("Pass 7 live charge. Do the live topup in the browser first, then")
    print("re-run. To reverse this row anyway, pass --topup-id explicitly.")
    sys.exit(1)

# --- already reversed? -----------------------------------------------------
prior = (
    s.table("wallet_transactions")
    .select("id,amount_usd,created_at")
    .eq("user_id", USER)
    .eq("kind", "adjustment")
    .eq("parent_tx_id", topup_id)
    .execute()
    .data
)
if prior:
    print()
    print(f"Already reversed by adjustment row id={prior[0]['id']} "
          f"(amount={prior[0]['amount_usd']}). Nothing to do.")
    sys.exit(0)

# --- authoritative balance from the ledger --------------------------------
all_rows = (
    s.table("wallet_transactions")
    .select("amount_usd")
    .eq("user_id", USER)
    .execute()
    .data
)
ledger_sum = sum((Decimal(str(r["amount_usd"])) for r in all_rows), Decimal("0"))
reversal = -topup_amount
new_balance = ledger_sum + reversal

print()
print(f"ledger balance now:  ${ledger_sum}")
print(f"reversal row:        ${reversal}")
print(f"balance after:       ${new_balance}")
if new_balance < 0:
    print("WARNING: this would push the balance negative.")

if not apply_change:
    print()
    print("DRY RUN - nothing written. Re-run with --apply to reverse it.")
    sys.exit(0)

# --- apply -----------------------------------------------------------------
pi = topup.get("stripe_payment_intent_id") or "unknown"
row = {
    "user_id": USER,
    "kind": "adjustment",
    "amount_usd": float(reversal),
    "balance_after_usd": float(new_balance),
    "parent_tx_id": topup_id,
    "notes": (
        f"Pass 7 test topup reversal after Stripe refund "
        f"(topup tx {topup_id}, PI {pi}). No user refund policy; test only."
    ),
}
print()
print("APPLYING ...")
inserted = s.table("wallet_transactions").insert(row).execute().data
adj_id = inserted[0]["id"] if inserted else "?"
print(f"  inserted adjustment row id={adj_id}  amount=${reversal}")
s.table("user_wallets").update(
    {"balance_usd": float(new_balance)}
).eq("user_id", USER).execute()
print(f"  user_wallets.balance_usd -> ${new_balance}")

# --- verify the invariant --------------------------------------------------
after_rows = (
    s.table("wallet_transactions")
    .select("amount_usd")
    .eq("user_id", USER)
    .execute()
    .data
)
after_sum = sum((Decimal(str(r["amount_usd"])) for r in after_rows), Decimal("0"))
wallet = (
    s.table("user_wallets")
    .select("balance_usd")
    .eq("user_id", USER)
    .single()
    .execute()
    .data
)
bal = Decimal(str(wallet["balance_usd"]))
print()
print(f"invariant: SUM(amount_usd)=${after_sum}  balance_usd=${bal}")
if after_sum == bal:
    print("PASS: ledger invariant holds; wallet is clean.")
else:
    print(f"FAIL: off by ${after_sum - bal}. Investigate before trusting the wallet.")
    sys.exit(1)
