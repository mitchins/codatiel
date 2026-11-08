"""Deterministic fact extraction for the four mechanical proof classes.

Every extractor is a pure function of (path, text): no models, no IO.
Facts that cannot be extracted deterministically are NEVER guessed —
extractors return [] and the candidate stage records `uncertain`.

Fact shape:
{
  "fact_id": "fact_<digest16>",
  "detector_id": "docs_code",
  "proof_class": "literal_default",
  "fact_kind": "runtime_default" | "documented_default" | ...,
  "subject": "<normalized core compound>",
  "subject_display": "<human form>",
  ...class-specific fields...,
  "path": ..., "line_start": ..., "line_end": ...,
}
Evidence IDs are attached by the caller (the reader of the bytes).
"""

from __future__ import annotations

import re

from codatiel_i2.common.hashing import short_id
from codatiel_i2.common.jsonio import canonical_json

DETECTOR_ID = "docs_code"

# ------------------------------------------------------------------ numbers

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "twenty": 20, "thirty": 30, "sixty": 60,
    "hundred": 100, "thousand": 1000,
}


def parse_number_phrase(words: list[str]) -> int | float | None:
    """Tiny compositional parser for fixture-grade phrases (sixty, one
    hundred twenty-eight, nine hundred). Returns None when not a number."""
    total = 0
    group = 0
    seen = False
    small = {**NUMBER_WORDS}
    tens = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
            "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
    for word in words:
        word = word.lower()
        if word in small and word not in ("hundred", "thousand"):
            group += small[word]
            seen = True
        elif word in tens:
            group += tens[word]
            seen = True
        elif word == "hundred":
            if not seen:
                return None
            group = max(group, 1) * 100
        elif word == "thousand":
            if not seen:
                return None
            total += max(group, 1) * 1000
            group = 0
        else:
            return None
    if not seen:
        return None
    return total + group


def coerce_scalar(raw: str) -> tuple[str, int | float | str]:
    """Return (type, value): int, float, or the raw string when not numeric."""
    raw = raw.strip().strip(",")
    try:
        return "int", int(raw)
    except ValueError:
        pass
    try:
        return "float", float(raw)
    except ValueError:
        pass
    return "string", raw


# ------------------------------------------------------------------ subjects

