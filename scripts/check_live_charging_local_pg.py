"""Drive migration 0045's live-run functions against the LOCAL supabase stack.

    supabase start
    supabase db reset --local      # applies every migration, 0045 included
    venv/Scripts/python.exe scripts/check_live_charging_local_pg.py

Reads the URL and keys from ``supabase status -o env`` and exits before any
call when the API host is not 127.0.0.1 or localhost. No call is mocked
except ``shared.wallet._post_settle_hooks``, which would reach Stripe and the
mailer; it is replaced with a recorder so the amounts it is handed can be
checked. Exits 1 when any check fails.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    RESULTS.append((bool(ok), name, str(detail)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if not ok else ""))


def local_env() -> dict:
    exe = shutil.which("supabase") or shutil.which("supabase.exe")
    if not exe:
        sys.exit("supabase CLI not found on PATH")
    out = subprocess.run(
        [exe, "status", "-o", "env"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    env = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', out, re.M))
    host = urlparse(env.get("API_URL", "")).hostname
    if host not in ("127.0.0.1", "localhost"):
        sys.exit(f"refusing to run: API_URL host is {host!r}, not a local stack")
    return env


ENV = local_env()
for name in list(os.environ):
    if name.startswith(("SUPABASE", "STRIPE", "RESEND", "SENDGRID", "SMTP", "POSTMARK")):
        del os.environ[name]
os.environ["SUPABASE_URL"] = ENV["API_URL"]
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = ENV["SERVICE_ROLE_KEY"]
os.environ["SUPABASE_ANON_KEY"] = ENV["ANON_KEY"]
sys.path.insert(0, str(ROOT))

from supabase import create_client  # noqa: E402

import shared.wallet as wallet  # noqa: E402

if urlparse(os.environ["SUPABASE_URL"]).hostname not in ("127.0.0.1", "localhost"):
    sys.exit("refusing to run: SUPABASE_URL changed during import")

HOOKS: list[tuple[str, Decimal, Decimal]] = []
wallet._post_settle_hooks = lambda user_id, w, cost: HOOKS.append(
    (user_id, D(str((w or {}).get("balance_usd"))), D(str(cost)))
)

CONTAINER = "supabase_db_" + tomllib.loads((ROOT / "supabase/config.toml").read_text())["project_id"]
SVC = create_client(ENV["API_URL"], ENV["SERVICE_ROLE_KEY"])
ANON = create_client(ENV["API_URL"], ENV["ANON_KEY"])


def psql(sql: str) -> str:
    return subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "postgres", "-tAc", sql],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def new_user(credit: str | None = "10", *, signup: str | None = None) -> str:
    email = f"live-charging-check-{uuid.uuid4().hex[:12]}@example.test"
    uid = psql(
        "INSERT INTO auth.users (id, instance_id, aud, role, email, created_at, updated_at) "
        "VALUES (gen_random_uuid(), '00000000-0000-0000-0000-000000000000', "
        f"'authenticated', 'authenticated', '{email}', now(), now()) RETURNING id"
    ).splitlines()[0]
    SVC.table("user_wallets").insert({"user_id": uid, "balance_usd": 0, "wallet_frozen": False}).execute()
    if signup:
        SVC.rpc("credit_wallet", {
            "p_user_id": uid, "p_amount_usd": signup, "p_kind": "signup_credit",
            "p_stripe_event_id": f"signup_credit:{uid}", "p_stripe_payment_intent_id": None,
        }).execute()
    if credit:
        top_up(uid, credit)
    return uid


def top_up(uid: str, amount: str) -> None:
    tag = uuid.uuid4().hex
    assert wallet.top_up_wallet(uid, D(amount), stripe_payment_intent_id=f"pi_{tag}", stripe_event_id=f"evt_{tag}")


def ledger(uid: str) -> list[dict]:
    return SVC.table("wallet_transactions").select("*").eq("user_id", uid).order("id").execute().data


def balance(uid: str) -> Decimal:
    return D(str(SVC.table("user_wallets").select("balance_usd").eq("user_id", uid).single().execute().data["balance_usd"]))


def invariant(uid: str, label: str) -> None:
    total = sum((D(str(r["amount_usd"])) for r in ledger(uid)), D("0"))
    bal = balance(uid)
    check(f"{label}: SUM(ledger) == balance_usd and balance >= 0", total == bal and bal >= 0, f"sum={total} balance={bal}")


def children(anchor: str) -> list[dict]:
    return SVC.table("wallet_transactions").select("*").eq("parent_tx_id", anchor).order("id").execute().data


def debit(anchor: str, uid: str, due: str) -> dict:
    return wallet.debit_live_run(anchor, uid, D(due), 60, "A10G")


def settle(anchor: str, final: str) -> dict:
    """settle_live_run's SQL with an exact final cost (the wrapper derives it from GPU seconds)."""
    return SVC.rpc("settle_live_run", {
        "p_hold_tx_id": anchor, "p_final_due_usd": final, "p_gpu_seconds": 60, "p_gpu_class": "A10G",
    }).execute().data


