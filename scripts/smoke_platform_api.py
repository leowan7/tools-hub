"""End-to-end smoke test for the Ranomics Platform REST API.

Usage:
    RK_LIVE_KEY=rk_live_... python scripts/smoke_platform_api.py
    RK_LIVE_KEY=rk_live_... PLATFORM_API_BASE_URL=https://tools.ranomics.com/api/v1 python scripts/smoke_platform_api.py

Exercises the live submit -> persist -> read -> withdraw loop without
going through the MCP transport. Covers the gap between
tests/test_platform_api.py (unit, mocked Supabase) and a manual one-off
curl session.

The final step withdraws (DELETE /experiments/{id}) the experiment it
created, so a passing run leaves no row behind. If the withdraw step
fails, the summary prints the leftover experiment_id and the SQL to drop
it by hand.

Exit code: 0 on all-pass, 1 on any failure (suitable for CI).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://tools.ranomics.com/api/v1"
HTTP_TIMEOUT_S = 30


def _env_or_die(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.stderr.write(
            f"ERROR: env var {name} is required. "
            f"Mint a member-role key at https://tools.ranomics.com/account/api-keys "
            f"and re-run with {name}=rk_live_... python scripts/smoke_platform_api.py\n"
        )
        sys.exit(2)
    return value


BASE_URL = os.environ.get("PLATFORM_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
RK_LIVE_KEY = _env_or_die("RK_LIVE_KEY")


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: Any  # parsed JSON or raw text


def _http(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> HttpResponse:
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {RK_LIVE_KEY}",
        "Accept": "application/json",
    }
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            parsed: Any
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = raw
            return HttpResponse(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=parsed,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp is not None else ""
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = raw
        return HttpResponse(
            status=exc.code,
            headers={k.lower(): v for k, v in exc.headers.items()},
            body=parsed,
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        # Connection refused / DNS / TLS / read timeout. Return a sentinel
        # non-2xx so the calling step fails gracefully and main() still
        # reaches the summary (and reports any leftover row) instead of dying
        # with an unhandled traceback that would leak the row unreported.
        return HttpResponse(status=0, headers={}, body=f"network error: {exc}")


# ---------------------------------------------------------------------------
# Test steps
# ---------------------------------------------------------------------------


@dataclass
class Step:
    name: str
    passed: bool
    elapsed_ms: int
    note: str = ""


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


CANONICAL_PAYLOAD: dict[str, Any] = {
    "name": f"smoke-test-{_now_iso_utc()}",
    "experiment_spec": {
        "experiment_type": "yeast_display",
        "target": {"custom": {"name": "Smoke Test Antigen"}},
        "library_design": {"mode": "designed_panel", "diversity_estimate": 100},
        "sequences": {
            "seq001": "MASRYLLNPHWGV",
            "seq002": "MALRSNPQRVWY",
        },
    },
}


def step_get_targets() -> tuple[Step, dict[str, Any] | None]:
    t0 = time.perf_counter()
    resp = _http("GET", "/targets")
    elapsed = int((time.perf_counter() - t0) * 1000)
    if resp.status != 200:
        return Step("GET /targets", False, elapsed, f"HTTP {resp.status}: {resp.body!r}"), None
    if not isinstance(resp.body, dict) or "targets" not in resp.body or "total" not in resp.body:
        return Step("GET /targets", False, elapsed, f"shape mismatch: {resp.body!r}"), None
    note = f"total={resp.body['total']}"
    return Step("GET /targets", True, elapsed, note), resp.body


def step_cost_estimate_custom() -> Step:
    t0 = time.perf_counter()
    resp = _http(
        "POST",
        "/experiments/cost-estimate",
        body={
            "experiment_type": "yeast_display",
            "candidate_count": 100,
            "library_diversity": 100,
            "target_kind": "custom",
        },
    )
    elapsed = int((time.perf_counter() - t0) * 1000)
    if resp.status != 200:
        return Step("POST /experiments/cost-estimate (custom)", False, elapsed, f"HTTP {resp.status}: {resp.body!r}")
    body = resp.body
    if not isinstance(body, dict) or body.get("requires_human_quote") is not True:
        return Step("POST /experiments/cost-estimate (custom)", False, elapsed, f"unexpected: {body!r}")
    if "estimated_range_usd" not in body:
        return Step("POST /experiments/cost-estimate (custom)", False, elapsed, "missing estimated_range_usd")
    return Step("POST /experiments/cost-estimate (custom)", True, elapsed, f"range={body['estimated_range_usd']}")


def step_submit_create(idem_key: str) -> tuple[Step, dict[str, Any] | None]:
    t0 = time.perf_counter()
    resp = _http(
        "POST",
        "/experiments",
        body=CANONICAL_PAYLOAD,
        extra_headers={"Idempotency-Key": idem_key},
    )
    elapsed = int((time.perf_counter() - t0) * 1000)
    if resp.status != 201:
        return (
            Step("POST /experiments (create)", False, elapsed, f"expected 201, got {resp.status}: {resp.body!r}"),
            None,
        )
    body = resp.body
    if not isinstance(body, dict):
        return Step("POST /experiments (create)", False, elapsed, f"non-dict body: {body!r}"), None
    experiment_id = body.get("experiment_id")
    if not experiment_id:
        return Step("POST /experiments (create)", False, elapsed, "missing experiment_id"), None
    if body.get("status") != "WaitingForConfirmation":
        return (
            Step(
                "POST /experiments (create)",
                False,
                elapsed,
                f"expected status WaitingForConfirmation, got {body.get('status')!r}",
            ),
            None,
        )
    status_log = body.get("status_log")
    if not isinstance(status_log, list) or len(status_log) < 2:
        return (
            Step(
                "POST /experiments (create)",
                False,
                elapsed,
                f"expected status_log with >=2 entries, got {status_log!r}",
            ),
            None,
        )
    statuses_seen = [entry.get("status") for entry in status_log if isinstance(entry, dict)]
    if "Draft" not in statuses_seen or "WaitingForConfirmation" not in statuses_seen:
        return (
            Step(
                "POST /experiments (create)",
                False,
                elapsed,
                f"status_log missing Draft+WaitingForConfirmation transitions: {statuses_seen!r}",
            ),
            None,
        )
    note = f"experiment_id={experiment_id} status_log={statuses_seen}"
    return Step("POST /experiments (create)", True, elapsed, note), body


def step_submit_replay(idem_key: str, original_experiment_id: str) -> Step:
    t0 = time.perf_counter()
    resp = _http(
        "POST",
        "/experiments",
        body=CANONICAL_PAYLOAD,
        extra_headers={"Idempotency-Key": idem_key},
    )
    elapsed = int((time.perf_counter() - t0) * 1000)
    if resp.status != 200:
        return Step(
            "POST /experiments (idempotent replay)",
            False,
            elapsed,
            f"expected 200 on replay, got {resp.status}: {resp.body!r}",
        )
    if resp.headers.get("idempotent-replay", "").lower() != "true":
        return Step(
            "POST /experiments (idempotent replay)",
            False,
            elapsed,
            f"missing Idempotent-Replay: true header, got headers={resp.headers!r}",
        )
    body = resp.body
    if not isinstance(body, dict):
        return Step("POST /experiments (idempotent replay)", False, elapsed, f"non-dict body: {body!r}")
    if body.get("experiment_id") != original_experiment_id:
        return Step(
            "POST /experiments (idempotent replay)",
            False,
            elapsed,
            f"replay returned different experiment_id: {body.get('experiment_id')} != {original_experiment_id}",
        )
    return Step("POST /experiments (idempotent replay)", True, elapsed, "same experiment_id, replay header present")


def step_get_experiment(experiment_id: str) -> Step:
    t0 = time.perf_counter()
    resp = _http("GET", f"/experiments/{experiment_id}")
    elapsed = int((time.perf_counter() - t0) * 1000)
    if resp.status != 200:
        return Step("GET /experiments/{id}", False, elapsed, f"HTTP {resp.status}: {resp.body!r}")
    body = resp.body
    if not isinstance(body, dict):
        return Step("GET /experiments/{id}", False, elapsed, f"non-dict body: {body!r}")
    if body.get("experiment_id") != experiment_id:
        return Step(
            "GET /experiments/{id}",
            False,
            elapsed,
            f"experiment_id mismatch: {body.get('experiment_id')} != {experiment_id}",
        )
    if body.get("status") != "WaitingForConfirmation":
        return Step(
            "GET /experiments/{id}",
            False,
            elapsed,
            f"status mismatch on read-back: {body.get('status')!r}",
        )
    if body.get("results_status") != "none":
        return Step(
            "GET /experiments/{id}",
            False,
            elapsed,
            f"results_status mismatch: {body.get('results_status')!r}",
        )
    return Step("GET /experiments/{id}", True, elapsed, f"status={body['status']} results_status={body['results_status']}")


def step_withdraw(experiment_id: str) -> Step:
    """Withdraw the experiment created above, then confirm it is gone.

    Exercises DELETE /experiments/{id} (must be in Draft /
    WaitingForConfirmation) and verifies the row is actually deleted by
    asserting a follow-up read 404s. This both tests the withdraw endpoint
    and leaves the smoke self-cleaning (no lab_campaigns row accrues)."""
    label = "DELETE /experiments/{id} (withdraw)"
    t0 = time.perf_counter()
    resp = _http("DELETE", f"/experiments/{experiment_id}")
    elapsed = int((time.perf_counter() - t0) * 1000)
    if resp.status != 200:
        return Step(label, False, elapsed, f"expected 200, got {resp.status}: {resp.body!r}")
    body = resp.body
    if not isinstance(body, dict) or body.get("experiment_id") != experiment_id:
        return Step(label, False, elapsed, f"unexpected body: {body!r}")
    if body.get("status") != "Withdrawn":
        return Step(label, False, elapsed, f"expected status Withdrawn, got {body.get('status')!r}")
    # Confirm the row is actually gone: a follow-up read must 404.
    check = _http("GET", f"/experiments/{experiment_id}")
    if check.status != 404:
        return Step(label, False, elapsed, f"row still readable after withdraw (GET returned {check.status})")
    return Step(label, True, elapsed, "withdrawn; read-back 404 (auto-cleanup OK)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print(f"smoke_platform_api.py against {BASE_URL}")
    print(f"  started_at: {_now_iso_utc()}\n")

    steps: list[Step] = []
    experiment_id: str | None = None
    cleaned = False

    targets_step, targets_body = step_get_targets()
    steps.append(targets_step)
    if not targets_step.passed:
        return _summarise(steps, experiment_id, cleaned=cleaned, started_clean=False)

    cost_step = step_cost_estimate_custom()
    steps.append(cost_step)

    idem_key = f"smoke-{_stamp_slug()}-{uuid.uuid4().hex[:8]}"
    create_step, create_body = step_submit_create(idem_key)
    steps.append(create_step)
    if create_step.passed and create_body is not None:
        experiment_id = create_body["experiment_id"]

    if experiment_id is not None:
        replay_step = step_submit_replay(idem_key, experiment_id)
        steps.append(replay_step)

        get_step = step_get_experiment(experiment_id)
        steps.append(get_step)

        # Always attempt cleanup of the row we just created, even if an
        # earlier step failed: the withdraw endpoint is itself under test,
        # and a passing withdraw leaves nothing to sweep by hand.
        withdraw_step = step_withdraw(experiment_id)
        steps.append(withdraw_step)
        cleaned = withdraw_step.passed

    return _summarise(steps, experiment_id, cleaned=cleaned, started_clean=True)


def _summarise(steps: list[Step], experiment_id: str | None, *, cleaned: bool, started_clean: bool) -> int:
    print("=" * 72)
    print("RESULTS")
    print("=" * 72)
    width = max((len(s.name) for s in steps), default=20)
    all_passed = True
    for s in steps:
        marker = "PASS" if s.passed else "FAIL"
        if not s.passed:
            all_passed = False
        print(f"  [{marker}] {s.name.ljust(width)}  {s.elapsed_ms:>5} ms   {s.note}")

    print()
    if experiment_id and cleaned:
        print(f"experiment_id {experiment_id} created and withdrawn; no leftover row.")
    elif experiment_id:
        # Withdraw did not run or did not pass — the row is still present.
        print(f"experiment_id created but NOT cleaned up: {experiment_id}")
        print("  -> review at https://tools.ranomics.com/admin/campaigns")
        print("  -> the withdraw step did not pass; drop the row by hand:")
        print(f"     DELETE FROM lab_campaigns WHERE id = '{experiment_id}';")
    elif not started_clean:
        print("(no experiment created; smoke aborted before submit)")
    else:
        print("(no experiment_id captured; submit step did not return one)")

    print()
    print("OVERALL:", "PASS" if all_passed else "FAIL")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
