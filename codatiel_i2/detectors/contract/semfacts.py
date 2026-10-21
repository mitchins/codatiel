"""C4.1 semantic fact extractors, part 1: value/type + allowed values.

Observable contract properties only; no compatibility verdicts. Python
via ast; other languages via bounded patterns with exact-behaviour tests.
"""

from __future__ import annotations

import ast
import re


def _mk(proof_class: str, path: str, symbol: str, payload: dict,
        ls: int, le: int) -> dict:
    from .facts import _fact
    payload = {"subject": {"file": path, "symbol": symbol}, **payload}
    return _fact(proof_class, _FACT_KIND[proof_class], path, ls, le,
                 payload)


def _lineno(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1

_IDS = {
    "contract_sem_value": "contract.sem_value.v1",
    "contract_sem_allowed": "contract.sem_allowed.v1",
    "contract_sem_null": "contract.sem_null.v1",
    "contract_sem_unit": "contract.sem_unit.v1",
    "contract_sem_error": "contract.sem_error.v1",
}

_FACT_KIND = {
    "contract_sem_value": "value_contract",
    "contract_sem_allowed": "allowed_values",
    "contract_sem_null": "nullability",
    "contract_sem_unit": "unit_contract",
    "contract_sem_error": "error_contract",
}


def _mk(proof_class: str, path: str, symbol: str, payload: dict,
        ls: int, le: int) -> dict:
    from .facts import _fact
    payload = {"subject": {"file": path, "symbol": symbol}, **payload}
    return _fact(proof_class, _FACT_KIND[proof_class], path, ls, le,
                 payload)


def _py_value_type(node) -> str:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in ("str", "String"):
            return "string"
        if node.func.id in ("int", "float", "Number"):
            return "integer"
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return "string"
        if isinstance(node.value, (int, float)):
            return "integer"
        return "unknown"
    if isinstance(node, ast.BinOp):
        return "integer"
    return "unknown"


def _extract_py_value(path: str, text: str) -> list[dict]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in node.body:
            if isinstance(sub, ast.Return) and sub.value is not None:
                out.append(_mk(
                    "contract_sem_value", path, node.name,
                    {"role": "producer",
                     "value_type": _py_value_type(sub.value),
                     "coercion": "none", "operation": "none"},
                    sub.lineno, sub.lineno))
        dump = ast.dump(node)
        if "Add" in dump:
            if "int" in dump or "Int" in dump:
                op, co = "parse_then_add", "parse_integer"
            else:
                op, co = "numeric_addition", "none"
            out.append(_mk(
                "contract_sem_value", path, node.name,
                {"role": "consumer", "value_type": "unknown",
                 "coercion": co, "operation": op},
                node.lineno, node.lineno))
        if "JoinedStr" in dump or "to_string" in dump or "str(" in dump:
            out.append(_mk(
                "contract_sem_value", path, node.name,
                {"role": "consumer", "value_type": "unknown",
                 "coercion": "to_string", "operation": "string_concat"},
                node.lineno, node.lineno))
    return out


def _c_value_type(expr: str) -> str:
    e = expr.strip()
    if re.match(r"(?i)^(str|string|String|strconv\.Itoa|itoa)\s*\(", e):
        return "string"
    if re.match(r"(?i)^(int|Number|Int)\s*\(", e):
        return "integer"
    if re.match(r"^[\w.]+\.length$|^[\w.]+\.count$|^len\s*\(", e):
        return "integer"
    if re.search(r"[+\-*/]", e) and re.search(
            r"\.length|\.count|\blen\s*\(|\bcount\b", e):
        return "integer"
    if e.startswith('"') or e.startswith("'"):
        return "string"
    return "unknown"


def _extract_c_value(path: str, text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"\breturn\s+([^;}\n]+)", text):
        expr = m.group(1).strip().rstrip(";")
        ln = _lineno(text, m.start())
        vt = _c_value_type(expr)
        if vt != "unknown":
            out.append(_mk(
                "contract_sem_value", path, "module",
                {"role": "producer", "value_type": vt,
                 "coercion": "none", "operation": "none"}, ln, ln))
    for m in re.finditer(r"(?:=|\breturn)\s*([^;\n]*\+[^;\n]*)", text):
        expr = m.group(1)
        ln = _lineno(text, m.start())
        if re.search(r"\b(int|Number|Int|mustInt)\s*\(", expr):
            op, co = "parse_then_add", "parse_integer"
        elif '"' in expr or "'" in expr:
            op, co = "string_concat", "to_string"
        else:
            op, co = "numeric_addition", "none"
        out.append(_mk(
            "contract_sem_value", path, "module",
            {"role": "consumer", "value_type": "unknown",
             "coercion": co, "operation": op}, ln, ln))
    return out


def extract_sem_value(path: str, lines: list[str]) -> list[dict]:
    text = "\n".join(lines)
    if path.endswith(".py"):
        return _extract_py_value(path, text)
    if path.rsplit(".", 1)[-1] in ("go", "ts", "swift", "rs"):
        return _extract_c_value(path, text)
    return []


def extract_sem_allowed(path: str, lines: list[str]) -> list[dict]:
    if path.rsplit(".", 1)[-1] not in ("py", "go", "ts", "swift", "rs"):
        return []
    text = "\n".join(lines)
    out = []
    for m in re.finditer(
            r'["\']?(kind|status)["\']?\s*[:=]\s*["\']([A-Za-z_][\w]*)["\']',
            text):
        out.append(_mk(
            "contract_sem_allowed", path, "module",
            {"role": "emitted", "key": m.group(1), "values": [m.group(2)]},
            _lineno(text, m.start()), _lineno(text, m.start())))
    for m in re.finditer(
            r"\b(write|suspend|save\w*)\s*\([^()]*,\s*"
            r"['\"]([A-Za-z_][\w]*)['\"]", text, re.I):
        out.append(_mk(
            "contract_sem_allowed", path, "module",
            {"role": "emitted", "key": "status", "values": [m.group(2)]},
            _lineno(text, m.start()), _lineno(text, m.start())))
    for m in re.finditer(
            r"(?:==|===|!=|!==)\s*['\"]([A-Za-z_][\w]*)['\"]", text):
        out.append(_mk(
            "contract_sem_allowed", path, "module",
            {"role": "accepted", "key": "", "values": [m.group(1)]},
            _lineno(text, m.start()), _lineno(text, m.start())))
    for m in re.finditer(
            r"['\"]([A-Za-z_][\w]*)['\"]\s*(?:==|===|!=|!==)", text):
        out.append(_mk(
            "contract_sem_allowed", path, "module",
            {"role": "accepted", "key": "", "values": [m.group(1)]},
            _lineno(text, m.start()), _lineno(text, m.start())))
    if re.search(r"\belse\b|\bdefault\b", text):
        out.append(_mk(
            "contract_sem_allowed", path, "module",
            {"role": "accepted", "key": "", "values": ["<default-arm>"]},
            1, 1))
    return out


def _split_top(body: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def extract_sem_null(path: str, lines: list[str]) -> list[dict]:
    text = "\n".join(lines)
    out = []
    if path.endswith(".sql"):
        for m in re.finditer(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                r"([A-Za-z_][\w]*)\s*\((.*?)\);", text, re.S | re.I):
            for part in _split_top(m.group(2)):
                cm = re.match(r"\s*([A-Za-z_][\w]*)\s+(.*)", part, re.S)
                if not cm or cm.group(1).upper() in (
                        "PRIMARY", "FOREIGN", "UNIQUE", "CHECK",
                        "CONSTRAINT"):
                    continue
                col, rest = cm.group(1), cm.group(2).upper()
                out.append(_mk(
                    "contract_sem_null", path, m.group(1),
                    {"role": "schema", "column": col,
                     "nullable": "NOT NULL" not in rest,
                     "handling": "none"}, _lineno(text, m.start()),
                    _lineno(text, m.start())))
        return out
    if path.rsplit(".", 1)[-1] in ("py", "go", "ts", "swift", "rs"):
        for m in re.finditer(
                r"(?:\brow\b|\brecord\b)\s*\[\s*[\"']([A-Za-z_][\w]*)[\"']",
                text):
            col = m.group(1)
            ln = _lineno(text, m.start())
            window = "\n".join(lines[max(0, ln - 1):ln + 1])
            if 'or ""' in window or "??" in window or "or ''" in window:
                handling = "fallback"
            elif "!!" in window or re.search(r"\)\s*!(?!=)", window):
                handling = "force_unwrap"
            else:
                handling = "direct"
            out.append(_mk(
                "contract_sem_null", path, "module",
                {"role": "consumer", "column": col, "nullable": None,
                 "handling": handling}, ln, ln))
    return out


def extract_sem_unit(path: str, lines: list[str]) -> list[dict]:
    text = "\n".join(lines)
    out = []
    if path.endswith((".yaml", ".yml")):
        for i, line in enumerate(lines, 1):
            m = re.match(r"^([A-Za-z_][\w]*)\s*:\s*([0-9]+)\s*(?:#\s*(.*))?$",
                         line)
            if m:
                unit = (m.group(3) or "").strip().lower() or "unstated"
                out.append(_mk(
                    "contract_sem_unit", path, "config",
                    {"role": "declaration", "name": m.group(1),
                     "value": int(m.group(2)), "unit_annotation": unit,
                     "conversion": "none"}, i, i))
        return out
    if path.rsplit(".", 1)[-1] in ("py", "go", "ts", "rs"):
        for m in re.finditer(r"cfg\[\"([A-Za-z_][\w]*)\"\]", text):
            name = m.group(1)
            ln = _lineno(text, m.start())
            window = "\n".join(lines[max(0, ln - 1):ln + 2])
            if re.search(r"/\s*1000", window):
                conv = "divides_by_1000"
            elif re.search(r"\*\s*1000", window):
                conv = "multiplies_by_1000"
            else:
                conv = "none"
            out.append(_mk(
                "contract_sem_unit", path, "module",
                {"role": "consumer", "name": name, "value": None,
                 "unit_annotation": "unstated", "conversion": conv},
                ln, ln))
    return out


def extract_sem_error(path: str, lines: list[str]) -> list[dict]:
    if path.rsplit(".", 1)[-1] not in ("py", "go", "ts", "swift", "rs"):
        return []
    text = "\n".join(lines)
    out = []
    errkeys: dict[int, set[str]] = {}
    for m in re.finditer(
            r'["\']?(code|error|message|detail)["\']?\s*:|=',
            text.replace("==", "\x00").replace("===", "\x00")):
        errkeys.setdefault(_lineno(text, m.start()), set()).add(m.group(1))
    for m in re.finditer(r"(?:==|===)\s*[\"']([A-Za-z_][\w]*)[\"']", text):
        ln = _lineno(text, m.start())
        window = "\n".join(lines[max(0, ln - 1):ln + 1])
        keys = sorted({k for lno, ks in errkeys.items()
                       for k in ks if abs(lno - ln) <= 3})
        if "code" in window or "error" in window:
            out.append(_mk(
                "contract_sem_error", path, "module",
                {"role": "consumer", "keys": keys, "codes": [m.group(1)]},
                ln, ln))
    prod = sorted({k for ks in errkeys.values() for k in ks})
    if prod:
        out.append(_mk(
            "contract_sem_error", path, "module",
            {"role": "producer", "keys": prod, "codes": []}, 1, 1))
    return out