_WORD_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def subject_parts(name: str) -> list[str]:
    parts: list[str] = []
    for chunk in _WORD_SPLIT.split(name):
        for bit in re.split(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", chunk):
            if bit:
                parts.append(bit.lower())
    return parts


def _core_parts(name: str) -> list[str]:
    parts = subject_parts(name)
    if len(parts) >= 2 and parts[0] in _WRAPPERS:
        parts = parts[1:]
    if len(parts) > 1 and parts[-1] in _UNITS:
        parts = parts[:-1]
    return [_singular(p) for p in parts]


def _singular(word: str) -> str:
    # Known asymmetry: retries stays retries while retry is singular
    # (vowel guard). Same-concept splits across that boundary are a
    # documented recall gap, not a silent mismatch.
    if (len(word) > 3 and word.endswith("s") and not word.endswith("ss")
            and word[-2] not in "aeiou"):
        return word[:-1]
    return word


_WRAPPERS = ("default", "max", "min")
_UNITS = ("seconds", "second", "secs", "sec", "milliseconds", "millis", "ms",
          "minutes", "minute", "hours", "hour", "days", "day", "bytes", "byte",
          "interval", "count", "size", "port")

_TIME_UNITS = {"seconds", "second", "secs", "sec", "milliseconds", "millis",
               "ms", "minutes", "minute", "hours", "hour", "days", "day"}
_SIZE_UNITS = {"bytes", "byte"}
_COUNT_UNITS = {"count", "size"}
# Identity-like suffixes: WORKER_PORT vs WORKER_COUNT must NOT match.
_IDENTITY_UNITS = {"port", "interval"}


def _unit_dimension(part: str) -> str:
    if part in _TIME_UNITS:
        return "time"
    if part in _SIZE_UNITS:
        return "size"
    if part in _COUNT_UNITS:
        return "count"
    return "identity"


def strips_compatible(doc_raw: list[str], doc_core: list[str],
                      code_raw: list[str], code_core: list[str]) -> bool:
    """Stripped-away parts must not discriminate: WORKER_PORT (port) vs
    WORKER_COUNT (count) collide after stripping but name different
    dimensions. Same-dimension strips (interval vs seconds is time... no:
    interval is identity) or one-sided strips stay compatible."""
    doc_stripped = [_singular(p) for p in doc_raw if _singular(p) not in doc_core]
    code_stripped = [_singular(p) for p in code_raw if _singular(p) not in code_core]
    if not doc_stripped or not code_stripped:
        return True

    def _ok(part_d: str, part_c: str) -> bool:
        if part_d == part_c:
            return True
        dim_d, dim_c = _unit_dimension(part_d), _unit_dimension(part_c)
        return dim_d == dim_c and dim_d != "identity"

    # Every stripped part needs a compatible counterpart on the other side.
    return all(any(_ok(d, c) for c in code_stripped) for d in doc_stripped) and all(
        any(_ok(d, c) for d in doc_stripped) for c in code_stripped)



def subject_key(name: str) -> str:
    """Normalized subject compound: DEFAULT_TIMEOUT -> timeout."""
    return "".join(_core_parts(name))


def _fact(proof_class: str, fact_kind: str, path: str, line_start: int,
          line_end: int, payload: dict, revision: str = "",
          revision_sha: str = "", evidence_ids: list | None = None) -> dict:
    evidence_ids = sorted(evidence_ids or [])
    extractor_id, extractor_version = _extractor_for(proof_class, fact_kind, payload)
    fid = "fact_" + short_id(DETECTOR_ID, proof_class, fact_kind, path,
                             str(line_start), str(line_end), revision,
                             revision_sha, extractor_id, extractor_version,
                             canonical_json(payload), ",".join(evidence_ids))
    return {"fact_id": fid, "detector_id": DETECTOR_ID, "proof_class": proof_class,
            "fact_kind": fact_kind, "path": path, "line_start": line_start,
            "line_end": line_end, "revision": revision,
            "revision_sha": revision_sha, "evidence_ids": evidence_ids,
            "extractor_id": extractor_id, "extractor_version": extractor_version,
            **payload}


def _extractor_for(proof_class: str, fact_kind: str, payload: dict) -> tuple[str, str]:
    """Strict registry from fact type to the exact extraction logic."""
    if payload.get("claim_directed"):
        return "docs_code.documented_value_claim.v1", "v1"
    table = {
        ("literal_default", "runtime_default"): "docs_code.runtime_default.v1",
        ("literal_default", "documented_default"): "docs_code.documented_default.v1",
        ("named_contract", "declared_name"): "docs_code.declared_name.v1",
        ("named_contract", "documented_name"): "docs_code.documented_name.v1",
        ("allowed_values", "implemented_values"): "docs_code.implemented_values.v1",
        ("allowed_values", "documented_values"): "docs_code.documented_values.v1",
        ("interface", "implemented_interface"): "docs_code.implemented_interface.v1",
        ("interface", "documented_interface"): "docs_code.documented_interface.v1",
    }
    name = table.get((proof_class, fact_kind), "")
    return (name, "v1") if name else ("", "")


def bind_fact(fact: dict, revision: str, revision_sha: str,
              evidence: list[dict]) -> dict:
    """Bind an extracted fact to the exact revision bytes + evidence that
    produced it. The ID incorporates revision, SHA and evidence IDs, so
    unchanged base/head facts never share identity. Evidence IDs are
    deduplicated: re-reading a file must not change the identity of an
    otherwise identical logical fact (that instability defeated issue
    dedup by candidate ID)."""
    ids = sorted({e["evidence_id"] for e in evidence
                  if e.get("path") == fact.get("path")
                  and e.get("revision") == revision
                  and e.get("evidence_id")})
    payload = {k: v for k, v in fact.items()
               if k not in ("fact_id", "detector_id", "proof_class", "fact_kind",
                           "path", "line_start", "line_end",
                           "revision", "revision_sha", "evidence_ids",
                           "extractor_id", "extractor_version")}
    return _fact(fact.get("proof_class", ""), fact.get("fact_kind", ""),
                 fact.get("path", ""), fact.get("line_start", 1),
                 fact.get("line_end", 1), payload, revision, revision_sha, ids)


def bind_facts(facts_list: list[dict], path: str, revision: str,
               revision_sha: str, evidence: list[dict]) -> list[dict]:
    return [bind_fact(f, revision, revision_sha, evidence) for f in facts_list
            if f.get("path") == path]


CODE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".go", ".swift", ".rs")


def enumerate_code_names(root) -> tuple[set[str], set[str], int, list[str]]:
    """Enumerate declared names over a materialised checkout.

    Shared by detection and replay: both sides run this function, so the
    surface can never be a persisted assertion. Returns
    (names, aliases, scanned_files, scanned_rel_paths).
    """
    from pathlib import Path

    root = Path(root)
    names: set[str] = set()
    aliases: set[str] = set()
    scanned = 0
    rel_paths: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix not in CODE_SUFFIXES:
            continue
        scanned += 1
        rel_paths.append(str(path.relative_to(root)))
        try:
            content = path.read_text(encoding="utf-8", errors="strict").splitlines()
        except Exception:
            continue
        for fact in extract_code_names(str(path.relative_to(root)), content):
            names.add(fact["name"])
            aliases.update(fact.get("aliases", []))
    return names, aliases, scanned, rel_paths


# ------------------------------------------------------------------ literal

_CODE_LITERAL = re.compile(
    r"^\s*(?:(?:const|let|var)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[A-Za-z0-9_<>\[\]]+\s*)?=\s*(-?\d+(?:\.\d+)?)\s*;?\s*(?://|#|$)")


def extract_code_literals(path: str, lines: list[str]) -> list[dict]:
    """`NAME = 60` / `const NAME = 60;` / `let name = 60` declarations."""
    facts = []
    for i, line in enumerate(lines, 1):
        match = _CODE_LITERAL.match(line)
        if not match:
            continue
        name, raw = match.group(1), match.group(2)
        vtype, value = coerce_scalar(raw)
        if vtype == "string":
            continue
        facts.append(_fact("literal_default", "runtime_default", path, i, i, {
            "subject": subject_key(name), "subject_display": name,
            "subject_parts": _core_parts(name),
            "subject_raw_parts": subject_parts(name),
            "value": value, "value_type": vtype, "unit": "",
        }))
    return facts


