"""Fail the synthetic-smoke job when Epitope Scout is refusing its own users.

Run from ``.github/workflows/synthetic-smoke.yml`` every 6h on a GitHub-hosted
runner: scrape ``https://tools.ranomics.com/metrics`` with the ``METRICS_TOKEN``
bearer, and exit non-zero when the anonymous refusal share of metered Scout
traffic goes over a threshold. A non-zero exit fails the job, which is what
sends GitHub's failure email to the repo owner.

WHY THIS EXISTS
    ``/metrics`` had no consumer at all — no Prometheus, no Grafana, no scrape
    config anywhere in the repo. So Phase 6's "alert on refusal rate"
    (``docs/HANDOFF-2026-08-18-anon-rate-limiting.md``) was a MISSING CONSUMER,
    not a missing threshold. The failure it names — "a limiter refusing 40% of
    real users is an outage that does not look like one" — is invisible to
    every monitor that exists: ``/health`` and ``/readyz`` stay green through
    it, and of the five ways Scout refuses an anonymous caller one answers HTTP
    200 ``text/event-stream`` and two answer 503, so an error-rate alarm on
    status codes cannot see it either.

WHAT THE RATIO IS, AND IS NOT
    Prometheus counters are monotonic and RESET WHEN THE CONTAINER RESTARTS, so
    every number here is "since container boot", NOT a windowed rate. A deploy
    ten minutes before the scrape leaves a handful of samples and a ratio that
    swings on one request; ``REFUSAL_RATE_MIN_SAMPLES`` is the floor that stops
    this firing on that. It cuts the other way too: a refusal spike that ended
    hours ago still shows, diluted, until the next deploy. What survives both
    is a SUSTAINED refusal rate, which is exactly the condition the plan
    describes.

Stdlib only, deliberately — the workflow installs nothing.

Run by hand::

    METRICS_TOKEN=... python scripts/check_refusal_rate.py
"""

from __future__ import annotations

import math
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_METRICS_URL = "https://tools.ranomics.com/metrics"
HTTP_TIMEOUT_S = 30

# The denominator: requests to the five routes an anonymous caller can be
# refused on. Named explicitly rather than matched as ``scout.*`` because the
# blueprint also serves page loads, downloads and the feasibility tool, and
# folding those in would dilute the ratio until the outage stopped showing.
# The list is Phase 0's ("metered routes are exactly five"). If a sixth metered
# route ever appears and is not added here, this check gets NOISIER, not
# quieter — the refusals still count, the requests do not.
METERED_ROUTES = (
    "scout.upload",
    "scout.fetch_pdb",
    "scout.example",
    "scout.analyze",
    "scout.progress",
)

# The two SSE outcomes that are NOT us saying no: a caller who omitted job_id,
# and a job the reaper cleared. scout/ratelimit.py labels them the same way and
# for the same reason. They are reported below but never alerted on — every
# OTHER reason counts against the threshold, including one added after this
# file was written, which is the fail-safe direction.
INFO_REASONS = ("bad_request", "job_expired")

# Display order for the reasons that do count. Not a filter — see above.
POLICY_REASONS = (
    "rate_limited",
    "session_rate_limited",
    "no_session",
    "busy",
    "at_capacity",
)

# Phase 0 measured what the outage looks like: six researchers behind one
# university NAT made 18 intake attempts and 8 were refused — 44%. Ordinary use
# is nowhere near that: a real first visit spends 1-3 of an allowance of 10, so
# the expected share is low single digits. 0.20 sits between them with room on
# both sides — half the measured outage, an order of magnitude above normal.
DEFAULT_THRESHOLD = 0.20

# Below this many metered requests the ratio is noise, because the counters
# reset on every deploy and this app deploys often. At 50 the threshold means
# 10 refusals; at 5 it would mean one, and one refused scanner would page
# somebody at 3am.
DEFAULT_MIN_SAMPLES = 50

# Label values in this app's metrics are route names and fixed reason codes —
# no quotes, no commas, no braces — so the cheap pattern is enough. A value
# containing an escaped quote would parse short; nothing here emits one.
_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>\S+)\s*$"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


