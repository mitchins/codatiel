"""SEMDUP-06: sceptical editor unit tests. No inference, no network."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from codatiel_i2.semdup_dev import editor as ED


FARE_FILE = '''"""Zone fares (table implementation)."""

_ZONE_FARES = {1: 2.50, 2: 3.75, 3: 4.50, 4: 6.25}  # zone 3 summer promo


def zone_fare_lookup(zone):
    """Return the fare for zones 1-4; raise ValueError otherwise."""
    try:
        return _ZONE_FARES[zone]
    except KeyError:
        raise ValueError("unknown zone: %r" % (zone,)) from None
'''

COND_FILE = '''"""Zone fares (conditional implementation)."""


def fare_for_zone(zone):
    """Return the fare for zones 1-4; raise ValueError otherwise."""
    if zone == 1:
        return 2.50
    elif zone == 2:
        return 3.75
    elif zone == 3:
        return 5.00
    elif zone == 4:
        return 6.25
    raise ValueError("unknown zone: %r" % (zone,))
'''


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "chall"
    repo.mkdir()
    for name, text in (("fare_table.py", FARE_FILE),
                       ("fare_rules.py", COND_FILE)):
        (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@l"],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(repo), "add", "-A"],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "x"],
                   check=True, capture_output=True, timeout=60)
    rev = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True,
                         timeout=60).stdout.strip()
    return repo, rev


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "m.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE units (id INTEGER PRIMARY KEY, path TEXT, "
                 "symbol TEXT, start_line INT, end_line INT, text TEXT)")
    conn.execute("INSERT INTO units (path,symbol,start_line,end_line,text)"
                 " VALUES (?,?,?,?,?)",
                 ("fare_table.py", "zone_fare_lookup", 7, 12,
                  "\n".join(FARE_FILE.splitlines()[6:12])))
    conn.execute("INSERT INTO units (path,symbol,start_line,end_line,text)"
                 " VALUES (?,?,?,?,?)",
                 ("fare_rules.py", "fare_for_zone", 4, 14,
                  "\n".join(COND_FILE.splitlines()[3:14])))
    conn.commit()
    conn.close()
    return db


def _package(tmp_path: Path) -> dict:
    repo, rev = _git_repo(tmp_path)
    db = _db(tmp_path)
    pkg = ED.build_evidence_package(
        db, repo, rev,
        {"path": "fare_table.py", "symbol": "zone_fare_lookup"},
        {"path": "fare_rules.py", "symbol": "fare_for_zone"})
    assert pkg["ok"], pkg
    return pkg


def test_package_includes_module_data():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        pkg = _package(Path(td))
    # The decisive module-level table is inside the above-window.
    assert "_ZONE_FARES = {1: 2.50" in \
        "\n".join(pkg["files"]["fare_table.py"]["window_lines"])
    assert pkg["target"]["start_line"] == 7
    assert pkg["rev"]


def test_package_errors():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo, rev = _git_repo(tmp)
        db = _db(tmp)
        missing = ED.build_evidence_package(
            db, repo, rev, {"path": "nope.py", "symbol": "f"},
            {"path": "fare_rules.py", "symbol": "fare_for_zone"})
        assert missing["ok"] is False
        bad_rev = ED.build_evidence_package(
            db, repo, "0" * 40,
            {"path": "fare_table.py", "symbol": "zone_fare_lookup"},
            {"path": "fare_rules.py", "symbol": "fare_for_zone"})
        assert bad_rev["ok"] is False and bad_rev["code"] == "file_unreadable"
        frag = ED.build_evidence_package(
            db, repo, rev,
            {"path": "fare_table.py", "symbol": "x#region1"},
            {"path": "fare_rules.py", "symbol": "fare_for_zone"})
        assert frag["code"] == "fragment_source"


def test_render_input_and_proposal():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        pkg = _package(Path(td))
    terra = {"shared_component": "zone fare lookup",
             "shared_behavior": "look up fares",
             "opportunity": "partial_extraction",
             "differences": ["table vs conditionals"]}
    prop = ED.propose_comment(terra, pkg)
    assert "zone_fare_lookup" in prop and "fare_for_zone" in prop
    assert "partial_extraction" not in prop  # human wording, not the enum
    assert "extracting the shared component" in prop
    text = ED.render_editor_input(pkg, prop)
    assert "_ZONE_FARES" in text and "5.00" in text
    assert "zone_fare_lookup" in text


def test_validate_editor_output():
    good_keep = {"decision": "KEEP", "revised_comment": "reuse X",
                 "reason": "because X"}
    assert ED.validate_editor_output(good_keep) == []
    assert ED.validate_editor_output(
        {"decision": "REVISE", "revised_comment": "narrowed",
         "reason": "r"}) == []
    assert ED.validate_editor_output(
        {"decision": "DROP", "revised_comment": "", "reason": "noise"}) == []
    assert ED.validate_editor_output({"decision": "MAYBE",
                                      "revised_comment": "",
                                      "reason": ""}) != []
    assert ED.validate_editor_output(
        {"decision": "KEEP", "revised_comment": "",
         "reason": "r"}) != []
    assert ED.validate_editor_output(
        {"decision": "DROP", "revised_comment": "", "reason": ""}) != []


def test_check_comment_locations():
    files = ["fare_table.py", "fare_rules.py"]
    assert ED.check_comment_locations(
        "see fare_table.py::zone_fare_lookup lines 7-12", files) == []
    assert ED.check_comment_locations("no locations here", files) == []
    bad = ED.check_comment_locations("see elsewhere/evil.py::fn", files)
    assert len(bad) == 1 and "elsewhere/evil.py" in bad[0]


def _call(text=None, ok=True, error=""):
    def _fn(**kwargs):
        assert "zone_fare_lookup" in kwargs["user_input"]
        assert "Proposed review comment" in kwargs["user_input"]
        if not ok:
            return {"ok": False, "error": error, "tool_calls": [],
                    "wall_s": 0.0, "final_text": ""}
        return {"ok": True, "final_text": text, "tool_calls": [],
                "wall_s": 0.1}
    return _fn


def _pkg_lite() -> dict:
    return {"ok": True, "rev": "abc",
            "target": {"path": "fare_table.py", "symbol": "zone_fare_lookup",
                       "start_line": 7, "end_line": 12, "text": "t"},
            "counterpart": {"path": "fare_rules.py",
                            "symbol": "fare_for_zone", "start_line": 4,
                            "end_line": 14, "text": "c"},
            "files": {"fare_table.py": {"total_lines": 12, "head_lines": [],
                                        "window_start": 1, "window_end": 12,
                                        "window_lines": ["t"],
                                        "complete": True},
                      "fare_rules.py": {"total_lines": 14, "head_lines": [],
                                        "window_start": 1, "window_end": 14,
                                        "window_lines": ["c"],
                                        "complete": True}}}


def test_edit_pair_keep_revise_drop():
    pkg = _pkg_lite()
    out = ED.edit_pair(
        package=pkg, proposed_comment="share fare lookup", tools=[],
        model="m", model_settings=None, ledger=None,
        call=_call(json.dumps({"decision": "KEEP",
                               "revised_comment": "reuse the table",
                               "reason": "same fares"})))
    assert out["ok"] is True
    assert out["editor"]["decision"] == "KEEP"
    out2 = ED.edit_pair(
        package=pkg, proposed_comment="share fare lookup", tools=[],
        model="m", model_settings=None, ledger=None,
        call=_call(json.dumps({"decision": "DROP", "revised_comment": "",
                               "reason": "too generic"})))
    assert out2["ok"] is True and out2["editor"]["decision"] == "DROP"


def test_edit_pair_failures():
    pkg = _pkg_lite()
    kw = dict(package=pkg, proposed_comment="x", tools=[], model="m",
              model_settings=None, ledger=None)
    bad_model = ED.edit_pair(call=_call(ok=False, error="boom"), **kw)
    assert bad_model["ok"] is False
    unparse = ED.edit_pair(call=_call("not json"), **kw)
    assert unparse["ok"] is False
    invalid = ED.edit_pair(call=_call(json.dumps({"decision": "MAYBE",
                                                  "revised_comment": "",
                                                  "reason": ""})), **kw)
    assert invalid["ok"] is False
    unseen = ED.edit_pair(call=_call(json.dumps(
        {"decision": "REVISE", "revised_comment": "see evil/x.py::fn",
         "reason": "r"})), **kw)
    assert unseen["ok"] is False and "unseen" in unseen["error"]


def test_wrapper_prefilter():
    assert ED.wrapper_prefilter("def f():\n return g(x)\n", "g")["wrapper"] is True
    assert ED.wrapper_prefilter("def f():\n return 1\n", "g")["wrapper"] is False
    assert ED.wrapper_prefilter("", "g")["wrapper"] is False