_DOC_LITERAL_PATTERNS = [
    # "defaults to 60 seconds" / "default `60` (seconds)"
    re.compile(r"defaults?\s+to\s+`?(-?\d+(?:\.\d+)?)`?\s*([A-Za-z]*)", re.I),
    re.compile(r"default\s+`(-?\d+(?:\.\d+)?)`\s*\(?([A-Za-z]*)\)?", re.I),
    # "| subject | 60 seconds |" table cells
    re.compile(r"\|\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\|\s*`?(-?\d+(?:\.\d+)?)`?\s*([A-Za-z]*)\s*\|"),
    # "- `subject`: default `60` (seconds)"
    re.compile(r"-\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*:\s*default\s*`(-?\d+(?:\.\d+)?)`\s*\(?([A-Za-z]*)\)?"),
]


def _doc_number_words(text: str, start: int) -> tuple[int | float | None, int]:
    """Parse a number-word phrase at position start. Returns (value, end)."""
    words = re.findall(r"[A-Za-z-]+", text[start:start + 60].lower())
    for length in (4, 3, 2, 1):
        cand = " ".join(words[:length]).replace("-", " ").split()
        value = parse_number_phrase(cand)
        if value is not None:
            return value, start
    return None, start


def _phrase_subject(line: str) -> str:
    """Subject phrase before 'defaults to': 'The max connections defaults
    to 64' -> 'max connections'. Falls back to token guessing."""
    match = re.search(r"([A-Za-z_][A-Za-z0-9_ ]*?)\s+defaults?\s+to", line, re.I)
    if match:
        phrase = re.sub(r"^(the|a|an)\s+", "", match.group(1).strip(), flags=re.I)
        if phrase:
            return phrase
    return _guess_doc_subject(line)


def extract_doc_literals(path: str, lines: list[str]) -> list[dict]:
    facts = []
    seen: set[tuple] = set()
    for i, line in enumerate(lines, 1):
        for pattern in _DOC_LITERAL_PATTERNS:
            for match in pattern.finditer(line):
                groups = match.groups()
                if len(groups) == 3 and not groups[0][0].isdigit():
                    subject, raw, unit = groups
                    vtype, value = coerce_scalar(raw)
                elif len(groups) >= 2:
                    subject = _phrase_subject(line)
                    raw, unit = groups[0], (groups[1] if len(groups) > 1 else "")
                    vtype, value = coerce_scalar(raw)
                else:
                    continue
                if vtype == "string":
                    continue
                key = (subject_key(subject), value, (unit or "").lower())
                if key in seen:
                    continue
                seen.add(key)
                facts.append(_fact("literal_default", "documented_default", path, i, i, {
                    "subject": subject_key(subject), "subject_display": subject,
                    "subject_parts": _core_parts(subject),
                    "subject_raw_parts": subject_parts(subject),
                    "value": value, "value_type": vtype, "unit": (unit or "").lower(),
                }))
        # number-word form: "defaults to sixty seconds"
        word_match = re.search(r"defaults?\s+to\s+([A-Za-z][A-Za-z-]*(?:\s+[A-Za-z-]+){0,3})", line, re.I)
        if word_match:
            words = word_match.group(1).replace("-", " ").split()
            for length in (4, 3, 2, 1):
                value = parse_number_phrase(words[:length])
                if value is not None:
                    rest = words[length:]
                    unit = rest[0].lower() if rest else ""
                    subject = _phrase_subject(line)
                    vtype = "int" if isinstance(value, int) else "float"
                    facts.append(_fact("literal_default", "documented_default", path, i, i, {
                        "subject": subject_key(subject), "subject_display": subject,
                        "subject_parts": _core_parts(subject),
                        "subject_raw_parts": subject_parts(subject),
                        "value": value, "value_type": vtype, "unit": unit,
                    }))
                    break
    return facts


def _guess_doc_subject(line: str) -> str:
    """Best-effort subject: first backticked token, else first long word."""
    backticked = re.findall(r"`([^`]+)`", line)
    if backticked:
        return backticked[0]
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_]*", line)
             if w.lower() not in ("the", "defaults", "default", "to", "is", "a", "an")]
    return words[0] if words else "unknown"


# ------------------------------------------------------------------ named contracts

_ENV_READERS = [
    re.compile(r"os\.getenv\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"),
    # os.environ.get("X") / os.environ["X"]: the dominant Python idiom.
    re.compile(r"os\.environ\.get\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"),
    re.compile(r"os\.environ\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]"),
    re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"process\.env\[([\"'])([A-Za-z_][A-Za-z0-9_]*)\1\]"),
    re.compile(r'std::env::var\(\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\)'),
    re.compile(r'os\.Getenv\(\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\)'),
    re.compile(r'environment\["([A-Za-z_][A-Za-z0-9_]*)"\]'),
]

# Module-level string constants used as env keys:
#   PASSWORD_ENV = "YT2WIKI_OPERATOR_PASSWORD"; os.environ.get(PASSWORD_ENV)
_CONST_ASSIGN = re.compile(
    r"^\s*([A-Z][A-Z0-9_]{2,})\s*=\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']",
    re.M)


def _const_env_names(text: str) -> dict[str, str]:
    """Map UPPER_SNAKE identifiers to the string literal they alias."""
    return {m.group(1): m.group(2) for m in _CONST_ASSIGN.finditer(text)}


