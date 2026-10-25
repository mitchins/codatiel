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

# Per-request cache telemetry states (CACHE-01). Assigned by
# classify_cache() from provider-reported fields plus the backend's
# known caching capability — never inferred from totals.
CACHE_HIT = "CACHE_HIT"
CACHE_MISS = "CACHE_MISS"
CACHE_UNSUPPORTED = "CACHE_UNSUPPORTED"
CACHE_USAGE_UNAVAILABLE = "CACHE_USAGE_UNAVAILABLE"

# Per-request pricing states (STABILIZE-02 R1 Stage A). A request whose
# billed split cannot be established from provider-reported fields is
# never priced at an assumed tier: it is PRICING_UNAVAILABLE with a
# conservative bounded reservation, or an explicit unknown amount when
# no bound can be computed.
PRICED = "PRICED"
PRICING_UNAVAILABLE = "PRICING_UNAVAILABLE"


def classify_cache(entry: dict[str, Any] | None,
                   capable: bool | None) -> str:
    """Distinguish hit / miss / unsupported / unavailable for one entry.

    ``capable`` is the backend's known caching capability (True when the
    provider supports caching on this path, False when it cannot, None
    when unknown). Missing usage is always UNAVAILABLE, never a
    silent zero or an assumed miss.

    An explicit reported zero read is a measured MISS; an absent read
    is UNAVAILABLE even when writes are reported — a write proves cache
    activity, never a zero read.
    """
    if not entry or not entry.get("usage_known"):
        return CACHE_USAGE_UNAVAILABLE
    if capable is False:
        return CACHE_UNSUPPORTED
    read = entry.get("cached_input_tokens")
    if read is not None and read > 0:
        return CACHE_HIT
    if read is not None and read == 0:
        return CACHE_MISS
    return CACHE_USAGE_UNAVAILABLE


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
                               "usage_known": False,
                               "cached_input_tokens": None,
                               "cache_write_input_tokens": None,
                               "cache_in_input": True,
                               "reasoning_output_tokens": None}
            elif key not in merged and key not in self.entries:
                merged[key] = {
                    "response_id": key,
                    "requests": int(row.get("requests", 1) or 1),
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                    "usage_known": bool(row.get("usage_known", False)),
                    "cached_input_tokens": row.get("cached_input_tokens"),
                    "cache_write_input_tokens": row.get("cache_write_input_tokens"),
                    # Provider semantics must survive a crash: without this
                    # flag a restored Anthropic entry (input excludes cache)
                    # would be repriced with OpenAI semantics. Ancient rows
                    # predate the flag; True is the conservative upper bound.
                    "cache_in_input": row.get("cache_in_input", True),
                    "reasoning_output_tokens": row.get("reasoning_output_tokens"),
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

    def record_response(self, response_id: Any, usage: Any,
                        cached_input_tokens: Any = None,
                        cache_write_input_tokens: Any = None,
                        cache_in_input: Any = None,
                        reasoning_output_tokens: Any = None) -> bool:
        """Record one completed provider response.

        Returns True when this response added new usage, False when its ID was
        already reconciled. A missing response ID cannot be deduplicated, so it
        is kept as unknown rather than silently merged. Every new entry is
        appended to the sink immediately when one is attached.

        ``cached_input_tokens`` (cache reads) /
        ``cache_write_input_tokens`` / ``reasoning_output_tokens`` are
        recorded only when the caller passes provider-reported token details
        (backends whose adapter marks usage details as provider-sourced).
        They default to None (not reported), never to zero: an absent
        detail must not masquerade as a reported zero.

        ``cache_in_input`` records whether the provider's ``input_tokens``
        already include the cache sub-counts (True: OpenAI family —
        uncached slice = input − read − write; False: Anthropic —
        input_tokens counts only uncached tokens). Unknown defaults to
        True, the conservative upper bound (never undercounts).
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
                     "input_tokens": None, "output_tokens": None, "usage_known": False,
                     "cached_input_tokens": None, "cache_write_input_tokens": None,
                     "cache_in_input": True,
                     "reasoning_output_tokens": None}
        else:
            entry = {"response_id": key, "requests": max(1, requests),
                     "input_tokens": input_tokens, "output_tokens": output_tokens,
                     "usage_known": True,
                     "cached_input_tokens": (None if cached_input_tokens is None
                                             else _as_int(cached_input_tokens)),
                     "cache_write_input_tokens": (None if cache_write_input_tokens is None
                                                  else _as_int(cache_write_input_tokens)),
                     "cache_in_input": (True if cache_in_input is None
                                        else bool(cache_in_input)),
                     "reasoning_output_tokens": (
                         None if reasoning_output_tokens is None
                         else _as_int(reasoning_output_tokens))}
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
        entry["cached_input_tokens"] = None
        entry["cache_write_input_tokens"] = None
        entry["cache_in_input"] = True
        entry["reasoning_output_tokens"] = None
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

    def _rate_hit(self) -> float | None:
        try:
            hit = self.rate.get("input_cache_hit")
            return float(hit) if hit is not None else None
        except (TypeError, ValueError):
            return None

    def _rate_write(self, rate_in: float) -> float | None:
        """Cache-write rate, falling back to the flat input rate.

        A write priced at the input rate is the documented conservative
        upper bound when the provider's write premium is unpriced.
        (Legacy flat-rate path only. Tiered rate cards must carry an
        explicit write rate per tier; see _price_entry_tiered.)
        """
        try:
            write = self.rate.get("input_cache_write")
            return float(write) if write is not None else rate_in
        except (TypeError, ValueError):
            return rate_in

    # -- Tiered provider pricing (STABILIZE-02 R1 Stage A) ---------------
    # Active when ``rate`` carries a tiered card: ``pricing_backend`` in
    # {"luna", "haiku"}, an integer ``tier_threshold_tokens``, an explicit
    # ``processing_tier`` and a ``tiers`` mapping with per-tier input /
    # output / cache-hit / cache-write rates. Legacy flat ledgers (no
    # card) keep the exact historical behaviour below.

    def _tier_card(self) -> dict[str, Any] | None:
        rate = self.rate
        if not isinstance(rate, dict):
            return None
        tiers = rate.get("tiers")
        backend = rate.get("pricing_backend")
        if not isinstance(tiers, dict) or backend not in ("luna", "haiku"):
            return None
        try:
            threshold = int(rate.get("tier_threshold_tokens", -1))
        except (TypeError, ValueError):
            return None
        if threshold < 0 or rate.get("processing_tier") is None:
            return None
        return {"backend": backend, "threshold": threshold,
                "processing_tier": rate.get("processing_tier"),
                "tiers": tiers, "basis": rate.get("tier_threshold_basis"),
                "source": rate.get("rate_source")}

    @staticmethod
    def _tier_rates(tiers: dict[str, Any], name: str) -> dict[str, Any] | None:
        rates = tiers.get(name)
        return rates if isinstance(rates, dict) else None

    def _tier_for(self, entry: dict[str, Any],
                  card: dict[str, Any]) -> tuple[str, dict[str, Any] | None,
                                                 int | None]:
        """Select the context-size tier for one known-usage entry.

        Luna's threshold is on total input tokens as reported, which by
        provider semantics include cache reads and writes (verified live:
        ``input_tokens`` counts the whole prompt). Haiku's threshold is
        on the whole prompt — uncached input plus cache reads plus cache
        writes (per Anthropic's long-context pricing). A Haiku entry with
        an absent cache split has an unknowable prompt size and is placed
        in the higher tier: a missing classification never assumes cheap.
        """
        threshold = card["threshold"]
        if card["backend"] == "luna":
            total = entry["input_tokens"]
            name = "short" if total <= threshold else "long"
            return name, self._tier_rates(card["tiers"], name), total
        read = entry.get("cached_input_tokens")
        write = entry.get("cache_write_input_tokens")
        if read is None or write is None:
            name = "high"
            return name, self._tier_rates(card["tiers"], name), None
        total = entry["input_tokens"] + read + write
        name = "standard" if total <= threshold else "high"
        return name, self._tier_rates(card["tiers"], name), total

    @staticmethod
    def _upper_rate(tier_rates: dict[str, Any]) -> float | None:
        """Highest documented per-token rate in a tier: the conservative
        upper bound for a slice whose own category rate is unpriced."""
        best: float | None = None
        for key in ("input", "output", "input_cache_hit",
                    "input_cache_write"):
            try:
                value = tier_rates.get(key)
                number = float(value) if value is not None else None
            except (TypeError, ValueError):
                number = None
            if number is not None and (best is None or number > best):
                best = number
        return best

    def _price_entry_tiered(self, entry: dict[str, Any],
                            card: dict[str, Any]) -> dict[str, Any]:
        """Price one known-usage entry under tiered rules.

        Returns tier, pricing status, confirmed cost (PRICED only) and a
        conservative bounded reservation (PRICING_UNAVAILABLE only).
        Unknown or malformed usage never reaches here; absent splits and
        unpriced categories never produce confirmed spend.
        """
        tier_name, tier_rates, _total = self._tier_for(entry, card)
        priced = {"tier": tier_name, "pricing_status": PRICED,
                  "cost_usd": 0.0, "bounded_usd": None}
        unavailable = {"tier": tier_name,
                       "pricing_status": PRICING_UNAVAILABLE,
                       "cost_usd": None, "bounded_usd": None}
        if not tier_rates:
            return unavailable
        try:
            rate_in = float(tier_rates["input"])
            rate_out = float(tier_rates["output"])
        except (TypeError, ValueError, KeyError):
            return unavailable
        rate_hit = tier_rates.get("input_cache_hit")
        try:
            rate_hit = float(rate_hit) if rate_hit is not None else None
        except (TypeError, ValueError):
            return unavailable
        rate_write = tier_rates.get("input_cache_write")
        try:
            rate_write = float(rate_write) if rate_write is not None else None
        except (TypeError, ValueError):
            return unavailable
        read = entry.get("cached_input_tokens")
        write = entry.get("cache_write_input_tokens")
        output_tokens = entry["output_tokens"]
        if entry.get("cache_in_input", True):
            uncached = max(0, entry["input_tokens"]
                           - (read or 0) - (write or 0))
        else:
            # Anthropic semantics: input_tokens counts only uncached
            # tokens; cache reads and writes are separate billed lines.
            uncached = entry["input_tokens"]
        if read is None or write is None:
            # The billed split cannot be established: Luna hides unwritten
            # cache slices inside input_tokens; Anthropic bills unreported
            # cache lines separately. Price the known input at the highest
            # input-side rate of the tier as an explicit bounded reservation.
            upper_in = rate_in if rate_write is None else max(rate_in,
                                                              rate_write)
            base = entry["input_tokens"] if entry.get("cache_in_input",
                                                       True) else uncached
            unavailable["bounded_usd"] = (
                base / 1e6 * upper_in + output_tokens / 1e6 * rate_out)
            return unavailable
        if read > 0 and rate_hit is None:
            missing_category = True
        else:
            missing_category = write > 0 and rate_write is None
        if missing_category:
            # §A5: an unpriced category never inherits another rate.
            # Bound every slice at the tier's highest documented rate and
            # keep the request out of confirmed spend.
            upper = self._upper_rate(tier_rates)
            if upper is None:
                return unavailable
            unavailable["bounded_usd"] = (
                uncached / 1e6 * rate_in + read / 1e6 * upper
                + write / 1e6 * upper + output_tokens / 1e6 * rate_out)
            return unavailable
        priced["cost_usd"] = (
            uncached / 1e6 * rate_in
            + read / 1e6 * (rate_hit if rate_hit is not None else rate_in)
            + write / 1e6 * (rate_write if rate_write is not None else rate_in)
            + output_tokens / 1e6 * rate_out)
        return priced

    def confirmed_usd(self) -> float | None:
        """Confirmed lower-bound cost of known-usage requests, or None when
        rates are unconfirmed.

        Tiered rate cards price each request in its own context-size tier
        (see _tier_for). PRICING_UNAVAILABLE requests are excluded from the
        confirmed total and reported separately as a bounded reservation;
        see bounded_reserve_usd().

        When the rate provides ``input_cache_hit`` and an entry carries
        provider-reported ``cached_input_tokens``, that slice is priced at
        the hit rate and the remainder at the miss (``input``) rate.
        Cache writes price at ``input_cache_write`` (else the flat input
        rate). Otherwise the flat ``input`` rate applies — a conservative
        upper bound when caching is unpriced.
        """
        rate_in, rate_out = self._rate_pair()
        if rate_in is None or rate_out is None:
            return None
        card = self._tier_card()
        if card is not None:
            total = 0.0
            for entry in self.entries.values():
                if not entry["usage_known"]:
                    continue
                priced = self._price_entry_tiered(entry, card)
                if priced["pricing_status"] == PRICED:
                    total += priced["cost_usd"] or 0.0
            return total
        rate_hit = self._rate_hit()
        rate_write = self._rate_write(rate_in)
        total = 0.0
        for entry in self.entries.values():
            if not entry["usage_known"]:
                continue
            cached = entry.get("cached_input_tokens") or 0
            written = entry.get("cache_write_input_tokens") or 0
            if entry.get("cache_in_input", True):
                # OpenAI-family semantics: input_tokens is the whole prompt
                # INCLUDING cache sub-counts. The uncached slice excludes
                # both, otherwise writes would be priced twice.
                uncached = max(0, entry["input_tokens"] - cached - written)
            else:
                # Anthropic semantics: input_tokens counts only uncached
                # tokens; cache reads and writes are separate billed lines.
                uncached = entry["input_tokens"]
            if rate_hit is not None and entry.get("cached_input_tokens") is not None:
                total += (uncached / 1e6 * rate_in
                          + cached / 1e6 * rate_hit)
            else:
                total += entry["input_tokens"] / 1e6 * rate_in
            if written:
                total += written / 1e6 * rate_write
            total += entry["output_tokens"] / 1e6 * rate_out
        return total

    def bounded_reserve_usd(self) -> float | None:
        """Conservative bounded reservation for PRICING_UNAVAILABLE entries.

        Tiered cards only; legacy flat ledgers always return 0.0. None
        means a request cannot even be bounded (its tier carries no rates
        at all): the ceiling check then fails closed.
        """
        card = self._tier_card()
        if card is None:
            return 0.0
        total = 0.0
        for entry in self.entries.values():
            if not entry["usage_known"]:
                continue
            priced = self._price_entry_tiered(entry, card)
            if priced["pricing_status"] == PRICING_UNAVAILABLE:
                if priced["bounded_usd"] is None:
                    return None
                total += priced["bounded_usd"]
        return total

    def over_ceiling(self) -> bool:
        """True when confirmed spend plus bounded reservations exceed the
        per-response ceiling. Missing pricing cannot defeat the ceiling:
        unbounded requests fail closed. Legacy flat ledgers carry no
        bounds, so this reduces exactly to the historical confirmed-only
        check for them.
        """
        confirmed = self.confirmed_usd()
        bounded = self.bounded_reserve_usd()
        if bounded is None:
            return any(entry["usage_known"]
                       for entry in self.entries.values())
        return ((confirmed or 0.0) + bounded) > self.ceiling_usd

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
        detail_responses = sum(
            1 for entry in self.entries.values()
            if entry["usage_known"] and (
                entry.get("cached_input_tokens") is not None
                or entry.get("reasoning_output_tokens") is not None))
        cached_in = sum(entry.get("cached_input_tokens") or 0
                        for entry in self.entries.values()
                        if entry["usage_known"])
        cache_written = sum(entry.get("cache_write_input_tokens") or 0
                            for entry in self.entries.values()
                            if entry["usage_known"])
        reasoning_out = sum(entry.get("reasoning_output_tokens") or 0
                            for entry in self.entries.values()
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
        card = self._tier_card()
        unavailable_ids: list[str] = []
        bounded: float | None = 0.0
        ledger_rows: list[dict[str, Any]] = []
        for key in self.order:
            entry = self.entries[key]
            row = dict(entry)
            if card is not None and entry["usage_known"]:
                priced = self._price_entry_tiered(entry, card)
                row["tier"] = priced["tier"]
                row["pricing_status"] = priced["pricing_status"]
                row["priced_cost_usd"] = priced["cost_usd"]
                row["bounded_reserve_usd"] = priced["bounded_usd"]
                if priced["pricing_status"] == PRICING_UNAVAILABLE:
                    unavailable_ids.append(entry["response_id"])
                    if priced["bounded_usd"] is None:
                        bounded = None
                    elif bounded is not None:
                        bounded += priced["bounded_usd"]
            ledger_rows.append(row)
        if card is None:
            bounded = 0.0
        record = {"usage": {"requests": known_requests + unknown_requests,
                            "input_tokens": known_in, "output_tokens": known_out},
                   "known_requests": known_requests,
                   "usage_details_reported_responses": detail_responses,
                    "cached_input_tokens": cached_in,
                    "cache_write_input_tokens": cache_written,
                    "reasoning_output_tokens": reasoning_out,
                   "unknown_requests": unknown_requests,
                  "unknown_response_ids": unknown_ids,
                  "rate_status": self.rate_status,
                  "rate_confirmed": rate_confirmed,
                  "ceiling_usd": self.ceiling_usd,
                  "usd_estimate": usd,
                  "usd_is_confirmed_lower_bound": bool(unknown_requests or unavailable_ids),
                  "tokens_only": usd is None,
                  "recording_errors": self.recording_errors,
                  "sink_error": self.sink_error,
                  "request_ledger": ledger_rows}
        if card is not None:
            record["pricing_backend"] = card["backend"]
            record["processing_tier"] = card["processing_tier"]
            record["tier_threshold_tokens"] = card["threshold"]
            record["tier_threshold_basis"] = card["basis"]
            record["rate_source"] = card["source"]
            record["pricing_unavailable_ids"] = unavailable_ids
            record["pricing_unavailable_requests"] = sum(
                self.entries[key]["requests"] for key in self.order
                if self.entries[key]["response_id"] in unavailable_ids)
            record["bounded_reserve_usd"] = bounded
        else:
            record["pricing_unavailable_ids"] = []
            record["pricing_unavailable_requests"] = 0
            record["bounded_reserve_usd"] = 0.0
        record["exceeds_ceiling"] = self.over_ceiling()
        return record
