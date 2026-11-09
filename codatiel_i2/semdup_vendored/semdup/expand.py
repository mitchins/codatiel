"""Generic query-expansion lexicon: domain-neutral programming synonyms.

Hand-authored generic vocabulary (obtain/compute/create/check/convert/…).
NOT derived from any fixture or benchmark; tuning against the supplied
benchmark packet with this file is forbidden. Additions must be justified
as generic language, never as case-specific patches.
"""
from __future__ import annotations

# Each group: first entry is the canonical form used in descriptions.
GROUPS: tuple[tuple[str, ...], ...] = (
    ("obtain", "get", "fetch", "retrieve", "acquire"),
    ("compute", "calculate", "evaluate", "derive", "work"),
    ("create", "make", "build", "construct", "generate"),
    ("check", "verify", "validate", "ensure", "confirm"),
    ("convert", "transform", "format", "render", "translate"),
    ("find", "lookup", "search", "locate", "query"),
    ("remove", "delete", "drop", "clear", "discard"),
    ("add", "append", "insert", "include"),
    ("bound", "clamp", "clip", "limit", "cap"),
    ("total", "sum", "aggregate", "tally"),
    ("send", "emit", "publish", "dispatch"),
    ("load", "read", "fetch_file"),
    ("store", "save", "write", "persist"),
    ("decide", "determine", "resolve", "choose"),
    ("compare", "contrast", "diff"),
    ("filter", "select", "screen", "sift"),
    ("rate", "ratio", "proportion", "fraction"),
    ("eligible", "entitled", "qualified", "allowed"),
    ("active", "enabled", "live"),
    ("price", "cost", "amount", "fee", "charge"),
    ("member", "user", "account", "subscriber"),
    ("expiry", "deadline", "expiration", "due"),
)

_CANON: dict[str, str] = {}
for _group in GROUPS:
    for _word in _group:
        _CANON.setdefault(_word, _group[0])


def canonical(word: str) -> str:
    return _CANON.get(word.lower(), word.lower())


def expand(tokens: list[str]) -> list[str]:
    """Add canonical forms and group mates for known tokens (dedup, ordered)."""
    seen: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low not in seen:
            seen.append(low)
        canon = _CANON.get(low)
        if canon is not None:
            if canon not in seen:
                seen.append(canon)
            for mate in next(g for g in GROUPS if g[0] == canon):
                if mate not in seen:
                    seen.append(mate)
    return seen