def _dynamic_env_read(text: str) -> bool:
    """True when env is read through a non-literal key: os.environ.get(name),
    os.environ[expr], os.getenv(var). Absence from the static surface then
    proves nothing (ambiguous_absence, never a verified miss)."""
    stripped = re.sub(r"os\.environ(?:\.get)?\(\s*[\"'][^\"']*[\"']", "", text)
    stripped = re.sub(r"os\.environ\[\s*[\"'][^\"']*[\"']\s*\]", "", stripped)
    stripped = re.sub(r"os\.getenv\(\s*[\"'][^\"']*[\"']", "", stripped)
    return bool(re.search(
        r"os\.environ(?:\.get)?\(\s*[A-Za-z_]|os\.environ\[\s*[A-Za-z_]"
        r"|os\.getenv\(\s*[A-Za-z_]", stripped))

_CLI_FLAGS = [
    re.compile(r"add_argument\(\s*[\"'](--[A-Za-z0-9-]+)[\"']"),
    re.compile(r'\.option\(\s*["\'](--[A-Za-z0-9-]+)["\']'),
    re.compile(r'flag\.Bool\(\s*"([A-Za-z0-9-]+)"'),
    re.compile(r'customLong\(\s*"([A-Za-z0-9-]+)"\s*\)'),
    re.compile(r'long\s*=\s*"([A-Za-z0-9-]+)"'),
    # Generic string-literal flag use: .includes('--json'), == "--dry-run".
    re.compile(r"[\"'](--[a-z0-9][a-z0-9-]*)[\"']"),
]

_ALIAS_PATTERNS = [
    re.compile(r"os\.getenv\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\)\s*or\s*os\.getenv"),
    re.compile(r"\?\?\s*process\.env"),
    re.compile(r'if\s+value\s*==\s*""\s*\{'),
    re.compile(r'\?\?\s*ProcessInfo'),
    re.compile(r"or_else\(\s*\|_\|\s*std::env::var"),
]


def extract_code_names(path: str, lines: list[str]) -> list[dict]:
    """Declared env vars, CLI flags, config keys with alias detection."""
    facts = []
    text = "\n".join(lines)
    consts = _const_env_names(text)
    seen: set[tuple] = set()
    for pattern in _ENV_READERS:
        for match in pattern.finditer(text):
            name = match.group(match.lastindex or 1)
            lineno = text[:match.start()].count("\n") + 1
            key = (name, lineno)
            if key in seen:
                continue
            seen.add(key)
            facts.append(_fact("named_contract", "declared_name", path, lineno, lineno, {
                "name": name, "surface": _classify_surface(name, match.group(0)),
                "aliases": _find_aliases(text, name),
            }))
    # Constant indirection resolved in the same file only: IDENT = "LITERAL"
    # followed by os.environ.get(IDENT). Data-flow beyond this is out of scope.
    for ident, literal in consts.items():
        hit = re.search(
            rf"os\.environ(?:\.get)?\(\s*{ident}\s*[,)]|"
            rf"os\.environ\[\s*{ident}\s*\]|"
            rf"os\.getenv\(\s*{ident}\s*[,)]", text)
        if hit and (literal, 1) not in seen:
            lineno = text[:hit.start()].count("\n") + 1
            seen.add((literal, 1))
            facts.append(_fact("named_contract", "declared_name", path, lineno, lineno, {
                "name": literal, "surface": "env",
                "aliases": _find_aliases(text, literal),
            }))
    for pattern in _CLI_FLAGS:
        for match in pattern.finditer(text):
            name = match.group(1)
            lineno = text[:match.start()].count("\n") + 1
            facts.append(_fact("named_contract", "declared_name", path, lineno, lineno, {
                "name": name, "surface": "cli", "aliases": _find_aliases(text, name),
            }))
    return facts


def _classify_surface(name: str, context: str) -> str:
    if name.startswith("--") or "option(" in context or "add_argument" in context or "flag" in context.lower():
        return "cli"
    if ("getenv" in context or "process.env" in context or "env::var" in context
            or "environment[" in context or "os.environ" in context):
        return "env"
    return "config"


def _find_aliases(text: str, name: str) -> list[str]:
    """Detect fallback reads mentioning another name near this one."""
    aliases: set[str] = set()
    for pattern in _ALIAS_PATTERNS:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 200):match.end() + 200]
            if name in window:
                for other in re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_-]*)[\"']", window):
                    if other != name:
                        aliases.add(other)
    return sorted(aliases)


_DOC_NAME = re.compile(r"`(-{0,2}[A-Za-z_][A-Za-z0-9_-]*)`")


_BARE_FLAG = re.compile(r"(?<![A-Za-z0-9_`-])(--[a-z0-9][a-z0-9-]*)\b")
# 5+ all-caps chars: WORKERS yes, API/CLI/URL no.
_BARE_ENV = re.compile(r"\b([A-Z][A-Z0-9_]{4,})\b")
# A removal note is explicitly NOT a provision claim ("Removed X in v2").
# Neither is a non-requirement ("Does not require --gpus").
_REMOVAL_LINE = re.compile(r"\b(removed?|removes|deleted?|dropped|retired|no longer|"
                           r"does\s+not\s+require|do\s+not\s+require|"
                           r"requires?\s+no|not\s+required)\b", re.I)