def rpc_error(name: str, args: dict, client=SVC) -> str:
    try:
        client.rpc(name, args).execute()
    except Exception as exc:  # noqa: BLE001
        return str(getattr(exc, "code", "") or exc)
    return ""


def scenario_lifecycle() -> None:
    uid = new_user("10")
    anchor = wallet.open_live_run(uid, "colabfold")
    row = SVC.table("wallet_transactions").select("*").eq("id", anchor).single().execute().data
    check("open: anchor is a $0 'hold' with notes 'live run' and balance_after 10",
          row["kind"] == "hold" and D(str(row["amount_usd"])) == 0 and row["notes"] == "live run"
          and D(str(row["balance_after_usd"])) == 10, row)
    r = debit(anchor, uid, "1.00")
    check("debit 1.00: takes 1.00", (r["debited"], r["taken"], r["short"], r["balance_after"]) == (1, 1, 0, 9), r)
    r = debit(anchor, uid, "1.00")
    check("repeated tick at the same due takes nothing", (r["debited"], r["taken"], r["balance_after"]) == (0, 1, 9), r)
    with ThreadPoolExecutor(8) as pool:
        outs = list(pool.map(lambda _: debit(anchor, uid, "2.50"), range(8)))
    debits = [c for c in children(anchor) if c["kind"] == "run_debit"]
    check("8 overlapping ticks at due 2.50 take 1.50 in total, in one row",
          sum(o["debited"] for o in outs) == D("1.5") and len(debits) == 2 and balance(uid) == D("7.5"),
          [str(o["debited"]) for o in outs])
    r = debit(anchor, uid, "0.50")
    check("a due below what was taken takes nothing and is not short", (r["debited"], r["short"]) == (0, 0), r)
    invariant(uid, "lifecycle mid-run")
    r = settle(anchor, "3.00")
    check("settle final 3.00 >= taken 2.50: charges the rest 0.50",
          D(str(r["charged"])) == D("0.5") and D(str(r["released"])) == 0 and D(str(r["absorbed"])) == 0
          and D(str(r["balance_after"])) == 7, r)
    closers = [c for c in children(anchor) if c["kind"] != "run_debit"]
    check("settle wrote one 'charge' noted 'rest of the final cost'",
          [(c["kind"], c["notes"]) for c in closers] == [("charge", "live run: rest of the final cost")], closers)
    r = settle(anchor, "3.00")
    check("second settle is a no-op (settled_before)", r["settled_before"] is True and len(children(anchor)) == 3, r)
    r = debit(anchor, uid, "9.00")
    check("debit after settle takes nothing and reports settled", r["settled"] is True and r["debited"] == 0, r)
    invariant(uid, "lifecycle settled")
    spend = wallet.job_spend_by_hold(uid, [anchor])[anchor]
    check("job_spend_by_hold: usd 3.00, taken 2.50, settled, held 0",
          (spend["usd"], spend["taken"], spend["settled"], spend["held"]) == (3, D("2.5"), True, 0), spend)
    view = SVC.table("wallet_30d_spend").select("*").eq("user_id", uid).single().execute().data
    net = wallet._net_spend_usd(uid, datetime(2000, 1, 1, tzinfo=timezone.utc))
    check("wallet_30d_spend and _net_spend_usd both count debits + charge = 3.00",
          D(str(view["spent_usd_30d"])) == 3 and net == 3 and view["charges_30d"] == 1, (view, net))
    hook_amounts = [h[2] for h in HOOKS if h[0] == uid]
    check("post-settle hooks saw each debit that took money (1.00, 1.50)",
          hook_amounts == [D("1.00"), D("1.50")], hook_amounts)


