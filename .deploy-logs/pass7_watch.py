"""Pass 7 live-watch — poll wallet + latest ledger rows every 3s.

Usage:
    venv/Scripts/python.exe .deploy-logs/pass7_watch.py

Stop with Ctrl+C. Use a separate terminal while you run the live
$20 topup on https://tools.ranomics.com.
"""
import os
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
from supabase import create_client

s = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
USER = '03e51184-4d04-4acd-ab22-0cbd7fa08c77'

# Baseline = first row of state on entry; we highlight deltas after that.
def fetch():
    w = s.table('user_wallets').select('*').eq('user_id', USER).single().execute().data
    rows = (
        s.table('wallet_transactions')
        .select('id,kind,amount_usd,balance_after_usd,created_at,stripe_event_id,stripe_payment_intent_id,parent_tx_id')
        .eq('user_id', USER)
        .order('created_at', desc=True)
        .limit(5)
        .execute()
        .data
    )
    return w, rows


def fmt_pm(pm):
    if not pm:
        return 'None'
    is_live = not pm.startswith('pm_1TXNq')  # sandbox seed PM
    return f'{pm[:18]}... [{"LIVE" if is_live else "sandbox"}]'


def fmt_cust(cust):
    if not cust:
        return 'None'
    is_live = cust != 'cus_UWR3IFRvQ2R2GW'
    return f'{cust} [{"LIVE" if is_live else "sandbox"}]'


prev_balance = None
prev_top_tx_id = None

print(f'Watching wallet user_id={USER}')
print('Stop with Ctrl+C.')
print()
while True:
    try:
        wallet, rows = fetch()
        bal = float(wallet['balance_usd'])
        cust = wallet['stripe_customer_id']
        pm = wallet['stripe_payment_method_id']
        frozen = wallet['wallet_frozen']
        top = rows[0] if rows else None

        deltas = []
        if prev_balance is not None and abs(bal - prev_balance) > 1e-6:
            deltas.append(f'balance {prev_balance:.4f} -> {bal:.4f} (delta {bal - prev_balance:+.4f})')
        if top and prev_top_tx_id is not None and top['id'] != prev_top_tx_id:
            deltas.append(f'new ledger row id={top["id"]} kind={top["kind"]} amount={top["amount_usd"]}')

        now = datetime.now(timezone.utc).strftime('%H:%M:%S')
        flag = ' [DELTA]' if deltas else ''
        print(f'[{now}]{flag} balance=${bal:.4f}  frozen={frozen}  cust={fmt_cust(cust)}  pm={fmt_pm(pm)}')
        if top:
            evt = (top.get('stripe_event_id') or '-')[:25]
            pi = (top.get('stripe_payment_intent_id') or '-')[:25]
            print(f'           last_tx: {top["created_at"][11:19]}  kind={top["kind"]:<14}  amt={top["amount_usd"]:>+8}  after={top["balance_after_usd"]:>9}  evt={evt} pi={pi}')
        for d in deltas:
            print(f'           >>> {d}')

        prev_balance = bal
        if top:
            prev_top_tx_id = top['id']

        time.sleep(3)
    except KeyboardInterrupt:
        print()
        print('Watch stopped.')
        break
    except Exception as e:
        print(f'  ERROR: {e}')
        time.sleep(5)
