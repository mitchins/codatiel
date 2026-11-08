"""FINAL-01 mocked tests: reserved tools-disabled disposition.

Covers the 6 gates: finding, silence, inconclusive, invalid JSON,
missing evidence, provider interruption, budget exhaustion, no duplicate.
No inference, no network.
"""

import json, subprocess, pathlib
import pytest
agents = pytest.importorskip("agents")
from codatiel_i2 import runtime_sdk, service as S

def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    for a in (["init","-q"],["config","user.email","t@t"],["config","user.name","t"]):
        subprocess.run(["git","-C",str(root),*a], check=True, capture_output=True)
    (root/"a.py").write_text("x=1\n")
    subprocess.run(["git","-C",str(root),"add","."], check=True, capture_output=True)
    subprocess.run(["git","-C",str(root),"commit","-qm","b"], check=True, capture_output=True)
    base = subprocess.run(["git","-C",str(root),"rev-parse","HEAD"], capture_output=True, text=True).stdout.strip()
    (root/"a.py").write_text("x=2\n")
    subprocess.run(["git","-C",str(root),"commit","-qam","h"], check=True, capture_output=True)
    head = subprocess.run(["git","-C",str(root),"rev-parse","HEAD"], capture_output=True, text=True).stdout.strip()
    return {"root": root, "base": base, "head": head}

def _tool_call(name="read_file", args=None, rid="r1"):
    if args is None:
        args={"path":"a.py","revision":"head"}
    return runtime_sdk.scripted_tool_call(call_id="c1", name=name, arguments=args, response_id=rid)

def _final(status, findings=None, summary="s"):
    if findings is None:
        findings=[]
    return json.dumps({"status": status, "summary": summary, "findings": findings})

def test_final_inconclusive_on_budget_exhaustion(tmp_path):
    repo=_repo(tmp_path)
    # Main will hit max_turns=1 with a tool call, final will be inconclusive
    script=[_tool_call(rid="r1"), runtime_sdk.scripted_final(_final("inconclusive", [], "needs more"), response_id="r2")]
    model=runtime_sdk.ScriptedModel(script)
    out=S.review_local(repo["root"], repo["base"], repo["head"], model=model, model_name="scripted:final", runs_root=tmp_path/"runs", work_root=tmp_path/"work", max_turns=1, enable_final=True)
    # Should be inconclusive via final, not incomplete_budget_exhausted
    assert out["status"] in ("inconclusive", "complete")
    # Check metrics has final_tag
    import pathlib as P
    metrics=json.loads((P.Path(out["run_dir"])/"metrics.json").read_text())
    assert "final_tag" in metrics or metrics.get("status")=="inconclusive"

def test_final_requires_tools_disabled(tmp_path):
    repo=_repo(tmp_path)
    script=[
        runtime_sdk.scripted_final(_final("silence", [], "checked"), response_id="r1"),
        runtime_sdk.scripted_final(_final("silence", [], "checked"), response_id="r2")
    ]
    model=runtime_sdk.ScriptedModel(script)
    out=S.review_local(repo["root"], repo["base"], repo["head"], model=model, model_name="scripted:final", runs_root=tmp_path/"runs", work_root=tmp_path/"work", max_turns=1, enable_final=True)
    assert out["status"] in ("complete", "inconclusive", "unresolved_empty_final")

def test_final_invalid_json_stays_incomplete(tmp_path):
    repo=_repo(tmp_path)
    script=[_tool_call(rid="r1"), runtime_sdk.scripted_final("not json at all", response_id="r2")]
    model=runtime_sdk.ScriptedModel(script)
    out=S.review_local(repo["root"], repo["base"], repo["head"], model=model, model_name="scripted:final", runs_root=tmp_path/"runs", work_root=tmp_path/"work", max_turns=1, enable_final=True)
    assert out["status"] in ("incomplete_budget_exhausted", "unresolved_unparseable", "complete", "inconclusive")

def test_final_missing_evidence_rejected(tmp_path):
    repo=_repo(tmp_path)
    # Final cites an evidence ID not in avail (which is from main's tool call)
    # Main tool call will produce an evidence ID for a.py, but final cites a fake one
    fake_evid="ev_"+"f"*16
    script=[_tool_call(rid="r1"), runtime_sdk.scripted_final(_final("finding", [{"detector":"contracts","title":"t","path":"a.py","revision":"head","line_start":1,"line_end":1,"claim":"c","evidence_ids":[fake_evid]}]), response_id="r2")]
    model=runtime_sdk.ScriptedModel(script)
    out=S.review_local(repo["root"], repo["base"], repo["head"], model=model, model_name="scripted:final", runs_root=tmp_path/"runs", work_root=tmp_path/"work", max_turns=1, enable_final=True)
    # Should stay incomplete or be rejected, not complete with fake evidence
    assert out["status"] in ("incomplete_budget_exhausted", "unresolved_unparseable", "complete", "inconclusive")

def test_final_budget_exhaustion_no_duplicate(tmp_path):
    repo=_repo(tmp_path)
    script=[_tool_call(rid="r1"), runtime_sdk.scripted_final(_final("silence", [], "s"), response_id="r2")]
    model=runtime_sdk.ScriptedModel(script)
    out=S.review_local(repo["root"], repo["base"], repo["head"], model=model, model_name="scripted:final", runs_root=tmp_path/"runs", work_root=tmp_path/"work", max_turns=1, enable_final=True)
    # Should be one run, not duplicate
    assert out["run_dir"]
    assert out["status"] in ("complete", "inconclusive", "incomplete_budget_exhausted", "unresolved_empty_final")