def scenario_release() -> None:
    uid = new_user("10")
    anchor = wallet.open_live_run(uid, "af2")
    debit(anchor, uid, "4.00")
    r = settle(anchor, "2.50")
    closers = [c for c in children(anchor) if c["kind"] != "run_debit"]
    check("settle final 2.50 < taken 4.00: releases 1.50 as one 'hold_release'",
          D(str(r["released"])) == D("1.5") and D(str(r["balance_after"])) == D("7.5")
          and [(c["kind"], D(str(c["amount_usd"]))) for c in closers] == [("hold_release", D("1.5"))], r)
    view = SVC.table("wallet_30d_spend").select("spent_usd_30d").eq("user_id", uid).single().execute().data
    check("30-day spend nets the release: 4.00 - 1.50 = 2.50", D(str(view["spent_usd_30d"])) == D("2.5"), view)
    invariant(uid, "release")


def scenario_exact() -> None:
    uid = new_user("10")
    anchor = wallet.open_live_run(uid, "bindcraft")
    debit(anchor, uid, "2.00")
    r = settle(anchor, "2.00")
    closers = [c for c in children(anchor) if c["kind"] != "run_debit"]
    check("settle final == taken writes a $0 'charge' noted 'debits already took the final cost'",
          D(str(r["charged"])) == 0 and [(c["kind"], D(str(c["amount_usd"])), c["notes"]) for c in closers]
          == [("charge", D("0"), "live run: debits already took the final cost")], closers)
    view = SVC.table("wallet_30d_spend").select("*").eq("user_id", uid).single().execute().data
    check("bindcraft_spent_usd_30d counts a bindcraft run_debit", D(str(view["bindcraft_spent_usd_30d"])) == 2, view)
    invariant(uid, "exact")


def scenario_stop_at_zero() -> None:
    uid = new_user("1")
    anchor = wallet.open_live_run(uid, "esmfold")
    debit(anchor, uid, "0.60")
    r = debit(anchor, uid, "1.50")
    check("debit past the balance takes what is left (0.40) and is short 0.50",
          (r["debited"], r["short"], r["balance_after"]) == (D("0.4"), D("0.5"), 0), r)
    r = debit(anchor, uid, "2.00")
    check("next tick at $0 takes nothing and is short 1.00", (r["debited"], r["short"]) == (0, 1), r)
    check("open_live_run refuses a $0 balance", wallet.open_live_run(uid, "esmfold") is None)
    invariant(uid, "stop at $0")
    r = settle(anchor, "2.00")
    absorbed = [c for c in children(anchor) if c["kind"] == "absorbed_variance"]
    check("settle past $0 charges 0 and writes a $0 'absorbed_variance' carrying 1.00",
          D(str(r["charged"])) == 0 and D(str(r["absorbed"])) == 1 and len(absorbed) == 1
          and D(str(absorbed[0]["amount_usd"])) == 0 and D(str(absorbed[0]["estimated_cost_usd"])) == 1, r)
    invariant(uid, "stop at $0 settled")


def scenario_two_runs() -> None:
    uid = new_user("3")
    a, b = wallet.open_live_run(uid, "boltz2"), wallet.open_live_run(uid, "boltz2")
    check("a second live run opens while the balance is above $0", a and b and a != b, (a, b))
    debit(a, uid, "2.00")
    r = debit(b, uid, "2.00")
    check("second run takes only the 1.00 left and is short 1.00", (r["debited"], r["short"]) == (1, 1), r)
    r = debit(a, uid, "2.50")
    check("first run is now short too", (r["debited"], r["short"]) == (0, D("0.5")), r)
    invariant(uid, "two runs")
    uid2 = new_user("3")
    c, d = wallet.open_live_run(uid2, "af2"), wallet.open_live_run(uid2, "af2")
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: debit(c if i % 2 else d, uid2, "2.00"), range(8)))
    taken = sum(-D(str(x["amount_usd"])) for x in ledger(uid2) if x["kind"] == "run_debit")
    check("two runs ticking concurrently never take more than the balance", taken == 3 and balance(uid2) == 0, taken)
    invariant(uid2, "two concurrent runs")


