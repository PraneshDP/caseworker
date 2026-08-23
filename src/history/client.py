from __future__ import annotations
"""Client for the Calder County Resident History API.

Retrieving a resident's history, household composition and case events is
permitted outright by section 2.2 of Authority Policy ACA-2026/1, so this is a
plain read client with no gate in front of it.

Two design points that matter for the run:

FAILURE IS DATA, NOT AN EXCEPTION.
    A lookup that fails returns `ResidentHistory.unavailable(...)`, and the
    referral is still triaged -- with the triage note saying what the caseworker
    is missing and the risk signals carrying `data_incomplete`. The alternative,
    letting an exception escape, would make one unreachable record end the
    morning, which is the failure mode section 4.3 exists to prevent.

THE SNAPSHOT FALLBACK IS DECLARED, NOT SILENT.
    The API is a separate process the reviewer has to remember to start. If it is
    not running, the client reads `services/_history_data.json` -- the same file
    the service itself loads -- so a clean clone still produces a complete run.
    But `ResidentHistory.source` records `local_snapshot`, the digest prints a
    NOTE, and the ledger keeps the degraded flag. A reader can always tell which
    numbers came off the wire. Pass `allow_snapshot_fallback=False` to require the
    live service.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from src.domain.referral import ResidentHistory

DEFAULT_BASE_URL = "http://127.0.0.1:8083"
DEFAULT_SNAPSHOT = "services/_history_data.json"


@dataclass
class ClientStats:
    """Counters for the run summary, so degraded mode is visible not inferred."""

    api_calls: int = 0
    api_hits: int = 0
    not_found: int = 0
    transport_errors: int = 0
    snapshot_hits: int = 0
    retries: int = 0
    total_latency_ms: float = 0.0

    @property
    def degraded(self) -> bool:
        return self.snapshot_hits > 0 or self.transport_errors > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_calls": self.api_calls,
            "api_hits": self.api_hits,
            "not_found": self.not_found,
            "transport_errors": self.transport_errors,
            "snapshot_hits": self.snapshot_hits,
            "retries": self.retries,
            "total_latency_ms": round(self.total_latency_ms, 1),
            "degraded": self.degraded,
        }


class ResidentHistoryClient:
    """Reads resident history over HTTP, with a declared local fallback."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 5.0,
        retries: int = 2,
        backoff: float = 0.25,
        snapshot_path: str = DEFAULT_SNAPSHOT,
        allow_snapshot_fallback: bool = True,
        cache: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.snapshot_path = snapshot_path
        self.allow_snapshot_fallback = allow_snapshot_fallback
        self.stats = ClientStats()

        self._cache_enabled = cache
        self._cache: dict[str, ResidentHistory] = {}
        self._snapshot: Optional[dict[str, Any]] = None
        self._lock = threading.Lock()
        #: Set once the API has proved unreachable, so twelve referrals do not each
        #: pay the full retry budget for a service that is simply not running.
        self._api_down = False

    # -- health -------------------------------------------------------------

    @property
    def api_unreachable(self) -> bool:
        """True once the API has proved unreachable in this run.

        Public so a planning step can say up front that a lookup will fall back to
        the snapshot, rather than the degradation only becoming visible after the
        fact.
        """
        return self._api_down

    def health(self) -> dict[str, Any]:
        """Probe the service. Never raises; returns a report."""
        url = f"{self.base_url}/health"
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            elapsed = (time.perf_counter() - started) * 1000.0
            return {
                "reachable": True,
                "url": url,
                "latency_ms": round(elapsed, 1),
                "records": payload.get("records"),
                "status": payload.get("status"),
            }
        except Exception as exc:                       # noqa: BLE001 - reported
            return {
                "reachable": False,
                "url": url,
                "error": f"{type(exc).__name__}: {exc}",
                "fallback": (
                    self.snapshot_path if self.allow_snapshot_fallback else None
                ),
            }

    # -- the read -----------------------------------------------------------

    def fetch(self, resident_ref: str) -> ResidentHistory:
        """Retrieve one resident's history. Permitted by policy s.2.2."""
        ref = (resident_ref or "").strip()
        if not ref:
            return ResidentHistory.unavailable("", "no resident reference on the referral")

        if self._cache_enabled:
            with self._lock:
                cached = self._cache.get(ref)
            if cached is not None:
                return cached

        history = self._fetch_uncached(ref)

        if self._cache_enabled and history.available:
            with self._lock:
                self._cache[ref] = history
        return history

    def _fetch_uncached(self, ref: str) -> ResidentHistory:
        if not self._api_down:
            history = self._fetch_over_http(ref)
            if history is not None:
                return history

        if self.allow_snapshot_fallback:
            history = self._fetch_from_snapshot(ref)
            if history is not None:
                return history
            return ResidentHistory.unavailable(
                ref,
                f"not in the live API and not in the local snapshot "
                f"({self.snapshot_path})",
            )

        return ResidentHistory.unavailable(
            ref,
            f"Resident History API at {self.base_url} is unreachable and the local "
            f"snapshot fallback is disabled",
        )

    def _fetch_over_http(self, ref: str) -> Optional[ResidentHistory]:
        """Return a history, or None to mean 'try the fallback'.

        A 404 is authoritative -- the service knows its records -- so it returns an
        unavailable history rather than None. Only transport failures fall through.
        """
        url = f"{self.base_url}/residents/{urllib.parse.quote(ref)}"
        last_error = ""

        for attempt in range(self.retries + 1):
            started = time.perf_counter()
            try:
                self.stats.api_calls += 1
                with urllib.request.urlopen(url, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                elapsed = (time.perf_counter() - started) * 1000.0
                self.stats.total_latency_ms += elapsed

                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    return ResidentHistory.unavailable(
                        ref, f"Resident History API returned invalid JSON: {exc}"
                    )
                if not isinstance(payload, dict):
                    return ResidentHistory.unavailable(
                        ref,
                        f"Resident History API returned "
                        f"{type(payload).__name__}, expected an object",
                    )

                self.stats.api_hits += 1
                return ResidentHistory.from_api(
                    ref, payload, source="api", latency_ms=round(elapsed, 1)
                )

            except urllib.error.HTTPError as exc:
                elapsed = (time.perf_counter() - started) * 1000.0
                self.stats.total_latency_ms += elapsed
                if exc.code == 404:
                    self.stats.not_found += 1
                    return ResidentHistory.unavailable(
                        ref,
                        f"no record for {ref} in the Resident History API (HTTP 404)",
                    )
                if 500 <= exc.code < 600 and attempt < self.retries:
                    last_error = f"HTTP {exc.code}"
                    self.stats.retries += 1
                    time.sleep(self.backoff * (2 ** attempt))
                    continue
                return ResidentHistory.unavailable(
                    ref, f"Resident History API returned HTTP {exc.code}"
                )

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    self.stats.retries += 1
                    time.sleep(self.backoff * (2 ** attempt))
                    continue

        self.stats.transport_errors += 1
        # Remember the service is down so the remaining referrals skip the retries.
        self._api_down = True
        return None

    def _fetch_from_snapshot(self, ref: str) -> Optional[ResidentHistory]:
        snapshot = self._load_snapshot()
        if snapshot is None:
            return None
        record = snapshot.get(ref)
        if not isinstance(record, dict):
            return None
        self.stats.snapshot_hits += 1
        return ResidentHistory.from_api(ref, record, source="local_snapshot")

    def _load_snapshot(self) -> Optional[dict[str, Any]]:
        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            if not os.path.exists(self.snapshot_path):
                return None
            try:
                with open(self.snapshot_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                return None
            if not isinstance(data, dict):
                return None
            self._snapshot = data
            return self._snapshot

    # -- reporting ----------------------------------------------------------

    def source_note(self) -> str:
        """One line for the run summary describing where history came from."""
        s = self.stats
        if s.api_hits and not s.snapshot_hits:
            return (
                f"Resident history: {s.api_hits} record(s) from the live API at "
                f"{self.base_url}."
            )
        if s.snapshot_hits and not s.api_hits:
            return (
                f"Resident history: API at {self.base_url} unreachable; "
                f"{s.snapshot_hits} record(s) read from the local snapshot "
                f"{self.snapshot_path}. Start the service with "
                f"'python services/history_service.py' for a live run."
            )
        if s.snapshot_hits and s.api_hits:
            return (
                f"Resident history: {s.api_hits} from the live API, "
                f"{s.snapshot_hits} from the local snapshot after a transport "
                f"failure."
            )
        return "Resident history: no lookups performed."
