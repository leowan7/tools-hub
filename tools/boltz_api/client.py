"""Minimal Boltz Lab API client for the glycoform pilot.

Two job types exposed:
- Structure & Binding Prediction (Boltz-2 cofold)
- Protein Design (BoltzGen)

Hard credit ceiling enforced per-instance. dry_run defaults True — the first run of any
script writes the JSON request body to disk without submitting.

API docs: https://api.boltz.bio/docs/
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import requests

BASE_URL = "https://api.boltz.bio/compute/v1"
DEFAULT_CREDIT_CEILING_JOBS = 60
DEFAULT_POLL_S = 15
DEFAULT_TIMEOUT_S = 60 * 60

PredictionKind = Literal["structure_and_binding", "protein_design", "library_screen"]


class BoltzAPIError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CreditLedger:
    """Counts every billable job. Aborts before the ceiling-th submission."""

    ceiling: int = DEFAULT_CREDIT_CEILING_JOBS
    spent_jobs: int = 0
    spent_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    entries: list[dict[str, Any]] = field(default_factory=list)

    def will_overrun(self, n: int = 1) -> bool:
        return self.spent_jobs + n > self.ceiling

    def charge(self, kind: str, est_usd: Decimal | None, prediction_id: str | None) -> None:
        if self.will_overrun(1):
            raise BudgetExceeded(
                f"Credit ceiling {self.ceiling} jobs would be exceeded; aborting before submission"
            )
        self.spent_jobs += 1
        if est_usd is not None:
            self.spent_usd += est_usd
        self.entries.append(
            {
                "kind": kind,
                "prediction_id": prediction_id,
                "est_usd": str(est_usd) if est_usd is not None else None,
                "spent_jobs_after": self.spent_jobs,
                "spent_usd_after": str(self.spent_usd),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )

    def dump(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "ceiling": self.ceiling,
                    "spent_jobs": self.spent_jobs,
                    "spent_usd": str(self.spent_usd),
                    "entries": self.entries,
                },
                indent=2,
            )
        )


@dataclass
class BoltzClient:
    api_key: str | None = None
    base_url: str = BASE_URL
    ledger: CreditLedger = field(default_factory=CreditLedger)
    dry_run: bool = True
    dry_run_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = os.environ.get("BOLTZ_API_KEY")
        if self.dry_run_dir is None:
            self.dry_run_dir = Path("./dry_run")
        self.dry_run_dir = Path(self.dry_run_dir)
        self.dry_run_dir.mkdir(parents=True, exist_ok=True)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise BoltzAPIError(
                "BOLTZ_API_KEY env var not set and no api_key passed to BoltzClient"
            )
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _endpoint(self, kind: PredictionKind, suffix: str = "") -> str:
        slug = {
            "structure_and_binding": "predictions/structure-and-binding",
            "protein_design": "protein/design",
            "library_screen": "protein/library-screen",
        }[kind]
        return f"{self.base_url}/{slug}{suffix}"

    def _dump_dry_run(self, kind: str, label: str, body: dict[str, Any]) -> Path:
        path = self.dry_run_dir / f"{label}.{kind}.json"
        path.write_text(json.dumps(body, indent=2))
        return path

    def estimate_cost(self, kind: PredictionKind, body: dict[str, Any]) -> Decimal | None:
        """Estimate cost without spending credits.

        Returns Decimal USD or None if the endpoint isn't usable (e.g. design has no
        documented estimate endpoint per the API docs).
        """
        if kind != "structure_and_binding":
            return None
        url = self._endpoint(kind, "/estimate-cost")
        resp = requests.post(url, headers=self._headers(), json=body, timeout=60)
        if resp.status_code >= 400:
            raise BoltzAPIError(
                f"estimate_cost {kind} HTTP {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        raw = data.get("estimated_cost_usd")
        return Decimal(raw) if raw is not None else None

    def submit(
        self,
        kind: PredictionKind,
        body: dict[str, Any],
        *,
        label: str,
        est_usd: Decimal | None = None,
    ) -> str | None:
        """Submit a prediction. Returns prediction id, or None when dry_run."""
        if self.dry_run:
            path = self._dump_dry_run(kind, label, body)
            print(f"[dry_run] {kind} body written to {path} (not submitted)")
            return None
        if self.ledger.will_overrun(1):
            raise BudgetExceeded(
                f"Refusing to submit: would exceed credit ceiling of {self.ledger.ceiling} jobs"
            )
        url = self._endpoint(kind)
        resp = requests.post(url, headers=self._headers(), json=body, timeout=120)
        if resp.status_code >= 400:
            raise BoltzAPIError(f"submit {kind} HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        pred_id = data.get("id")
        if not pred_id:
            raise BoltzAPIError(f"submit {kind} response missing id: {data}")
        self.ledger.charge(kind, est_usd, pred_id)
        return pred_id

    def retrieve(self, kind: PredictionKind, prediction_id: str) -> dict[str, Any]:
        if self.dry_run:
            raise BoltzAPIError("retrieve called in dry_run mode")
        url = self._endpoint(kind, f"/{prediction_id}")
        resp = requests.get(url, headers=self._headers(), timeout=60)
        if resp.status_code >= 400:
            raise BoltzAPIError(f"retrieve {kind} HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def wait_for(
        self,
        kind: PredictionKind,
        prediction_id: str,
        poll_s: int = DEFAULT_POLL_S,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            data = self.retrieve(kind, prediction_id)
            status = data.get("status") or ("completed" if data.get("completed_at") else "pending")
            if status in {"completed", "succeeded"}:
                return data
            if status in {"failed", "error"}:
                raise BoltzAPIError(f"{kind} {prediction_id} failed: {data.get('error')}")
            time.sleep(poll_s)
        raise BoltzAPIError(f"{kind} {prediction_id} did not complete within {timeout_s}s")