def parse_exposition(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Parse Prometheus text exposition into ``(name, labels, value)`` triples.

    Comment/HELP/TYPE lines, blank lines, unparseable lines and non-finite
    values are dropped. Trailing exemplars and timestamps are not used by
    anything here.
    """
    out: list[tuple[str, dict[str, str], float]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        labels = dict(_LABEL_RE.findall(match.group("labels") or ""))
        out.append((match.group("name"), labels, value))
    return out


def totals(
    samples: list[tuple[str, dict[str, str], float]],
) -> tuple[float, dict[str, float]]:
    """Return ``(metered scout requests, {reason: refusals})``."""
    requests = 0.0
    refusals: dict[str, float] = {}
    for name, labels, value in samples:
        if name == "tools_hub_requests_total":
            if labels.get("route") in METERED_ROUTES:
                requests += value
        elif name == "tools_hub_scout_refusals_total":
            reason = labels.get("reason", "unknown")
            refusals[reason] = refusals.get(reason, 0.0) + value
    return requests, refusals


def evaluate(
    requests: float,
    refusals: dict[str, float],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_samples: float = DEFAULT_MIN_SAMPLES,
) -> tuple[int, list[str]]:
    """Return ``(exit_code, report_lines)`` for one scrape.

    Pure, so the threshold arithmetic and the minimum-denominator floor are
    testable without an HTTP round trip.
    """
    lines = [f"metered Scout requests since container boot: {requests:.0f}"]
    ordered = list(POLICY_REASONS) + list(INFO_REASONS)
    ordered += sorted(set(refusals) - set(ordered))
    for reason in ordered:
        count = refusals.get(reason, 0.0)
        share = count / requests if requests else 0.0
        note = "  (reported only)" if reason in INFO_REASONS else ""
        lines.append(f"  {reason:<22} {count:>8.0f}  {share:>6.1%}{note}")

    # Everything that is not one of the two non-refusals counts, so a reason
    # added later lands in the alert by default rather than silently outside it.
    refused = sum(v for r, v in refusals.items() if r not in INFO_REASONS)

    if requests < min_samples:
        lines.append(
            f"SKIP: only {requests:.0f} metered requests since the last deploy "
            f"(floor is {min_samples:.0f}). Counters reset on deploy, so this "
            f"ratio is not yet a rate."
        )
        return 0, lines

    share = refused / requests
    lines.append(
        f"refusal share: {share:.1%} ({refused:.0f}/{requests:.0f})  "
        f"threshold {threshold:.1%}"
    )
    if share > threshold:
        lines.append(
            "FAIL: Epitope Scout is refusing more anonymous traffic than the "
            "threshold allows. This is the outage that does not look like one "
            "— /health stays green through it. Check the per-reason split "
            "above: rate_limited means a whole network hit the shared ceiling, "
            "busy/at_capacity mean the box is under pressure, "
            "session_rate_limited alone is ordinary over-use."
        )
        return 1, lines
    lines.append("OK")
    return 0, lines


def fetch(url: str, token: str, timeout: float = HTTP_TIMEOUT_S) -> tuple[int, str]:
    """GET ``url`` with a bearer token. Returns ``(status, body)``."""
    request = urllib.request.Request(  # noqa: S310 — fixed https URL
        url, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"warning: {name}={raw!r} is not a number; using {default}")
        return default


def main(argv: list[str] | None = None) -> int:
    url = os.environ.get("METRICS_URL", "").strip() or DEFAULT_METRICS_URL
    token = os.environ.get("METRICS_TOKEN", "").strip()
    threshold = _float_env("REFUSAL_RATE_THRESHOLD", DEFAULT_THRESHOLD)
    min_samples = _float_env("REFUSAL_RATE_MIN_SAMPLES", DEFAULT_MIN_SAMPLES)

    if not token:
        print(
            "ERROR: METRICS_TOKEN is not set. Add it as a repository secret "
            "and set the same value on the Railway service."
        )
        return 2

    # A missing or forbidden /metrics FAILS this job rather than skipping it.
    # A 403 means the token is wrong or METRICS_TOKEN was dropped in Railway,
    # which silently disables the only refusal-rate alarm that exists — a
    # monitor that goes quiet when its own plumbing breaks is precisely the
    # failure Phase 6 was written to stop. The counter-argument is alert noise
    # from a transient edge 5xx, and it does not apply here: this job already
    # fails wholesale when Railway is unreachable, so this step adds no new
    # class of false alarm.
    try:
        status, body = fetch(url, token)
    except Exception as exc:  # URLError, socket timeout, TLS, ...
        print(f"ERROR: could not reach {url}: {exc!r}")
        return 1

    if status != 200:
        print(
            f"ERROR: {url} answered HTTP {status}. 403 means METRICS_TOKEN "
            f"does not match the value set on the Railway service; anything "
            f"else means /metrics is not being served."
        )
        return 1

    samples = parse_exposition(body)
    if not samples:
        # /metrics answering 200 with nothing in it has shipped here before
        # (PROMETHEUS_MULTIPROC_DIR set after --preload imported the app).
        print(
            f"ERROR: {url} answered 200 with no parseable samples "
            f"({len(body)} bytes). The exposition is empty or malformed."
        )
        return 1

    requests, refusals = totals(samples)
    code, lines = evaluate(
        requests, refusals, threshold=threshold, min_samples=min_samples
    )
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
