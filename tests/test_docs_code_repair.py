"""v0.1.1 canary repair: manufactured docs_code findings must not verify.

Frozen regression suite for the yt2wiki#15 pathology (268 findings, 2.64MB
email, all false). Every test is deterministic: pure extractors/validators,
scan_pair over tmp git fixtures, and the email builder over fake run dirs.
No network, no paid inference.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codatiel_i2.detectors.docs_code import candidates as cand_mod
from codatiel_i2.detectors.docs_code import detector as det_mod
from codatiel_i2.detectors.docs_code import facts as facts_mod
from codatiel_i2.detectors.docs_code import validators as validators_mod


# ---------------------------------------------------------------- extractors

def test_dict_get_is_not_an_http_route():
    lines = ['entry.get("job_id")', 'cfg = providers.get("x", {})']
    assert facts_mod.extract_code_interfaces("a.py", lines) == []


def _routes(lines):
    return {(f["interface"]["method"], f["interface"]["path"])
            for f in facts_mod.extract_code_interfaces("a.py", lines)
            if f["interface"].get("kind") == "http_route"}


def test_config_get_with_slash_is_not_a_served_route():
    # Receiver discipline: an ordinary call on a non-registrar receiver is
    # data access, even when the string looks like a path.
    assert _routes(['config.get("/library")']) == set()
    assert _routes(['client.get("/api/auth/state")']) == set()
    assert _routes(['view.get("article_id")']) == set()


def test_registrar_receivers_count_as_served():
    assert _routes(['app.get("/library")']) == {("GET", "/library")}
    assert _routes(['self.app.post("/add")']) == {("POST", "/add")}
    assert _routes(['router.delete("/old")']) == {("DELETE", "/old")}


def test_decorator_registration_counts_for_any_receiver():
    assert _routes(['@router.get("/items")']) == {("GET", "/items")}
    assert _routes(['@app.post("/add")']) == {("POST", "/add")}


def test_decorator_routes_require_leading_slash():
    lines = ['@app.get("/library")', '@app.post("revert")']
    got = facts_mod.extract_code_interfaces("a.py", lines)
    # '@app.get(' also matches the generic .get("...") pattern: both facts
    # describe the same served route; the pair-level key collapses them.
    assert {(f["interface"]["method"], f["interface"]["path"]) for f in got} == {
        ("GET", "/library")}


def test_route_template_params_are_wildcards():
    assert facts_mod.normalize_route_template("/articles/{id}") == \
        facts_mod.normalize_route_template("/articles/{article_id}")


def test_prose_nouns_are_not_contracts():
    lines = ["work with `YT2WIKI_HOME` set once via `export`), plus the full",
             "without importing `torch`/`transformers`",
             "`httpx` `MockTransport` returning real `RUBRIC_PROMPT` JSON"]
    got = facts_mod.extract_doc_names("docs/d.md", lines)
    names = {f["name"] for f in got}
    assert "export" not in names and "torch" not in names
    assert "httpx" not in names and "MockTransport" not in names
    assert "YT2WIKI_HOME" in names  # UPPER + "set" verb: admitted


def test_negated_requirement_is_not_a_contract():
    lines = ["- Does not require `--gpus` or NVIDIA runtime to start"]
    assert facts_mod.extract_doc_names("docs/d.md", lines) == []


def test_no_foo_disclaims_provision():
    lines = ["Build from the wheel (no `PYTHONPATH`):"]
    assert facts_mod.extract_doc_names("docs/d.md", lines) == []


def test_verified_404_is_not_a_served_contract():
    lines = ["OpenAI `POST /v1/audio/transcriptions` (verified 404)."]
    got = facts_mod.extract_doc_interfaces("docs/d.md", lines)
    assert [f for f in got
            if f["interface"].get("kind") == "http_route"] == []


def test_os_environ_get_is_an_env_surface():
    lines = ['env_dir = os.environ.get("LLAMA_SERVER_DIR")',
             'x = os.environ["HF_HOME"]']
    names = {f["name"] for f in facts_mod.extract_code_names("a.py", lines)}
    assert {"LLAMA_SERVER_DIR", "HF_HOME"} <= names


def test_const_indirection_resolves_env_name():
    lines = ['PASSWORD_ENV = "YT2WIKI_OPERATOR_PASSWORD"',
             'pw = os.environ.get(PASSWORD_ENV)']
    names = {f["name"] for f in facts_mod.extract_code_names("a.py", lines)}
    assert "YT2WIKI_OPERATOR_PASSWORD" in names


def test_dynamic_env_read_detected():
    assert facts_mod._dynamic_env_read('raw = os.environ.get(name)')
    assert not facts_mod._dynamic_env_read('x = os.environ.get("LIT")')


def test_doc_params_mark_positional_vs_keyword():
    params = facts_mod._parse_params("", "Settings.load(data_dir=...)")
    assert params and params[0]["name"] == "Settings"
    assert params[0]["keyword"] is False  # nested '=' is not keyword passing
    params2 = facts_mod._parse_params("", "a, Timeout=5")
    assert [p["keyword"] for p in params2] == [False, True]


# ---------------------------------------------------------------- validators

def _doc_sig(symbol, params):
    return {"subject": symbol,
            "interface": {"kind": "call_signature", "symbol": symbol,
                          "parameters": params}}


def _p(name, required=True, keyword=False, kwonly=False):
    return {"name": name, "required": required, "keyword_only": kwonly,
            "keyword": keyword, "variadic": False}


def test_positional_case_difference_is_compatible():
    doc = {"kind": "call_signature", "symbol": "create_app",
           "parameters": [_p("Settings", required=False)]}
    code = {"kind": "call_signature", "symbol": "create_app",
            "parameters": [_p("settings", required=False)]}
    ok, _ = cand_mod.call_compatible(doc, code)
    assert ok


def test_missing_required_param_is_a_demonstrated_break():
    doc = {"kind": "call_signature", "symbol": "connect",
           "parameters": [_p("host", keyword=False)]}
    code = {"kind": "call_signature", "symbol": "connect",
            "parameters": [_p("host"), _p("port")]}
    ok, failure = cand_mod.call_compatible(doc, code)
    assert not ok and "TypeError" in failure and "port" in failure


def test_unexpected_keyword_is_a_demonstrated_break():
    doc = {"kind": "call_signature", "symbol": "fetch",
           "parameters": [_p("Timeout", keyword=True)]}
    code = {"kind": "call_signature", "symbol": "fetch",
            "parameters": [_p("timeout"), _p("retries", required=False)]}
    ok, failure = cand_mod.call_compatible(doc, code)
    assert not ok and "Timeout" in failure


def _sig(params, pos_var=False, kw_var=False):
    return {"kind": "call_signature", "symbol": "f",
            "parameters": params, "variadic_pos": pos_var,
            "variadic_kw": kw_var}


def _pp(name, required=True, keyword=False, kwonly=False):
    return {"name": name, "required": required, "keyword_only": kwonly,
            "keyword": keyword}


def test_star_args_absorbs_positionals():
    doc = _sig([_pp("a"), _pp("b")])
    code = _sig([], pos_var=True)
    ok, _ = cand_mod.call_compatible(doc, code)
    assert ok


def test_kwargs_does_not_absorb_positionals():
    doc = _sig([_pp("a"), _pp("b")])
    code = _sig([_pp("x")], kw_var=True)
    ok, failure = cand_mod.call_compatible(doc, code)
    assert not ok and "TypeError" in failure


def test_star_args_does_not_absorb_unexpected_keyword():
    doc = _sig([_pp("x", keyword=True), _pp("unexpected", keyword=True)])
    code = _sig([_pp("x")], pos_var=True)
    ok, failure = cand_mod.call_compatible(doc, code)
    assert not ok and "unexpected" in failure


def test_kwargs_absorbs_unexpected_keyword():
    doc = _sig([_pp("a"), _pp("unexpected", keyword=True)])
    code = _sig([_pp("x")], kw_var=True)
    ok, _ = cand_mod.call_compatible(doc, code)
    assert ok


def test_duplicate_positional_and_keyword_is_a_break():
    doc = _sig([_pp("a"), _pp("x", keyword=True)])
    code = _sig([_pp("x"), _pp("y")], kw_var=True)
    ok, failure = cand_mod.call_compatible(doc, code)
    assert not ok and "both positionally and by keyword" in failure


def test_unprovable_call_is_ambiguous_never_a_break():
    doc = {"kind": "call_signature", "symbol": "f", "parameters": None}
    code = _sig([_pp("x")])
    verdict, _ = cand_mod.call_compatible(doc, code)
    assert verdict is None
    res = validators_mod.validate_interface_contract_mismatch(
        {"fact_id": "d", "subject": "f", "interface": doc}, {"fact_id": "c",
         "subject": "f", "interface": code})
    assert res["survives"] is False and res["reason"] == "ambiguous_call"
    assert "TypeError" not in res.get("bad_outcome", "")


def test_reference_framed_name_is_not_a_contract():
    doc = {"name": "YT2WIKI_GEMMA_API_KEY",
           "claim": "api_key_env names an env var (YT2WIKI_GEMMA_API_KEY)"}
    formed = cand_mod.form_named_candidate(doc, [])
    assert formed == {"candidate": False, "reason": "reference_not_contract"}


def test_route_served_anywhere_is_matched():
    doc = {"fact_id": "fact_d", "subject": "/library",
           "interface": {"kind": "http_route", "method": "GET",
                         "path": "/library"}}
    routes = {("GET", "/library"), ("GET", "/other")}
    res = validators_mod.validate_interface_contract_mismatch(
        doc, None, implemented_routes=routes, route_count=len(routes))
    assert res == {**res, "survives": False} and res["reason"] == "matched_interface"


def test_template_param_names_do_not_matter():
    doc = {"fact_id": "fact_d", "subject": "/articles/{id}",
           "interface": {"kind": "http_route", "method": "GET",
                         "path": "/articles/{id}"}}
    routes = {("GET", "/articles/{}")}
    res = validators_mod.validate_interface_contract_mismatch(
        doc, None, implemented_routes=routes, route_count=1)
    assert res["survives"] is False


def test_shorthand_route_is_ambiguous_not_a_miss():
    doc = {"fact_id": "fact_d", "subject": "/revert",
           "interface": {"kind": "http_route", "method": "POST",
                         "path": "/revert"}}
    routes = {("POST", "/api/articles/{}/revert")}
    res = validators_mod.validate_interface_contract_mismatch(
        doc, None, implemented_routes=routes, route_count=1)
    assert res["survives"] is False and res["reason"] == "ambiguous_absence"


def test_truly_unserved_route_still_fires():
    doc = {"fact_id": "fact_d", "subject": "/gone",
           "interface": {"kind": "http_route", "method": "GET",
                         "path": "/gone"}}
    routes = {("GET", "/library")}
    res = validators_mod.validate_interface_contract_mismatch(
        doc, None, implemented_routes=routes, route_count=1)
    assert res["survives"] is True
    assert "not served by any of the 1 implemented routes" in res["detail"]


def test_dynamic_reads_make_static_absence_ambiguous():
    doc = {"name": "SOME_KEY", "claim": "Set SOME_KEY before start",
           "fact_id": "fact_x"}
    res = validators_mod.validate_named_contract_missing(
        doc, set(), set(), set(), direction="docs_changed",
        surface_enumerated=True, dynamic_reads=True)
    assert res["survives"] is False and res["reason"] == "ambiguous_absence"


# ---------------------------------------------------------------- scan-level

def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, check=True,
                         timeout=60)
    return out.stdout.strip()


@pytest.fixture()
def patho_repo(tmp_path: Path) -> dict:
    """Miniature of the yt2wiki pathology: doc routes served in app.py,
    a changed test file whose only 'routes' are dict.get() calls."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(
        'import os\nfrom fastapi import FastAPI\napp = FastAPI()\n'
        'HOME = os.environ.get("YT2WIKI_HOME")\n'
        '@app.get("/library")\ndef library():\n    return {}\n'
        'def create_app(settings=None):\n    return app\n')
    (root / "docs").mkdir()
    (root / "docs" / "deploy.md").write_text(
        "# deploy\n`GET /library` (200, shows article card).\n"
        "`create_app(Settings.load(data_dir=...))` exercises.\n"
        "Set `YT2WIKI_HOME` before start.\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "tests" / "test_x.py").write_text(
        "def test_a():\n    entry = {}\n"
        '    assert entry.get("job_id") is None\n')
    (root / "docs" / "deploy.md").write_text(
        (root / "docs" / "deploy.md").read_text()
        + "Run with `YT2WIKI_HOME` set via `export` once.\n")
    _git(root, "commit", "-qam", "head")
    head = _git(root, "rev-parse", "HEAD")
    return {"root": root, "base": base, "head": head}