# A contract must be CLAIMED (provision verb), SHAPED (UPPER_SNAKE env or
# --flag), not negated and not reference-framed. Arbitrary prose nouns
# (export, model, torch), JSON/config keys and foreign-tool flags are not
# contracts, even on a line that happens to contain "set" or "now".
_CLAIM_VERBS = ("support", "set ", "configur", "flag", "option", "requir",
                "provid", "accept", "expect", "use ", "pass", "defin",
                "need", "allow", "specif", "expos", "enabl", "formerly",
                "alias", "reference", "now")
# "no `FOO`" / "without `FOO`": asserts absence, never provision.
_NO_PROVISION = re.compile(r"(?:^|[\s(>,;])(?:no|without)\s+`([^`]+)`")


def _contract_shape(name: str) -> bool:
    if name.startswith("--"):
        return True
    return bool(name.isupper() and (len(name) >= 4 or "_" in name))


def extract_doc_names(path: str, lines: list[str]) -> list[dict]:
    """Backticked names in claim-bearing sentences, CLI flags, or ENV-style
    constants. A name becomes a contract ONLY when it is claimed (provision
    verb on the line) AND shaped (UPPER_SNAKE env or --flag): bare prose
    nouns, JSON/config keys and foreign-tool flags never qualify, even on
    lines containing weak verbs like "set" or "now"."""
    facts = []
    seen: set[tuple] = set()
    for i, line in enumerate(lines, 1):
        lowered = line.lower()
        if _REMOVAL_LINE.search(line):
            continue
        noprov = set()
        for m in _NO_PROVISION.finditer(line):
            noprov.add(m.group(1))
        for name in _DOC_NAME.findall(line):
            if name in noprov:
                continue
            if not _contract_shape(name):
                continue
            if not any(k in lowered for k in _CLAIM_VERBS):
                continue
            key = (name, i)
            if key in seen:
                continue
            seen.add(key)
            facts.append(_fact("named_contract", "documented_name", path, i, i, {
                "name": name,
                "surface": "cli" if name.startswith("--") else "env",
                "claim": line.strip()[:300],
            }))
        if any(v in lowered for v in ("support", "set ", "configure", "flag", "now")):
            for name in _BARE_FLAG.findall(line) + _BARE_ENV.findall(line):
                if name.lower() in ("the", "and", "now", "set"):
                    continue
                key = (name, i)
                if key in seen:
                    continue
                seen.add(key)
                facts.append(_fact("named_contract", "documented_name", path, i, i, {
                    "name": name,
                    "surface": "cli" if name.startswith("--") else "env",
                    "claim": line.strip()[:300],
                }))
    return facts


# ------------------------------------------------------------------ allowed values

_CODE_SETS = [
    re.compile(r"Literal\[([^\]]+)\]"),
    re.compile(r"type\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*((?:[\"'][^\"']+[\"']\s*\|\s*)+[\"'][^\"']+[\"'])"),
    re.compile(r"\[\]string\{([^}]+)\}"),
    re.compile(r"enum\s+[A-Za-z_][A-Za-z0-9_]*(?:\s*:\s*[A-Za-z_][A-Za-z0-9_]*)?\s*\{([^}]+)\}"),
]


def _split_values(raw: str) -> list[str]:
    parts = []
    for chunk in re.split(r"[,|]", raw):
        chunk = chunk.strip().strip("\"'")
        chunk = re.sub(r"^(or|and)\s+", "", chunk, flags=re.I).strip()
        chunk = re.sub(r"^case\s+", "", chunk, flags=re.I).strip()
        if chunk:
            parts.append(chunk.lower())
    return parts


def extract_code_sets(path: str, lines: list[str]) -> list[dict]:
    facts = []
    text = "\n".join(lines)
    subject = _guess_code_subject(text)
    for pattern in _CODE_SETS:
        for match in pattern.finditer(text):
            values = _split_values(match.group(1))
            if len(values) < 1:
                continue
            lineno = text[:match.start()].count("\n") + 1
            facts.append(_fact("allowed_values", "implemented_values", path, lineno, lineno, {
                "subject": subject_key(subject), "subject_display": subject,
                "subject_parts": _core_parts(subject),
                "subject_raw_parts": subject_parts(subject),
                "values": sorted(set(values)),
            }))
    return facts


def _guess_code_subject(text: str) -> str:
    match = re.search(r"(?:type|enum|var|const|let)\s+([A-Za-z_][A-Za-z0-9_]*)", text)
    if match:
        return match.group(1)
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*Literal", text)
    return match.group(1) if match else "unknown"


_DOC_EXHAUSTIVE = re.compile(r"(may be|allowed .* values|must be one of)", re.I)


_DOC_VALUE_CLAIM = [
    re.compile(r"now supports?\s+[\"']?([A-Za-z0-9_.-]+)[\"']?", re.I),
    re.compile(r"support for\s+[\"']?([A-Za-z0-9_.-]+)[\"']?", re.I),
    re.compile(r"([A-Za-z][A-Za-z0-9_.-]*)\s+is now supported", re.I),
]


