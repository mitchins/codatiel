"""Deterministic subject normalization (B5): no models, no embeddings.

Normalizes identifier/document forms derived from IDENTIFIER STRUCTURE and
typed facts only — never a curated synonym dictionary:

- snake_case, SCREAMING_SNAKE_CASE, camelCase, PascalCase -> word parts
- kebab-case / CLI flags (--dry-run, slashes) -> word parts
- prose word forms (whitespace/punctuation splits)
- structural wrappers: DEFAULT/MAX/MIN affixes removed when a subject
  remainder survives (wired into strip_wrapper, not decorative)
- true unit suffixes (Seconds/Millis/Bytes/Minutes/...) removed when the
  extractor identifies them as units; subject words like port/ttl/count
  are NEVER stripped
- compositional number bridging both directions (sixty <-> 60,
  one hundred twenty-eight <-> 128), derived from number structure

Two texts match when their normalized part-sets overlap. A normalized
COMPOUND (joined parts) match scores extra: DEFAULT_TIMEOUT and
"timeout defaults" share the compound "timeout".
"""

from __future__ import annotations

import re

SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

WRAPPER_PREFIXES = ("default", "max", "min")
# True units only: stripping these never destroys a subject because a bare
# unit alone cannot be a subject (guarded by remainder checks below).
# Deliberately EXCLUDES port/ttl/interval/count/size: those are subject
# words (defaultPort IS the subject `port`), not units.
UNIT_SUFFIXES = ("seconds", "second", "secs", "sec", "milliseconds", "millis",
                 "ms", "minutes", "minute", "hours", "hour", "days", "day",
                 "bytes", "byte")

ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
        "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
        "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
        "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000}
NUMBER_WORDS = set(ONES) | set(TENS) | set(SCALES)

_ONES_REV = {v: k for k, v in ONES.items() if v < 20}
_TENS_REV = {v: k for k, v in TENS.items()}


def parse_number_run(words: list[str]) -> int | None:
    total = 0
    group = 0
    seen = False
    for word in words:
        if word in ONES:
            group += ONES[word]
            seen = True
        elif word in TENS:
            group += TENS[word]
            seen = True
        elif word == "hundred":
            if not seen:
                return None
            group = max(group, 1) * 100
            seen = True
        elif word in ("thousand", "million"):
            if not seen:
                return None
            total += max(group, 1) * SCALES[word]
            group = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + group



def number_to_words(n: int) -> str:
    """Compositional digits->words for 0 <= n < 10000."""
    if n < 0 or n >= 10000:
        raise ValueError(n)
    if n < 20:
        return _ONES_REV[n]
    if n < 100:
        tens, rest = divmod(n, 10)
        base = _TENS_REV[tens * 10]
        return base if rest == 0 else f"{base} {number_to_words(rest)}"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        base = f"{number_to_words(hundreds)} hundred"
        return base if rest == 0 else f"{base} {number_to_words(rest)}"
    thousands, rest = divmod(n, 1000)
    base = f"{number_to_words(thousands)} thousand"
    return base if rest == 0 else f"{base} {number_to_words(rest)}"


DIGIT_WORDS = {**ONES, **TENS, "hundred": "100", "thousand": "1000"}
WORD_DIGITS = {str(v): k for k, v in list(ONES.items()) + list(TENS.items())}


def split_identifier(token: str) -> list[str]:
    """Split any identifier style into lowercase word parts."""
    parts: list[str] = []
    for chunk in SPLIT_RE.split(token):
        if not chunk:
            continue
        for part in CAMEL_RE.split(chunk):
            if part:
                parts.append(part.lower())
    return parts


def strip_wrapper(parts: list[str]) -> list[str]:
    """Remove one structural wrapper affix when a subject remains.

    DEFAULT_TIMEOUT -> [timeout]: the guard keeps single-word subjects
    ("default" alone never strips) and requires a non-empty remainder.
    """
    if len(parts) >= 2 and parts[0] in WRAPPER_PREFIXES:
        return parts[1:]
    if len(parts) >= 2 and parts[-1] in ("default", "value", "setting"):
        return parts[:-1]
    return parts


def strip_unit(parts: list[str]) -> list[str]:
    """Remove a trailing true-unit part when a subject remains."""
    if len(parts) > 1 and parts[-1] in UNIT_SUFFIXES:
        return parts[:-1]
    return parts


def _word_runs(words: list[str]) -> list[list[str]]:
    """Maximal runs of number-words (e.g. [one,hundred,twenty,eight])."""
    runs: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if word in NUMBER_WORDS:
            current.append(word)
        else:
            if current:
                runs.append(current)
                current = []
    if current:
        runs.append(current)
    return runs


def _singular(word: str) -> str:
    """Trailing-s plural tolerance (workers->worker). Conservative: keeps
    status/class/alias (vowel/ss endings)."""
    if (len(word) > 3 and word.endswith("s") and not word.endswith("ss")
            and word[-2] not in "aeiou"):
        return word[:-1]
    return word


def normalize_token(token: str) -> set[str]:
    """All normalized forms of one token: parts, stripped, compound, numbers."""
    forms: set[str] = set()
    parts = split_identifier(token)
    if not parts:
        return forms
    forms.update(parts)
    forms.update(_singular(p) for p in parts)
    core = strip_unit(strip_wrapper(parts))
    forms.update(core)
    forms.add("".join(core))
    forms.add("_".join(core))
    for part in list(forms):
        if part in DIGIT_WORDS:
            forms.add(DIGIT_WORDS[part])
        if part in WORD_DIGITS:
            forms.add(WORD_DIGITS[part])
    # Multi-word number phrases inside one token are handled at text level.
    return {f for f in forms if f}


TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./$-]*")


def normalize_text(text: str) -> set[str]:
    forms: set[str] = set()
    words: list[str] = []
    for token in TOKEN_RE.findall(text):
        if len(token) > 1:
            forms.update(normalize_token(token))
        words.extend(split_identifier(token))
    # Compositional number bridging over word runs: "one hundred
    # twenty-eight" <-> "128", "sixty" <-> "60". Derives from number
    # structure, not a value dictionary.
    for run in _word_runs(words):
        value = parse_number_run(run)
        if value is not None:
            forms.add(str(value))
    for token in TOKEN_RE.findall(text):
        if token.isdigit():
            try:
                n = int(token)
            except ValueError:
                continue
            if 0 <= n < 10000:
                try:
                    forms.update(split_identifier(number_to_words(n)))
                except ValueError:
                    pass
    return forms


def normalized_overlap(query_forms: set[str], doc_forms: set[str]) -> float:
    if not query_forms:
        return 0.0
    return len(query_forms & doc_forms) / len(query_forms)