def test_pathology_fixture_yields_no_findings(patho_repo: dict,
                                              tmp_path: Path):
    from codatiel_i2.core.workspace import prepare_local_workspace
    from codatiel_i2.core import diff as diff_mod
    fix = patho_repo
    ws = prepare_local_workspace(fix["root"], fix["base"], fix["head"],
                                 tmp_path / "work")
    assert ws.status == "ready"
    changes = diff_mod.changed_files(fix["root"], ws.base_sha, ws.head_sha)
    scan = det_mod.scan_pair(
        base_dir=ws.base_dir, head_dir=ws.head_dir, base_sha=ws.base_sha,
        head_sha=ws.head_sha, repo="t/patho", changes=changes)
    assert scan["findings"] == []
    assert scan["coverage"]["docs_code"]["duplicates_suppressed"] >= 0
    # The real relations still resolve honestly, not dropped silently.
    reasons = {(c["proof_class"], c.get("reason")) for c in scan["candidates"]}
    assert ("interface", "matched_interface") in reasons
    assert ("named_contract", "present_contract") in reasons or \
        ("interface", "compatible_call") in reasons


def test_genuine_route_miss_still_fires(tmp_path: Path):
    from codatiel_i2.core.workspace import prepare_local_workspace
    from codatiel_i2.core import diff as diff_mod
    root = tmp_path / "repo2"
    root.mkdir()
    (root / "app.py").write_text("@app.get('/a')\ndef a():\n    pass\n")
    (root / "docs").mkdir()
    (root / "docs" / "d.md").write_text("# d\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "docs" / "d.md").write_text("# d\n`GET /missing` serves zip.\n")
    _git(root, "commit", "-qam", "head")
    head = _git(root, "rev-parse", "HEAD")
    ws = prepare_local_workspace(root, base, head, tmp_path / "work2")
    assert ws.status == "ready"
    changes = diff_mod.changed_files(root, ws.base_sha, ws.head_sha)
    scan = det_mod.scan_pair(
        base_dir=ws.base_dir, head_dir=ws.head_dir, base_sha=ws.base_sha,
        head_sha=ws.head_sha, repo="t/miss", changes=changes)
    assert len(scan["findings"]) == 1
    detail = scan["findings"][0]["proof_result"]["detail"]
    assert "not served by any of the 1 implemented routes" in detail


def test_genuine_arity_break_still_fires():
    doc = {"fact_id": "fact_d", "subject": "connect",
           "interface": {"kind": "call_signature", "symbol": "connect",
                         "parameters": [{"name": "host", "required": True,
                                         "keyword_only": False,
                                         "keyword": False,
                                         "variadic": False}]}}
    code = dict(doc)
    code_if = {"kind": "call_signature", "symbol": "connect",
               "parameters": [{"name": "host", "required": True,
                               "keyword_only": False, "keyword": False,
                               "variadic": False},
                              {"name": "port", "required": True,
                               "keyword_only": False, "keyword": False,
                               "variadic": False}]}
    code = {"fact_id": "fact_c", "subject": "connect", "interface": code_if}
    res = validators_mod.validate_interface_contract_mismatch(doc, code)
    assert res["survives"] is True and "TypeError" in res["bad_outcome"]


# ---------------------------------------------------------------- email caps

def _write_run(tmp_path: Path, name: str, findings: list[dict],
               status: str = "complete") -> Path:
    run_dir = tmp_path / name
    run_dir.mkdir()
    base, head = "a" * 40, "b" * 40
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": name, "repo": "o/r", "pr_number": 1,
        "pr_url": "https://github.com/o/r/pull/1",
        "base_sha": base, "head_sha": head,
        "config": {"model": "luna:t"}}))
    (run_dir / "metrics.json").write_text(json.dumps({
        "status": status, "model": "luna:t", "tool_calls": 3,
        "spend": {"usd_estimate": 0.01, "accounting_status": "CONFIRMED"}}))
    (run_dir / "shadow_result.json").write_text(json.dumps({"status": status}))
    (run_dir / "findings.jsonl").write_text(
        "".join(json.dumps(f) + "\n" for f in findings))
    (run_dir / "evidence.jsonl").write_text("")
    return run_dir, {"repo": "o/r", "number": 1,
                     "url": "https://github.com/o/r/pull/1",
                     "base": base, "head": head}


