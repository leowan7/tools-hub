"""Pass 7 baseline snapshot — test user wallet state before live $20 topup."""
import os
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client
s = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
u = '03e51184-4d04-4acd-ab22-0cbd7fa08c77'
w = s.table('user_wallets').select('*').eq('user_id', u).single().execute().data
print('=== BASELINE (test user / leowan7@gmail.com) ===')
for k in ('balance_usd','wallet_frozen','stripe_customer_id','stripe_payment_method_id',
         'auto_reload_enabled','auto_reload_threshold_usd','auto_reload_amount_usd',
         'auto_reload_monthly_cap_usd','updated_at'):
    print(f'  {k}: {w[k]}')
print()
print('=== LATEST 5 LEDGER ROWS (pre-Pass 7) ===')
rows = s.table('wallet_transactions').select(
    'id,kind,amount_usd,balance_after_usd,created_at,stripe_event_id,stripe_payment_intent_id'
).eq('user_id', u).order('created_at', desc=True).limit(5).execute().data
for r in rows:
    evt = (r.get('stripe_event_id') or '-')[:30]
    pi = (r.get('stripe_payment_intent_id') or '-')[:30]
    print(f'  {r["created_at"][:19]} | {r["kind"]:<16} | amt={r["amount_usd"]:>+8} | after={r["balance_after_usd"]:>9} | evt={evt} pi={pi}')
print()
print('=== POST-PASS-7 EXPECTATIONS ===')
print('  - balance_usd ~= 109.9895 (89.9895 + 20)')
print('  - new ledger row: kind=topup, amount=+20')
print('  - stripe_session_id should start with cs_live_ (NOT cs_test_)')
print('  - stripe_payment_intent_id should start with pi_live_')
print('  - stripe_customer_id changes (live customer != sandbox cus_UWR)')
print('  - if save-card checked: stripe_payment_method_id changes (live PM != pm_1TXNq)')