def scenario_topup_mid_run() -> None:
    uid = new_user("1")
    anchor = wallet.open_live_run(uid, "colabfold")
    debit(anchor, uid, "1.00")
    r = debit(anchor, uid, "1.50")
    check("at $0 the run is short 0.50", (r["debited"], r["short"]) == (0, D("0.5")), r)
    top_up(uid, "5")
    r = debit(anchor, uid, "1.50")
    check("after a top-up the next tick catches up the 0.50", (r["debited"], r["short"], r["balance_after"]) == (D("0.5"), 0, D("4.5")), r)
    spend = wallet.job_spend_by_hold(uid, [anchor])[anchor]
    check("job_spend_by_hold on an open run: not settled, usd = taken = 1.50",
          (spend["settled"], spend["usd"], spend["taken"]) == (False, D("1.5"), D("1.5")), spend)
    settle(anchor, "2.00")
    check("settle after the top-up leaves 4.00", balance(uid) == 4, balance(uid))
    invariant(uid, "top-up mid-run")


def scenario_refund() -> None:
    uid = new_user("10")
    anchor = wallet.open_live_run(uid, "af2")
    debit(anchor, uid, "3.00")
    r = wallet.settle_live_run(anchor, 600, "A10G", {}, failure_reason="infra", refund=True)
    closers = [c for c in children(anchor) if c["kind"] != "run_debit"]
    check("refund (final 0) returns all 3.00 in one 'hold_release' carrying the failure reason",
          r["final"] == 0 and r["released"] == 3 and balance(uid) == 10
          and [(c["kind"], c["failure_reason"]) for c in closers] == [("hold_release", "infra")], r)
    view = SVC.table("wallet_30d_spend").select("spent_usd_30d").eq("user_id", uid).single().execute().data
    check("a refunded run spends 0 in the 30-day view", D(str(view["spent_usd_30d"])) == 0, view)
    invariant(uid, "refund")
    uid2 = new_user("10")
    anchor2 = wallet.open_live_run(uid2, "colabfold")
    want = wallet.live_due_usd("colabfold", 600, "A10G", {})
    r = wallet.settle_live_run(anchor2, 600, "A10G", {})
    check("settle_live_run's final is live_due_usd for the run's tool and GPU time",
          r["final"] == want.quantize(D("0.0001")) and balance(uid2) == 10 - r["charged"], (r, want))
    invariant(uid2, "wrapper settle")


def scenario_signup_expiry() -> None:
    uid = new_user(None, signup="5")
    SVC.table("user_wallets").update({"signup_credit_expires_at": "2000-01-01T00:00:00Z"}).eq("user_id", uid).execute()
    anchor = wallet.open_live_run(uid, "esmfold")
    debit(anchor, uid, "1.00")
    check("Python expiry sees the live run as open", wallet.expire_signup_credit(uid) == "hold_open")
    last = ledger(uid)[-1]["id"]
    out = SVC.rpc("expire_signup_credit", {"p_user_id": uid, "p_amount_usd": "1", "p_seen_last_tx_id": last}).execute().data
    check("SQL expiry returns hold_open while the run has only debits", out == "hold_open", out)
    settle(anchor, "1.00")
    check("after settle the expiry runs", wallet.expire_signup_credit(uid) == "expired")
    rows = ledger(uid)
    expiry = [D(str(r["amount_usd"])) for r in rows if r["kind"] == "signup_credit_expiry"]
    check("it expires the 4.00 the run did not spend", expiry == [D("-4")] and balance(uid) == 0, expiry)
    invariant(uid, "signup expiry")