def _verified(i: int, key: str | None = None) -> dict:
    return {"finding_id": f"finding_{i:04d}", "detector": "docs_code",
            "disposition": "verified_proof", "confidence": 0.9,
            "bad_outcome": f"bad {i}", "trigger": "t", "head_state": "h",
            "base_state": "b",
            "changed_anchor": {"path": "d.md", "line_start": 1,
                               "line_end": 2},
            "evidence_ids": [], **({"issue_key": key} if key else {})}


def test_email_caps_findings_dedupes_and_counts(tmp_path: Path):
    from codatiel_i2 import watcher_manager as M
    # 12 rows, 4 distinct issues: dedup collapses, no cap needed.
    findings = [_verified(i, key=f"route|GET|/p{i % 4}") for i in range(12)]
    run_dir, ident = _write_run(tmp_path, "cap", findings)
    _subj, body = M.build_email(ident, Path(run_dir))
    assert body.count("--- finding ") == 4
    assert "4 distinct" in body and "8 duplicate(s)" in body
    assert "OMITTED" not in body
    # 12 distinct issues: cap at 10, omitted counted explicitly.
    findings = [_verified(i, key=f"route|GET|/q{i}") for i in range(12)]
    run_dir, ident = _write_run(tmp_path, "cap2", findings)
    _subj, body = M.build_email(ident, Path(run_dir))
    assert body.count("--- finding ") == M.EMAIL_FINDING_CAP
    assert "OMITTED 2 additional distinct finding(s)" in body
    assert "12 total, 12 distinct" in body