def extract_doc_value_claims(path: str, added_lines: list[tuple[int, str]]) -> list[dict]:
    """Claim-directed values from ADDED doc lines only: 'now supports xml'.

    The value prefers an ALLCAPS token in the claim phrase, else the
    directly governed word. Suspicious rewordings without claim verbs
    produce nothing.
    """
    facts = []
    for lineno, line in added_lines:
        for pattern in _DOC_VALUE_CLAIM:
            match = pattern.search(line)
            if not match:
                continue
            raw_value = match.group(1)
            if len(match.groups()) > 1 and match.lastindex:
                raw_value = match.group(match.lastindex)
            # Prefer an ALLCAPS token anywhere in the line (CSV in "CSV
            # report output is now supported"); else the governed word.
            line_caps = [c for c in re.findall(r"[A-Za-z][A-Za-z0-9_.-]*", line)
                         if c.isupper() and len(c) > 1]
            value = (line_caps[0] if line_caps else raw_value).strip("\"'").rstrip(".").lower()
            if not value:
                continue
            facts.append(_fact("allowed_values", "documented_values", path, lineno, lineno, {
                "subject": subject_key(value), "subject_display": value,
                "subject_parts": _core_parts(value),
                "subject_raw_parts": subject_parts(value),
                "values": [value], "exhaustiveness": "unknown",
                "claim_directed": True,
            }))
    return facts


def extract_doc_sets(path: str, lines: list[str]) -> list[dict]:
    facts = []
    for i, line in enumerate(lines, 1):
        backticked = re.findall(r"`([^`]+)`", line)
        plain_sets = re.findall(r"may be ([A-Za-z0-9_,\s]+?)(?:\.|$)", line, re.I)
        values: list[str] = []
        subject_guess = _guess_doc_subject(line)
        for item in backticked:
            if subject_key(item) == subject_key(subject_guess):
                continue  # the subject names the set; it is not a member
            if "," in item or item.strip().lower() in ("json", "yaml", "xml", "asc", "desc"):
                values.extend(_split_values(item))
            elif re.fullmatch(r"[A-Za-z0-9_.-]+", item.strip()) and len(backticked) > 1:
                values.append(item.strip().lower())
        for group in plain_sets:
            values.extend(_split_values(group))
        or_groups = re.findall(r"accepts\s+([A-Za-z0-9_,\s]+?)(?:\.|$)", line, re.I)
        for group in or_groups:
            values.extend(_split_values(group))
        values = sorted(set(values))
        if not values:
            continue
        exhaustive = "exhaustive" if _DOC_EXHAUSTIVE.search(line) else (
            "exhaustive" if len(backticked) >= 2 and ("values" in line.lower()) else "unknown")
        subject = _guess_doc_subject(line)
        facts.append(_fact("allowed_values", "documented_values", path, i, i, {
            "subject": subject_key(subject), "subject_display": subject,
            "subject_parts": _core_parts(subject),
            "subject_raw_parts": subject_parts(subject),
            "values": values, "exhaustiveness": exhaustive,
        }))
    return facts


# ------------------------------------------------------------------ interface

# Receivers that register routes. An ordinary method CALL is only route
# evidence on one of these; anything else (config, entry, client, view,
# providers, ...) is data access, never a served endpoint. Ambiguous
# receivers must not count as evidence that a route is served: a phantom
# surface entry would hide a genuinely missing documented route.
_ROUTE_REGISTRARS = frozenset({"app", "router", "api"})

