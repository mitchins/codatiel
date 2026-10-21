"""Contract fact model + deterministic extractors (C3.1/C3.2).

Fact vocabulary (small, reusable across the 7-cluster mechanical slice):

- provided_names / required_names over namespaces:
    mapping_key   dict/map key exposed or read (API fields, wire keys)
    config_key    YAML/TF declaration or reader key
    schema_column SQL column or DB-row read
  (API response fields and wire keys share mapping_key deliberately: no
  mechanical distinction exists at the access-pattern level. Contract
  identity comes from subject binding, not namespace splitting.)
- callable_signature {subject, required[], optional[]}
- callsite_args {subject, callee, positional, keywords[]}
- migration_effect {subject, removed_columns[], added_columns[]}

Subject identity {file, symbol} binds every fact to its source side.
Validators require subject files to equal the proposed evidence pair, so
spliced cross-contract facts cannot prove.

Python is AST-backed. Other languages use bounded-pattern extractors over
fixture-grade grammar; each pattern has exact-behaviour tests. Namespace
channels are syntactic idioms (row[...] -> schema_column,
cfg|config|settings[...] -> config_key, else mapping_key), documented and
adversarially tested.
"""

from __future__ import annotations

import ast
import re

from codatiel_i2.common.hashing import short_id
from codatiel_i2.common.jsonio import canonical_json

DETECTOR_ID = "contract"
EXTRACTOR_VERSION = "v1"

NAMESPACES = ("mapping_key", "config_key", "schema_column")

_ROW_BASES = ("row", "record")
_CFG_BASES = ("cfg", "config", "settings")


# ------------------------------------------------------------------ facts

def _fact(proof_class: str, fact_kind: str, path: str, line_start: int,
          line_end: int, payload: dict, revision: str = "",
          revision_sha: str = "", evidence_ids: list | None = None) -> dict:
    evidence_ids = sorted(evidence_ids or [])
    extractor_id, extractor_version = _extractor_for(proof_class, fact_kind)
    fid = "fact_" + short_id(DETECTOR_ID, proof_class, fact_kind, path,
                             str(line_start), str(line_end), revision,
                             revision_sha, extractor_id, extractor_version,
                             canonical_json(payload), ",".join(evidence_ids))
    return {"fact_id": fid, "detector_id": DETECTOR_ID,
            "proof_class": proof_class, "fact_kind": fact_kind,
            "path": path, "line_start": line_start, "line_end": line_end,
            "revision": revision, "revision_sha": revision_sha,
            "evidence_ids": evidence_ids,
            "extractor_id": extractor_id,
            "extractor_version": extractor_version, **payload}


def _extractor_for(proof_class: str, fact_kind: str) -> tuple[str, str]:
    table = {
        ("contract_names", "provided_names"): "contract.code_names.v1",
        ("contract_names", "required_names"): "contract.code_names.v1",
        ("contract_schema", "provided_names"): "contract.schema.v1",
        ("contract_config", "provided_names"): "contract.config.v1",
        ("contract_config", "required_names"): "contract.config.v1",
        ("contract_signature", "callable_signature"): "contract.signatures.v1",
        ("contract_signature", "callsite_args"): "contract.signatures.v1",
        ("contract_migration", "migration_effect"): "contract.migration.v1",
        ("contract_sem_value", "value_contract"): "contract.sem_value.v1",
        ("contract_sem_allowed", "allowed_values"): "contract.sem_allowed.v1",
        ("contract_sem_null", "nullability"): "contract.sem_null.v1",
        ("contract_sem_unit", "unit_contract"): "contract.sem_unit.v1",
        ("contract_sem_error", "error_contract"): "contract.sem_error.v1",
    }
    name = table.get((proof_class, fact_kind), "")
    return (name, EXTRACTOR_VERSION) if name else ("", "")


def bind_fact(fact: dict, revision: str, revision_sha: str,
              evidence: list[dict]) -> dict:
    ids = sorted(e["evidence_id"] for e in evidence
                 if e.get("path") == fact.get("path")
                 and e.get("revision") == revision
                 and e.get("evidence_id"))
    payload = {k: v for k, v in fact.items()
               if k not in ("fact_id", "detector_id", "proof_class",
                            "fact_kind", "path", "line_start", "line_end",
                            "revision", "revision_sha", "evidence_ids",
                            "extractor_id", "extractor_version")}
    return _fact(fact.get("proof_class", ""), fact.get("fact_kind", ""),
                 fact.get("path", ""), fact.get("line_start", 1),
                 fact.get("line_end", 1), payload, revision, revision_sha,
                 ids)