def scenario_guards() -> None:
    uid = new_user("10")
    anchor = wallet.open_live_run(uid, "af2")
    check("release_hold on an unused anchor succeeds", wallet.release_hold(anchor, "view_early_return"))
    closers = children(anchor)
    check("it writes one $0 'hold_release'", [(c["kind"], D(str(c["amount_usd"]))) for c in closers] == [("hold_release", 0)], closers)
    r = debit(anchor, uid, "1.00")
    check("a released anchor cannot be debited", r["settled"] is True and r["debited"] == 0 and balance(uid) == 10, r)
    used = wallet.open_live_run(uid, "af2")
    debit(used, uid, "1.00")
    wallet.release_hold(used, "x")
    check("release_hold on an anchor with debits writes nothing (live runs settle through settle_live_run)",
          [c["kind"] for c in children(used)] == ["run_debit"], children(used))
    settle(used, "0")
    check("unknown anchor: debit_live_run returns None", wallet.debit_live_run("999999999", uid, D("1"), 1, "A10G") is None)
    topup_id = next(r["id"] for r in ledger(uid) if r["kind"] == "topup")
    check("a non-anchor row is refused with 22023",
          rpc_error("debit_live_run", {"p_hold_tx_id": topup_id, "p_due_usd": "1", "p_gpu_seconds": 1, "p_gpu_class": None}) == "22023")
    hold = SVC.rpc("try_hold_for_job", {"p_user_id": uid, "p_amount_usd": 1, "p_tool_slug": "af2",
                                        "p_job_id": None, "p_hard_cap_usd": 50}).execute().data
    check("a cushioned (non-$0) hold is refused with 22023",
          rpc_error("debit_live_run", {"p_hold_tx_id": hold, "p_due_usd": "1", "p_gpu_seconds": 1, "p_gpu_class": None}) == "22023"
          and rpc_error("settle_live_run", {"p_hold_tx_id": hold, "p_final_due_usd": "1", "p_gpu_seconds": 1, "p_gpu_class": None}) == "22023")
    wallet.release_hold(str(hold), "x")
    live = wallet.open_live_run(uid, "af2")
    check("a negative due is refused with 22023",
          rpc_error("debit_live_run", {"p_hold_tx_id": live, "p_due_usd": "-1", "p_gpu_seconds": 1, "p_gpu_class": None}) == "22023")
    settle(live, "0")
    invariant(uid, "guards")
    SVC.table("user_wallets").update({"wallet_frozen": True}).eq("user_id", uid).execute()
    check("open_live_run refuses a frozen wallet", wallet.open_live_run(uid, "af2") is None)
    check("open_live_run refuses a user with no wallet", wallet.open_live_run(str(uuid.uuid4()), "af2") is None)
    check("anon cannot call open_live_run", rpc_error("open_live_run", {"p_user_id": uid, "p_tool_slug": "af2"}, ANON) != "")
    grants = psql(
        "SELECT string_agg(p.proname || ':' || r || '=' || has_function_privilege(r, p.oid, 'EXECUTE'), ' ' ORDER BY p.proname, r) "
        "FROM pg_proc p, unnest(ARRAY['anon','authenticated','service_role']) r "
        "WHERE p.pronamespace = 'public'::regnamespace "
        "AND p.proname IN ('open_live_run','debit_live_run','settle_live_run','expire_signup_credit')"
    )
    want = " ".join(
        f"{f}:{r}={'true' if r == 'service_role' else 'false'}"
        for f in sorted(["open_live_run", "debit_live_run", "settle_live_run", "expire_signup_credit"])
        for r in ("anon", "authenticated", "service_role")
    )
    check("EXECUTE is service_role only on all four functions", grants == want, grants)


def main() -> int:
    print(f"local stack: {ENV['API_URL']} (container {CONTAINER})")
    print("migrations:", psql("SELECT max(version) FROM supabase_migrations.schema_migrations"))
    for scenario in (scenario_lifecycle, scenario_release, scenario_exact, scenario_stop_at_zero,
                     scenario_two_runs, scenario_topup_mid_run, scenario_refund,
                     scenario_signup_expiry, scenario_guards):
        print(f"-- {scenario.__name__}")
        try:
            scenario()
        except Exception as exc:  # noqa: BLE001
            check(f"{scenario.__name__} raised", False, repr(exc))
    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