_ROUTE_PATTERNS = [
    # @app.get("/x") / @router.post("/y"): decorator registration syntax
    # is itself evidence of a route; any receiver qualifies.
    (re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']"), "http_route"),
    # app.get("/x"): an ordinary call needs a registrar receiver.
    # config.get("/library") and entry.get("job_id") are not endpoints.
    (re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]"), "http_route"),
    (re.compile(r"HandleFunc\(\s*\"([^\"]+)\""), "http_route"),
    (re.compile(r"\.route\(\s*\"([^\"]+)\""), "http_route"),
]

_DOC_ROUTE = re.compile(r"`(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_./{}-]+)`")
_DOC_BARE_PATH = re.compile(r"(?:exposes|expose|at|route|endpoint|path)\s+(/[A-Za-z0-9_./{}-]+)", re.I)


def _clean_path(path: str) -> str:
    return path.rstrip(".")

_SIG_DEF = re.compile(
    r"^\s*(?:def|function|func|fn)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)")


def _parse_params(lang_hint: str, params: str) -> list[dict]:
    out = []
    depth = 0
    current = ""
    current_eq0 = False  # '=' at nesting depth 0: keyword-passed or defaulted
    parts = []
    for char in params:
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        if char == "=" and depth == 0:
            current_eq0 = True
        if char == "," and depth == 0:
            parts.append((current, current_eq0))
            current = ""
            current_eq0 = False
        else:
            current += char
    if current.strip():
        parts.append((current, current_eq0))
    keyword_only = False
    for part, eq0 in parts:
        part = part.strip()
        if part == "*":
            keyword_only = True
            continue
        if part.startswith("**"):
            # Keyword variadic (**kwargs) absorbs extra keywords; it does
            # not change the positional/keyword-only boundary (and must be
            # last in valid Python).
            continue
        if re.match(r"\*\s*[A-Za-z_]", part):
            # Positional variadic (*args, *args: T): every following
            # parameter is keyword-only per Python semantics. Record the
            # splat at the interface level via _variadic_kinds, never as a
            # named parameter.
            keyword_only = True
            continue
        name_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", part)
        if not name_match:
            continue
        name = name_match.group(1)
        if name in ("self", "cls"):
            continue
        required = ("=" not in part and "?" not in part and "opts" not in part.lower()
                    and "options" not in part.lower())
        out.append({"name": name, "required": required,
                    "keyword_only": keyword_only, "keyword": eq0})
    return out


def _variadic_kinds(params: str) -> tuple[bool, bool]:
    """(pos_variadic, kw_variadic) for one raw parameter string.

    ``*args`` absorbs extra POSITIONALS; ``**kwargs`` absorbs extra
    KEYWORDS; neither does the other's job. A bare ``*`` is only the
    keyword-only marker and absorbs nothing.
    """
    depth = 0
    current = ""
    parts = []
    for char in params:
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    pos = kw = False
    for part in parts:
        stripped = part.strip()
        if re.match(r"\*\*\s*[A-Za-z_]", stripped):
            kw = True
        elif re.match(r"\*\s*[A-Za-z_]", stripped):
            pos = True
    return pos, kw


def extract_code_interfaces(path: str, lines: list[str]) -> list[dict]:
    facts = []
    text = "\n".join(lines)
    for pi, (pattern, kind) in enumerate(_ROUTE_PATTERNS):
        for match in pattern.finditer(text):
            groups = match.groups()
            if len(groups) == 3:
                receiver, method, route = groups[0], groups[1].upper(), groups[2]
                if pi == 1 and receiver.split(".")[-1] not in _ROUTE_REGISTRARS:
                    # Ordinary call on a non-registrar receiver:
                    # data access, not a served endpoint.
                    continue
            elif len(groups) == 2:
                method, route = groups[0].upper(), groups[1]
            else:
                method, route = "*", groups[0]
            # Route discipline: a served path starts with '/'. Bare tokens
            # (dict .get("job_id"), config lookups) are not routes, even when
            # the receiver looks like an HTTP verb.
            if not route.startswith("/"):
                continue
            lineno = text[:match.start()].count("\n") + 1
            facts.append(_fact("interface", "implemented_interface", path, lineno, lineno, {
                "subject": route, "interface": {"kind": "http_route", "method": method, "path": route},
            }))
    for i, line in enumerate(lines, 1):
        match = _SIG_DEF.match(line)
        if match:
            symbol, params = match.group(1), match.group(2)
            pos_var, kw_var = _variadic_kinds(params)
            facts.append(_fact("interface", "implemented_interface", path, i, i, {
                "subject": symbol,
                "interface": {"kind": "call_signature", "symbol": symbol,
                              "parameters": _parse_params(path, params),
                              "variadic_pos": pos_var, "variadic_kw": kw_var},
            }))
    return facts


def extract_doc_interfaces(path: str, lines: list[str]) -> list[dict]:
    from codatiel_i2.detectors.docs_code.candidates import NOT_SERVED_LINE
    facts = []
    for i, line in enumerate(lines, 1):
        # A line that disclaims serving ("verified 404", "not served")
        # describes an upstream/third-party API, never a project contract.
        route_disclaimed = bool(NOT_SERVED_LINE.search(line))
        for match in _DOC_ROUTE.finditer(line):
            if route_disclaimed:
                continue
            method, route = match.group(1), _clean_path(match.group(2))
            facts.append(_fact("interface", "documented_interface", path, i, i, {
                "subject": route,
                "interface": {"kind": "http_route", "method": method, "path": route},
            }))
        for match in _DOC_BARE_PATH.finditer(line):
            if route_disclaimed:
                continue
            if "`" in line[max(0, match.start() - 40):match.start()]:
                continue  # already covered by a backticked route nearby
            route = _clean_path(match.group(1))
            facts.append(_fact("interface", "documented_interface", path, i, i, {
                "subject": route,
                "interface": {"kind": "http_route", "method": None, "path": route},
            }))
        for match in re.finditer(r"`([A-Za-z_][A-Za-z0-9_]*)\(([^`]*)\)`", line):
            symbol, params = match.group(1), match.group(2)
            pos_var, kw_var = _variadic_kinds(params)
            facts.append(_fact("interface", "documented_interface", path, i, i, {
                "subject": symbol,
                "interface": {"kind": "call_signature", "symbol": symbol,
                              "parameters": _parse_params(path, params),
                              "variadic_pos": pos_var, "variadic_kw": kw_var},
            }))
    return facts


_ROUTE_TEMPLATE = re.compile(r"\{[^}]*\}")


def normalize_route_template(path: str) -> str:
    """Path params are wildcards: /articles/{id} == /articles/{article_id}."""
    return _ROUTE_TEMPLATE.sub("{}", path)


def pool_has_dynamic_reads(pool: dict[str, str]) -> bool:
    """True when any pooled code file reads env through a non-literal key."""
    return any(path.endswith(CODE_SUFFIXES) and _dynamic_env_read(text)
               for path, text in (pool or {}).items())


def dynamic_env_reads_in(root) -> bool:
    """True when any code file reads env through a non-literal key."""
    from pathlib import Path

    root = Path(root)
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix not in CODE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except Exception:
            continue
        if _dynamic_env_read(text):
            return True
    return False


def enumerate_code_routes(root) -> tuple[set[tuple[str, str]], int]:
    """Repo-wide implemented route surface: (method, template) pairs.

    Route serving is a repository property; judging it against one
    counterpart file manufactures misses for routes served elsewhere.
    Shares the file walk with enumerate_code_names (same CODE_SUFFIXES).
    """
    from pathlib import Path

    root = Path(root)
    routes: set[tuple[str, str]] = set()
    scanned = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix not in CODE_SUFFIXES:
            continue
        scanned += 1
        try:
            content = path.read_text(encoding="utf-8", errors="strict").splitlines()
        except Exception:
            continue
        for fact in extract_code_interfaces(str(path.relative_to(root)), content):
            iface = fact.get("interface", {})
            if iface.get("kind") == "http_route":
                routes.add((iface.get("method", "*"),
                            normalize_route_template(iface.get("path", ""))))
    return routes, scanned


EXTRACTORS: dict[str, object] = {}


def _register_extractors() -> None:
    EXTRACTORS.update({
        "docs_code.runtime_default.v1": extract_code_literals,
        "docs_code.documented_default.v1": extract_doc_literals,
        "docs_code.declared_name.v1": extract_code_names,
        "docs_code.documented_name.v1": extract_doc_names,
        "docs_code.implemented_values.v1": extract_code_sets,
        "docs_code.documented_values.v1": extract_doc_sets,
        "docs_code.documented_value_claim.v1": extract_doc_value_claims,
        "docs_code.implemented_interface.v1": extract_code_interfaces,
        "docs_code.documented_interface.v1": extract_doc_interfaces,
    })


_register_extractors()


def replay_fact(fact: dict, evidence_by_id: dict, *,
                base_checkout=None, head_checkout=None) -> dict:
    """Re-extract one persisted fact from its cited revision bytes.

    Returns {"ok": True, "fact": recomputed} or {"ok": False, "errors": [...] }.
    Uses the SAME extraction functions as detection (no trust regexes).
    """
    from pathlib import Path

    from codatiel_i2.core.evidence import (
        checkout_holds_sha, replay_source_evidence, validate_envelope)

    errors: list[str] = []
    extractor_id = fact.get("extractor_id", "")
    fn = EXTRACTORS.get(extractor_id)
    if fn is None:
        return {"ok": False, "errors": [f"unknown extractor {extractor_id!r}"]}
    revision = fact.get("revision", "")
    checkout = {"base": base_checkout, "head": head_checkout}.get(revision)
    if checkout is None:
        return {"ok": False, "errors": [f"no {revision} checkout for fact replay"]}
    checkout = Path(checkout)
    revision_sha = fact.get("revision_sha", "")
    if not checkout_holds_sha(checkout, revision_sha):
        return {"ok": False, "errors": ["checkout does not hold fact revision_sha"]}
    # 1-4. Resolve + validate + replay every cited evidence item, bound to
    # this fact's revision/path/SHA.
    envelopes: list[dict] = []
    if not fact.get("evidence_ids"):
        return {"ok": False, "errors": ["fact cites no evidence"]}
    for eid in fact.get("evidence_ids", []):
        env = evidence_by_id.get(eid)
        if env is None:
            return {"ok": False, "errors": [f"fact evidence {eid!r} does not resolve"]}
        if env.get("revision") != revision or env.get("revision_sha") != revision_sha:
            return {"ok": False, "errors": [f"fact evidence {eid!r} revision mismatch"]}
        if env.get("path") != fact.get("path"):
            return {"ok": False, "errors": [f"fact evidence {eid!r} path mismatch"]}
        errors.extend(validate_envelope(env))
        errors.extend(replay_source_evidence(env, base_checkout=base_checkout,
                                             head_checkout=head_checkout))
        envelopes.append(env)
    if errors:
        return {"ok": False, "errors": errors}
    # 5. Re-read the exact file and re-run the registered extractor.
    try:
        target = checkout / fact.get("path", "")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {"ok": False, "errors": [f"cannot re-read fact file: {exc}"]}
    if extractor_id == "docs_code.documented_value_claim.v1":
        base_target = Path(base_checkout) / fact.get("path", "") if base_checkout else None
        try:
            base_lines = base_target.read_text(encoding="utf-8", errors="replace").splitlines() \
                if base_target and base_target.exists() else []
        except Exception:
            base_lines = []
        base_set = set(base_lines)
        added = [(i + 1, line) for i, line in enumerate(lines) if line not in base_set]
        recomputed = extract_doc_value_claims(fact.get("path", ""), added)
    else:
        recomputed = fn(fact.get("path", ""), lines)  # type: ignore[operator]
    # 6. The persisted fact must exactly match one re-extracted fact
    # (canonical compare excluding fact_id), then the ID recomputes.
    for candidate in recomputed:
        bound = bind_fact(candidate, revision, revision_sha, envelopes)
        mine = {k: v for k, v in fact.items() if k != "fact_id"}
        theirs = {k: v for k, v in bound.items() if k != "fact_id"}
        if mine == theirs:
            if bound["fact_id"] != fact.get("fact_id"):
                return {"ok": False, "errors": ["recomputed fact_id mismatch"]}
            return {"ok": True, "fact": bound}
    return {"ok": False, "errors": ["persisted fact not present in re-extracted bytes"]}