def bind_facts(facts_list: list[dict], path: str, revision: str,
               revision_sha: str, evidence: list[dict]) -> list[dict]:
    return [bind_fact(f, revision, revision_sha, evidence)
            for f in facts_list if f.get("path") == path]


# ------------------------------------------------------- shared helpers

def _provided(proof_class: str, path: str, namespace: str, symbol: str,
              names: list[str], ls: int, le: int) -> dict:
    assert namespace in NAMESPACES, namespace
    return _fact(proof_class, "provided_names", path, ls, le,
                 {"namespace": namespace,
                  "subject": {"file": path, "symbol": symbol},
                  "names": sorted(set(names))})


def _required(proof_class: str, path: str, namespace: str, symbol: str,
              names: list[str], ls: int, le: int) -> dict:
    assert namespace in NAMESPACES, namespace
    return _fact(proof_class, "required_names", path, ls, le,
                 {"namespace": namespace,
                  "subject": {"file": path, "symbol": symbol},
                  "names": sorted(set(names))})


def _lineno(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _channel(base: str | None) -> str:
    if base in _ROW_BASES:
        return "schema_column"
    if base in _CFG_BASES:
        return "config_key"
    return "mapping_key"


# ------------------------------------------------------- python (AST)

_PY_IGNORE_CALLS = frozenset(
    "render store lookup int str len json dumps loads range isinstance "
    "open write read onboard ignore run _run _load _lookup _get _ok "
    "_absent get load".split())


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.prov: list[dict] = []
        self.req: list[dict] = []
        self.sigs: list[dict] = []
        self.calls: list[dict] = []
        self._func: list[str] = ["module"]

    def visit_FunctionDef(self, node):
        self._func.append(node.name)
        args = node.args
        pos = list(args.posonlyargs) + list(args.args)
        ndef = len(args.defaults)
        required = [a.arg for a in pos[:len(pos) - ndef]] if ndef else [a.arg for a in pos]
        optional = [a.arg for a in pos[len(pos) - ndef:]] if ndef else []
        optional += [a.arg for a in args.kwonlyargs]
        self.sigs.append(_fact(
            "contract_signature", "callable_signature", self.path,
            node.lineno, node.end_lineno or node.lineno,
            {"subject": {"file": self.path, "symbol": node.name},
             "required": required, "optional": optional}))
        self.generic_visit(node)
        self._func.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Dict(self, node):
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if keys:
            self.prov.append(_provided(
                "contract_names", self.path, "mapping_key", self._func[-1],
                keys, node.lineno, node.end_lineno or node.lineno))
        self.generic_visit(node)

    def visit_Subscript(self, node):
        base = node.value.id if isinstance(node.value, ast.Name) else None
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            self.req.append(_required(
                "contract_names", self.path, _channel(base), self._func[-1],
                [sl.value], node.lineno, node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        callee = None
        if isinstance(f, ast.Name):
            callee = f.id
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            callee = f.attr
        if callee and callee not in _PY_IGNORE_CALLS and not callee.startswith("_"):
            self.calls.append(_fact(
                "contract_signature", "callsite_args", self.path,
                node.lineno, node.end_lineno or node.lineno,
                {"subject": {"file": self.path, "symbol": self._func[-1]},
                 "callee": callee, "positional": len(node.args),
                 "keywords": sorted(kw.arg for kw in node.keywords if kw.arg)}))
        self.generic_visit(node)


def _extract_py(path: str, text: str) -> tuple[list, list, list, list]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], [], [], []
    vis = _PyVisitor(path)
    vis.visit(tree)
    return vis.prov, vis.req, vis.sigs, vis.calls


# ------------------------------------------- C-like bounded patterns

_SIG_RES = {
    "go": re.compile(r"^func\s+(?:\([^)]*\)\s+)?([A-Za-z_][\w]*)\s*\(([^)]*)\)", re.M),
    "typescript": re.compile(r"function\s+([A-Za-z_][\w]*)\s*\(([^)]*)\)"),
    "swift": re.compile(r"func\s+([A-Za-z_][\w]*)\s*\(([^)]*)\)"),
    "rust": re.compile(r"fn\s+([A-Za-z_][\w]*)\s*\(([^)]*)\)"),
}

_CALL_RES = {
    "go": re.compile(r"(?<![\w.])([A-Z][\w]*)\s*\("),
    "typescript": re.compile(r"(?<![\w.])([a-zA-Z_][\w]*)\s*\("),
    "swift": re.compile(r"(?<![\w.:])([a-zA-Z_][\w]*)\s*\("),
    "rust": re.compile(r"(?<![\w.:])([a-z_][\w]*)\s*(?:::<[^>]*>)?\s*\("),
}

_DEF_KW = frozenset(
    "func function fn return if for while switch map append len make panic".split())


def _split_top_commas(s: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _sig_params(lang: str, raw: str) -> tuple[list, list]:
    required, optional = [], []
    for p in _split_top_commas(raw):
        p = p.strip()
        if not p:
            continue
        if lang == "go":
            name = p.split()[0].strip(" *")
            (optional if "..." in p else required).append(name)
        elif lang == "typescript":
            m = re.match(r"([A-Za-z_][\w]*)", p)
            if not m:
                continue
            (optional if ("?" in p.split(":")[0] or "=" in p)
             else required).append(m.group(1))
        elif lang == "swift":
            toks = p.split()
            name = toks[-2] if len(toks) >= 2 and "=" in p else toks[-1]
            name = re.sub(r"^_+", "", name.strip(" :"))
            m = re.match(r"([A-Za-z_][\w]*)", name)
            if not m:
                continue
            (optional if "=" in p else required).append(m.group(1))
        elif lang == "rust":
            m = re.match(r"([A-Za-z_][\w]*)", p)
            if m and m.group(1) != "self":
                required.append(m.group(1))
    return required, optional


def _extract_c(path: str, text: str, lang: str) -> tuple[list, list, list, list]:
    prov, req, sigs, calls = [], [], [], []
    for m in re.finditer(r'["\']([A-Za-z_][\w]*)["\']\s*:', text):
        ln = _lineno(text, m.start())
        prov.append(_provided("contract_names", path, "mapping_key",
                             "module", [m.group(1)], ln, ln))
    if lang == "typescript":
        # Bare object-literal keys ({ id: …, displayName: … }). Bounded to
        # `{`/`,`-led positions so signatures, ternaries, and return-type
        # annotations do not match.
        for m in re.finditer(r"[{,]\s*([A-Za-z_][\w]*)\s*:", text):
            ln = _lineno(text, m.start())
            prov.append(_provided("contract_names", path, "mapping_key",
                                 "module", [m.group(1)], ln, ln))
    for m in re.finditer(r'\[\s*["\']([A-Za-z_][\w]*)["\']\s*:', text):
        ln = _lineno(text, m.start())
        prov.append(_provided("contract_names", path, "mapping_key",
                             "module", [m.group(1)], ln, ln))
    # Index reads base["key"], including call-result indexing f(...)["key"].
    for m in re.finditer(
            r"(?:([A-Za-z_][\w]*)|\))\s*\[\s*[\"']([A-Za-z_][\w]*)[\"']\s*\]",
            text):
        base, key = m.group(1), m.group(2)
        ln = _lineno(text, m.start())
        req.append(_required("contract_names", path, _channel(base),
                            "module", [key], ln, ln))
    # Column-mapping positions: string literals that ARE the mapping
    # (return "col" / const X = "col"). Namespace schema_column; pairing
    # is strict by evidence pair so other lanes never see these.
    for m in re.finditer(r"\breturn\s+[\"']([A-Za-z_][\w]*)[\"']", text):
        ln = _lineno(text, m.start())
        req.append(_required("contract_names", path, "schema_column",
                            "module", [m.group(1)], ln, ln))
    for m in re.finditer(
            r"(?:^|\n)\s*(?:export\s+)?(?:pub(?:\([^)]*\))?\s+)?"
            r"(?:const|let|var)\s+[A-Za-z_][\w]*"
            r"\s*(?::\s*[^=;\n]+)?=\s*[\"']([A-Za-z_][\w]*)[\"']", text):
        ln = _lineno(text, m.start())
        req.append(_required("contract_names", path, "schema_column",
                            "module", [m.group(1)], ln, ln))
    for m in _SIG_RES[lang].finditer(text):
        name, raw = m.group(1), m.group(2)
        required, optional = _sig_params(lang, raw)
        ln = _lineno(text, m.start())
        sigs.append(_fact("contract_signature", "callable_signature", path,
                           ln, ln,
                           {"subject": {"file": path, "symbol": name},
                            "required": required, "optional": optional}))
    defined = {s["subject"]["symbol"] for s in sigs}
    # Callsites: record every non-keyword call. Callee resolution across
    # the evidence pair is the pipeline's job (import-aware); filtering to
    # same-file definitions here would drop all cross-file contract calls.
    # Unmatched callees never pair, so noise cannot prove.
    for m in _CALL_RES[lang].finditer(text):
        name = m.group(1)
        if name in _DEF_KW:
            continue
        ln = _lineno(text, m.start())
        start, depth, pos = m.end(), 1, m.end()
        while pos < len(text) and depth:
            if text[pos] == "(":
                depth += 1
            elif text[pos] == ")":
                depth -= 1
            pos += 1
        parts = _split_top_commas(text[start:pos - 1])
        kws, positional = [], 0
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if lang == "swift" and re.match(r"[A-Za-z_][\w]*\s*:", p):
                kws.append(p.split(":")[0].strip())
            else:
                positional += 1
        calls.append(_fact("contract_signature", "callsite_args", path, ln,
                           ln,
                           {"subject": {"file": path, "symbol": "module"},
                            "callee": name, "positional": positional,
                            "keywords": sorted(kws)}))
    return prov, req, sigs, calls


# ------------------------------------------- schema / config / migration

def extract_schema(path: str, lines: list[str]) -> list[dict]:
    text = "\n".join(lines)
    out = []
    for m in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)\s*\((.*?)\);",
            text, re.S | re.I):
        table, body = m.group(1), m.group(2)
        cols = []
        for part in _split_top_commas(body):
            cm = re.match(r"\s*([A-Za-z_][\w]*)", part)
            if cm and cm.group(1).upper() not in (
                    "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                cols.append(cm.group(1))
        ln = _lineno(text, m.start())
        out.append(_provided("contract_schema", path, "schema_column",
                             table, cols, ln, ln))
    return _merge_name_facts(out)


def extract_config(path: str, lines: list[str]) -> list[dict]:
    text = "\n".join(lines)
    out = []
    if path.endswith(".yaml"):
        for i, line in enumerate(lines, 1):
            m = re.match(r"^([A-Za-z_][\w]*)\s*:", line)
            if m:
                out.append(_provided("contract_config", path, "config_key",
                                     "config", [m.group(1)], i, i))
    elif path.endswith(".tf"):
        for m in re.finditer(r'variable\s+"([A-Za-z_][\w]*)"', text):
            ln = _lineno(text, m.start())
            out.append(_provided("contract_config", path, "config_key",
                                 "config", [m.group(1)], ln, ln))
        for m in re.finditer(r"var\.([A-Za-z_][\w]*)", text):
            ln = _lineno(text, m.start())
            out.append(_required("contract_config", path, "config_key",
                                 "config", [m.group(1)], ln, ln))
    return _merge_name_facts(out)


def extract_migration(path: str, lines: list[str]) -> list[dict]:
    text = "\n".join(lines)
    removed = re.findall(r"DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w]*)",
                         text, re.I)
    added = re.findall(r"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)",
                       text, re.I)
    # Always emit: the BASE side of the counterfactual needs a replayable
    # fact even when the migration removes nothing.
    return [_fact("contract_migration", "migration_effect", path, 1,
                  len(lines),
                  {"subject": {"file": path, "symbol": path.split("/")[-1]},
                   "removed_columns": sorted(set(removed)),
                   "added_columns": sorted(set(added))})]


# ------------------------------------------------------------------ entry

def _merge_name_facts(facts: list[dict]) -> list[dict]:
    """Aggregate same-(file, symbol, namespace) name facts into one fact
    per side. Without this, a producer exposing {a, b} as two one-name
    facts lets a consumer requiring {a} 'miss' b — a spurious finding.
    Aliases and multi-key payloads require union semantics."""
    groups: dict[tuple, list[dict]] = {}
    for f in facts:
        if f.get("fact_kind") in ("provided_names", "required_names"):
            key = (f["proof_class"], f["fact_kind"], f["path"],
                   f["namespace"], canonical_json(f["subject"]))
            groups.setdefault(key, []).append(f)
    merged: list[dict] = []
    seen: set[int] = set()
    for f in facts:
        if f.get("fact_kind") not in ("provided_names", "required_names"):
            merged.append(f)
            continue
        key = (f["proof_class"], f["fact_kind"], f["path"],
               f["namespace"], canonical_json(f["subject"]))
        if id(f) in seen:
            continue
        members = groups[key]
        for m in members:
            seen.add(id(m))
        names = sorted({n for m in members for n in m["names"]})
        ls = min(m["line_start"] for m in members)
        le = max(m["line_end"] for m in members)
        mk = _provided if f["fact_kind"] == "provided_names" else _required
        merged.append(mk(f["proof_class"], f["path"], f["namespace"],
                         f["subject"]["symbol"], names, ls, le))
    return merged

def extract_code_names(path: str, lines: list[str]) -> list[dict]:
    text = "\n".join(lines)
    if path.endswith(".py"):
        prov, req, _, _ = _extract_py(path, text)
    elif path.endswith((".go", ".ts", ".swift", ".rs")):
        lang = {"go": "go", "ts": "typescript", "swift": "swift",
                "rs": "rust"}[path.rsplit(".", 1)[-1]]
        prov, req, _, _ = _extract_c(path, text, lang)
    else:
        return []
    return _merge_name_facts(prov + req)


def extract_signatures(path: str, lines: list[str]) -> list[dict]:
    text = "\n".join(lines)
    if path.endswith(".py"):
        _, _, sigs, calls = _extract_py(path, text)
    elif path.endswith((".go", ".ts", ".swift", ".rs")):
        lang = {"go": "go", "ts": "typescript", "swift": "swift",
                "rs": "rust"}[path.rsplit(".", 1)[-1]]
        _, _, sigs, calls = _extract_c(path, text, lang)
    else:
        return []
    return sigs + calls


EXTRACTORS: dict[str, object] = {}


def _register_extractors() -> None:
    from . import semfacts as _S
    EXTRACTORS.update({
        "contract.code_names.v1": extract_code_names,
        "contract.signatures.v1": extract_signatures,
        "contract.schema.v1": extract_schema,
        "contract.config.v1": extract_config,
        "contract.migration.v1": extract_migration,
        "contract.sem_value.v1": _S.extract_sem_value,
        "contract.sem_allowed.v1": _S.extract_sem_allowed,
        "contract.sem_null.v1": _S.extract_sem_null,
        "contract.sem_unit.v1": _S.extract_sem_unit,
        "contract.sem_error.v1": _S.extract_sem_error,
    })


_register_extractors()


def replay_fact(fact: dict, evidence_by_id: dict, *,
                base_checkout=None, head_checkout=None) -> dict:
    """B6-strength fact replay for contract facts (C3.5 steps 1-8)."""
    from pathlib import Path

    from codatiel_i2.core.evidence import (
        checkout_holds_sha, replay_source_evidence, validate_envelope)

    extractor_id = fact.get("extractor_id", "")
    fn = EXTRACTORS.get(extractor_id)
    if fn is None:
        return {"ok": False, "errors": [f"unknown extractor {extractor_id!r}"]}
    revision = fact.get("revision", "")
    checkout = {"base": base_checkout, "head": head_checkout}.get(revision)
    if checkout is None:
        return {"ok": False,
                "errors": [f"no {revision} checkout for fact replay"]}
    checkout = Path(checkout)
    revision_sha = fact.get("revision_sha", "")
    if not checkout_holds_sha(checkout, revision_sha):
        return {"ok": False,
                "errors": ["checkout does not hold fact revision_sha"]}
    errors: list[str] = []
    if not fact.get("evidence_ids"):
        return {"ok": False, "errors": ["fact cites no evidence"]}
    envelopes: list[dict] = []
    for eid in fact.get("evidence_ids", []):
        env = evidence_by_id.get(eid)
        if env is None:
            return {"ok": False,
                    "errors": [f"fact evidence {eid!r} does not resolve"]}
        if env.get("revision") != revision or \
                env.get("revision_sha") != revision_sha:
            return {"ok": False,
                    "errors": [f"fact evidence {eid!r} revision mismatch"]}
        if env.get("path") != fact.get("path"):
            return {"ok": False,
                    "errors": [f"fact evidence {eid!r} path mismatch"]}
        errors.extend(validate_envelope(env))
        errors.extend(replay_source_evidence(
            env, base_checkout=base_checkout, head_checkout=head_checkout))
        envelopes.append(env)
    if errors:
        return {"ok": False, "errors": errors}
    try:
        target = checkout / fact.get("path", "")
        lines = target.read_text(encoding="utf-8",
                                 errors="replace").splitlines()
    except Exception as exc:
        return {"ok": False, "errors": [f"cannot re-read fact file: {exc}"]}
    recomputed = fn(fact.get("path", ""), lines)
    for candidate in recomputed:
        bound = bind_fact(candidate, revision, revision_sha, envelopes)
        mine = {k: v for k, v in fact.items() if k != "fact_id"}
        theirs = {k: v for k, v in bound.items() if k != "fact_id"}
        if mine == theirs:
            if bound["fact_id"] != fact.get("fact_id"):
                return {"ok": False, "errors": ["recomputed fact_id mismatch"]}
            return {"ok": True, "fact": bound}
    return {"ok": False,
            "errors": ["persisted fact not present in re-extracted bytes"]}