def test_email_budget_never_silent(tmp_path: Path):
    from codatiel_i2 import watcher_manager as M
    big = _verified(1, key="route|GET|/big")
    big["bad_outcome"] = "x" * 200000
    run_dir, ident = _write_run(tmp_path, "big", [big])
    _subj, body = M.build_email(ident, Path(run_dir))
    assert len(body.encode()) <= M.EMAIL_BODY_BUDGET
    assert "OMITTED" in body and "findings.jsonl" in body


def test_zero_finding_email_is_legitimate_silence(tmp_path: Path):
    from codatiel_i2 import watcher_manager as M
    run_dir, ident = _write_run(tmp_path, "zero", [])
    _subj, body = M.build_email(ident, Path(run_dir))
    assert "NO MATERIAL FINDINGS" in body
    assert "--- finding " not in body


def test_oversized_silence_rationale_stays_within_budget(tmp_path: Path):
    from codatiel_i2 import watcher_manager as M
    run_dir, ident = _write_run(tmp_path, "bigrat", [], status="complete")
    shadow = json.loads((run_dir / "shadow_result.json").read_text())
    shadow["silence"] = {"summary": "yadda " * 40000}  # ~240KB
    (run_dir / "shadow_result.json").write_text(json.dumps(shadow))
    _subj, body = M.build_email(ident, Path(run_dir))
    assert len(body.encode()) <= M.EMAIL_BODY_BUDGET
    assert "NO MATERIAL FINDINGS" in body  # status preserved
    assert "repo: o/r  PR: #1" in body  # identity preserved
    assert "truncated to" in body and "shadow_result.json" in body


def test_oversized_failure_diagnostic_stays_within_budget(tmp_path: Path):
    from codatiel_i2 import watcher_manager as M
    run_dir, ident = _write_run(tmp_path, "bigfail", [], status="failed")
    shadow = json.loads((run_dir / "shadow_result.json").read_text())
    shadow["error"] = "boom\n" * 30000  # ~150KB
    (run_dir / "shadow_result.json").write_text(json.dumps(shadow))
    _subj, body = M.build_email(ident, Path(run_dir))
    assert len(body.encode()) <= M.EMAIL_BODY_BUDGET
    assert "REVIEW FAILED" in body  # status preserved
    assert "repo: o/r  PR: #1" in body  # identity preserved
    assert "truncated to" in body
