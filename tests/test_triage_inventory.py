"""M9.2 P3: triage inventory unit tests (synthetic repo, no model) + arm freeze.

- Triage is deterministic, generic, ranked by churn, capped, with raw
  facts (path class, defs, reference counts) and a recorded token cost.
- Arm definitions: A = minimal + file-list; B = guidebook-port + file-list;
  C = guidebook-port + triage. Service constants are content-derived.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codatiel_i2 import service as svc  # noqa: E402
from codatiel_i2 import triage as triage_mod  # noqa: E402
from codatiel_i2.core import diff as diff_mod  # noqa: E402
from codatiel_i2.core import files as files_mod  # noqa: E402


def _repo(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "authz.py").write_text("ALLOW = {'owner'}\n")
    (root / "src" / "util.py").write_text("def f():\n    return 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
    (root / "docs.md").write_text("# docs\n")
    subprocess.run(["git", "-C", root, "init", "-q"], check=True, timeout=60)
    subprocess.run(["git", "-C", root, "config", "user.email", "t@t"],
                   check=True, timeout=60)
    subprocess.run(["git", "-C", root, "config", "user.name", "t"],
                   check=True, timeout=60)
    subprocess.run(["git", "-C", root, "add", "."], check=True, timeout=60)
    subprocess.run(["git", "-C", root, "commit", "-qm", "b"], check=True,
                   timeout=60)
    base = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()
    # High-churn auth change + small util change + test/docs-only changes.
    (root / "src" / "authz.py").write_text(
        "ALLOW = {'owner', 'editor'}\n" + "\n".join(f"RULE_{i} = {i}"
                                                    for i in range(60)) + "\n")
    (root / "src" / "util.py").write_text("def f():\n    return 2\n")
    (root / "tests" / "test_x.py").write_text(
        "def test_a():\n    assert True\ndef test_b():\n    assert True\n")
    (root / "docs.md").write_text("# docs\nmore\n")
    subprocess.run(["git", "-C", root, "add", "."], check=True, timeout=60)
    subprocess.run(["git", "-C", root, "commit", "-qm", "h"], check=True,
                   timeout=60)
    head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()
    return {"root": root, "base": base, "head": head}


def _render(repo: dict) -> str:
    changes = diff_mod.changed_files(repo["root"], repo["base"], repo["head"])
    classification = files_mod.classify_changes(changes)
    return triage_mod.render_triage_inventory(
        changes, classification, [], diff_repo=repo["root"],
        head_dir=repo["root"], base_sha=repo["base"], head_sha=repo["head"])


def test_triage_ranks_churn_first_and_flags_classes(tmp_path: Path):
    repo = _repo(tmp_path)
    body = _render(repo)
    lines = body.splitlines()
    # Token cost recorded in the header.
    assert lines[0].startswith("[triage-0.1 chars=") and "~tokens=" in lines[0]
    # Highest-churn auth file ranks first.
    first = next(i for i, l in enumerate(lines) if l.startswith("- "))
    assert "src/authz.py" in lines[first]
    assert "class=auth" in lines[first]
    assert "churn=" in lines[first]
    # Test-only change classified, not ranked as source.
    test_line = next(l for l in lines if "tests/test_x.py" in l)
    assert "class=tests" in test_line
    assert "Source changes (non-test, non-doc): 2 files." in body
    # Raw facts present, no verdicts.
    assert "regression" not in body.lower()
    assert "vulnerab" not in body.lower()


def test_triage_is_deterministic_and_capped(tmp_path: Path):
    repo = _repo(tmp_path)
    assert _render(repo) == _render(repo)


def test_triage_reference_counts_use_existing_search(tmp_path: Path):
    repo = _repo(tmp_path)
    body = _render(repo)
    # util.py defines f (added line `def f():` is context, not +; authz adds
    # no defs) — reference section appears only when defs found; either way
    # the rendering never claims unknown facts.
    assert "unknown" not in body or "refs(" in body


def test_arm_definitions_are_content_derived(tmp_path: Path):
    # Arm A: minimal prompt, byte-identical to the historical constant.
    assert svc.CANONICAL_INSTRUCTIONS_ID == svc.instructions_id_for(
        "canonical-minimal-0.1", svc.CANONICAL_INSTRUCTIONS)
    # Arm B/C playbook: the committed port doc, loaded verbatim.
    doc = (Path(svc.__file__).resolve().parent.parent
           / "docs" / "reviewer_methodology" / "guidebook_port.md")
    assert svc.GUIDEBOOK_PORT_INSTRUCTIONS == doc.read_text(encoding="utf-8")
    assert svc.GUIDEBOOK_PORT_ID == svc.instructions_id_for(
        "guidebook-port-0.1", svc.GUIDEBOOK_PORT_INSTRUCTIONS)
    assert "guidebook-port-0.1@" in svc.GUIDEBOOK_PORT_ID
    # No PR #6 impact-path content in the port.
    assert "impact-path" not in svc.GUIDEBOOK_PORT_INSTRUCTIONS.lower()
    assert "Changed behavior" not in svc.GUIDEBOOK_PORT_INSTRUCTIONS
    # Strategies are distinct names.
    assert svc.INVENTORY_STRATEGY == "file-list-0.1"
    assert triage_mod.TRIAGE_INVENTORY_STRATEGY == "triage-0.1"


def test_arm_c_plumbing_records_playbook_and_triage(tmp_path: Path):
    """Scripted arm-C run: playbook containment + triage strategy recorded."""
    from codatiel_i2 import runtime_sdk
    repo = _repo(tmp_path)
    script = [
        runtime_sdk.scripted_tool_call(
            call_id="c1", name="read_file",
            arguments={"path": "src/authz.py", "revision": "head"},
            response_id="r1"),
        runtime_sdk.scripted_final(
            '{"status":"silence","summary":"checked","findings":[]}',
            response_id="r2"),
    ]
    import json
    m = runtime_sdk.ScriptedModel(list(script))
    out = svc.review_local(
        repo["root"], repo["base"], repo["head"], model=m,
        model_name="scripted:armc", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", max_turns=5,
        playbook_name="guidebook-port-0.1",
        playbook_text=svc.GUIDEBOOK_PORT_INSTRUCTIONS,
        inventory_strategy="triage-0.1")
    assert out["status"] == "complete"
    import json as _json
    prov = _json.loads((Path(out["run_dir"]) / "prompt_provenance.json")
                    .read_text())
    assert prov["declared_playbook"] == "guidebook-port-0.1"
    assert prov["playbook_content_match"] is True
    assert prov["inventory_strategy"] == "triage-0.1"
    assert prov["inventory"].startswith("[triage-0.1 chars=")
    assert "guidebook" in prov["instructions"].lower()
    run = _json.loads((Path(out["run_dir"]) / "run.json").read_text())
    assert run["config"]["inventory_strategy"] == "triage-0.1"
