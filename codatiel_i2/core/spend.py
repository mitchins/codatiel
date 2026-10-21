"""Durable per-response spend accounting for canonical reviews.

The ledger records provider responses as they complete, keyed by response ID.
Reconciliation is idempotent: repeated handling of the same response never
debits it twice. Unknown usage is preserved as unknown; it is never replaced
by an estimate. Cost is a confirmed lower bound when any billed request has
unknown tokens.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIRMED_USAGE = "CONFIRMED_USAGE"
UNKNOWN_USAGE = "UNKNOWN_USAGE"
CONFIRMED_ZERO_USAGE = "CONFIRMED_ZERO_USAGE"


class SpendCeilingExceeded(RuntimeError):
    """Raised when confirmed per-response spend already exceeds the ceiling."""


def _as_int(value: Any) -> int | None:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


@dataclass
class SpendLedger:
    rate: dict[str, Any] = field(default_factory=dict)
    rate_status: str = "unconfirmed"
    ceiling_usd: float = 5.0
    zero_cost_authoritative: bool = False
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    sink_path: str = ""
    sink_error: str = ""
    recording_errors: int = 0

    def attach_sink(self, path: Path) -> None:
        """Persist every recorded response to `path` as it completes.

        The sink is append-only JSONL, flushed and fsynced per response, so a
        later process crash cannot lose responses recorded before it. Existing
        in-memory entries are written first; the sink then stays authoritative
        for crash recovery via `load_sink_entries`.
        """
        target = Path(path)
        try:
            with target.open("a", encoding="utf-8") as handle:
                for key in self.order:
                    handle.write(json.dumps(self.entries[key], sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            self.sink_error = f"{type(exc).__name__}: {exc}"
            return
        self.sink_path = str(target)
        self.sink_error = ""

    @staticmethod
    def load_sink_entries(path: Path) -> list[dict[str, Any]]:
        """Read back a spend sink for crash recovery (durable, idempotent)."""
        rows: list[dict[str, Any]] = []
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    if isinstance(row, dict) and row.get("response_id"):
                        rows.append(row)
        return rows

    def restore_sink(self, path: Path) -> None:
        """Merge sink entries without double-debiting already-known responses.

        Correction rows (usage_known_correction) refer to an earlier row with
        the same response_id; last row wins so a crash-restored ledger keeps
        the unknown marking instead of resurrecting confirmed zero usage.
        """
        merged: dict[str, dict[str, Any]] = {}
        for row in self.load_sink_entries(path):
            key = str(row.get("response_id", ""))
            if not key:
                continue
            if row.get("usage_known_correction"):
                previous = merged.get(key, self.entries.get(key))
                base_requests = int((previous or {}).get("requests", 1) or 1)
                merged[key] = {"response_id": key, "requests": base_requests,
                               "input_tokens": None, "output_tokens": None,
                               "usage_known": False}
            elif key not in merged and key not in self.entries:
                merged[key] = {
                    "response_id": key,
                    "requests": int(row.get("requests", 1) or 1),
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                    "usage_known": bool(row.get("usage_known", False)),
                }
        for key, entry in merged.items():
            if key not in self.entries:
                self.entries[key] = entry
                self.order.append(key)
            elif entry["usage_known"] is False:
                self.entries[key] = entry

    def _append_sink(self, entry: dict[str, Any]) -> None:
        if not self.sink_path or self.sink_error:
            return
        try:
            with open(self.sink_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            self.sink_error = f"{type(exc).__name__}: {exc}"

    def record_response(self, response_id: Any, usage: Any) -> bool:
        """Record one completed provider response.

        Returns True when this response added new usage, False when its ID was
        already reconciled. A missing response ID cannot be deduplicated, so it
        is kept as unknown rather than silently merged. Every new entry is
        appended to the sink immediately when one is attached.
        """
        key = str(response_id) if response_id not in (None, "") else None
        if key is not None and key in self.entries:
            return False
        if key is None:
            key = f"unidentified-response-{len(self.order) + 1}"
        requests = _as_int(getattr(usage, "requests", None)) if usage is not None else None
        input_tokens = _as_int(getattr(usage, "input_tokens", None)) if usage is not None else None
        output_tokens = _as_int(getattr(usage, "output_tokens", None)) if usage is not None else None
        if usage is None or requests is None or input_tokens is None or output_tokens is None:
            entry = {"response_id": key, "requests": 1 if usage is None else max(1, requests or 1),
                     "input_tokens": None, "output_tokens": None, "usage_known": False}
        else:
            entry = {"response_id": key, "requests": max(1, requests),
                     "input_tokens": input_tokens, "output_tokens": output_tokens,
                     "usage_known": True}
        self.entries[key] = entry
        self.order.append(key)
        self._append_sink(entry)
        return True

    def mark_unknown(self, response_id: Any) -> None:
        """Mark a recorded response's usage as unknown (e.g. the provider
        returned a response with no usable usage payload)."""
        key = str(response_id)
        entry = self.entries.get(key)
        if entry is None:
            return
        entry["input_tokens"] = None
        entry["output_tokens"] = None
        entry["usage_known"] = False
        self._append_sink({**entry, "usage_known_correction": True})

    def note_recording_error(self) -> None:
        self.recording_errors += 1

    def snapshot(self) -> dict[str, Any]:
        return {"rate": dict(self.rate), "rate_status": self.rate_status,
                "ceiling_usd": self.ceiling_usd,
                "zero_cost_authoritative": self.zero_cost_authoritative,
                "entries": [dict(self.entries[key]) for key in self.order]}

    def restore(self, snapshot: dict[str, Any]) -> None:
        restored = snapshot.get("entries", []) if isinstance(snapshot, dict) else []
        for entry in restored:
            key = str(entry.get("response_id", ""))
            if key and key not in self.entries:
                self.entries[key] = dict(entry)
                self.order.append(key)

    def _rate_pair(self) -> tuple[float | None, float | None]:
        try:
            rate_in = self.rate.get("input")
            rate_out = self.rate.get("output")
            if rate_in is None or rate_out is None:
                return None, None
            return float(rate_in), float(rate_out)
        except (TypeError, ValueError):
            return None, None

    def confirmed_usd(self) -> float | None:
        """Confirmed lower-bound cost of known-usage requests, or None when
        rates are unconfirmed."""
        rate_in, rate_out = self._rate_pair()
        if rate_in is None or rate_out is None:
            return None
        known_in = sum(entry["input_tokens"] for entry in self.entries.values()
                       if entry["usage_known"])
        known_out = sum(entry["output_tokens"] for entry in self.entries.values()
                        if entry["usage_known"])
        return (known_in / 1e6 * rate_in) + (known_out / 1e6 * rate_out)

    def has_confirmed_tokens(self) -> bool:
        return any(entry["usage_known"] and (entry["input_tokens"] or entry["output_tokens"])
                   for entry in self.entries.values())

    def to_record(self) -> dict[str, Any]:
        rate_in, rate_out = self._rate_pair()
        rate_confirmed = rate_in is not None and rate_out is not None
        known_in = sum(entry["input_tokens"] for entry in self.entries.values()
                       if entry["usage_known"])
        known_out = sum(entry["output_tokens"] for entry in self.entries.values()
                        if entry["usage_known"])
        known_requests = sum(entry["requests"] for entry in self.entries.values()
                             if entry["usage_known"])
        unknown_requests = sum(entry["requests"] for entry in self.entries.values()
                               if not entry["usage_known"])
        unknown_ids = [entry["response_id"] for entry in self.entries.values()
                       if not entry["usage_known"]]
        usd = self.confirmed_usd()
        if usd == 0.0 and not self.zero_cost_authoritative and (
                unknown_requests or not self.entries):
            # A $0.00 with no authoritative zero-cost basis and no confirmed
            # usage is not evidence of zero billed cost: report no estimate.
            usd = None
        record = {"usage": {"requests": known_requests + unknown_requests,
                            "input_tokens": known_in, "output_tokens": known_out},
                  "known_requests": known_requests,
                  "unknown_requests": unknown_requests,
                  "unknown_response_ids": unknown_ids,
                  "rate_status": self.rate_status,
                  "rate_confirmed": rate_confirmed,
                  "ceiling_usd": self.ceiling_usd,
                  "usd_estimate": usd,
                  "usd_is_confirmed_lower_bound": bool(unknown_requests),
                  "tokens_only": usd is None,
                  "recording_errors": self.recording_errors,
                  "sink_error": self.sink_error,
                  "request_ledger": [dict(self.entries[key]) for key in self.order]}
        record["exceeds_ceiling"] = usd is not None and usd > self.ceiling_usd
        return record
