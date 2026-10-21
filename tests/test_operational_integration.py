"""M4 gate: provider + operational integration through the canonical path.

Unified backends (luna/haiku/gemma resolve + build with explicit failures),
privacy enforcement, spend ledger + ceiling, tool budget, failure records,
full forensic tool outputs, and shadow delivery via fixture transport.
No paid inference: scripted models and fixture transports only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codatiel_i2 import providers as providers_mod  # noqa: E402
from codatiel_i2 import service as service_mod  # noqa: E402

agents = pytest.importorskip("agents")


@pytest.fixture()
def repo(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "m4@t"],
                 ["config", "user.name", "m4"]):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       timeout=60)
    (root / "app.py").write_text("LIMIT = 100\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True,
                   timeout=60)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"],
                   check=True, timeout=60)
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()
    (root / "app.py").write_text("LIMIT = 10\n")
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "head"],
                   check=True, timeout=60)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()
    return {"root": root, "base": base, "head": head}


def _scenario(tmp_path: Path, name: str, turns: list, final: dict) -> str:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"turns": turns, "final": final}))
    return str(path)


def test_resolve_backends_and_privacy_posture():
    import os

    gemma = providers_mod.resolve_backend("gemma")
    assert gemma["provider"] == "openai_compatible"
    assert gemma["privacy"] == "local_inference_no_third_party_transmission"
    assert gemma["rate_usd_per_mtok"] == {"input": 0.0, "output": 0.0}
    for name, privacy in (
            ("luna", "OPENAI_STANDARD_API_NO_TRAINING"),
            ("haiku", "ANTHROPIC_COMMERCIAL_NO_TRAINING_DEFAULT")):
        profile = providers_mod.load_profiles()[name]
        assert profile["privacy"] == privacy
        if os.environ.get(profile.get("credential_env", "")):
            resolved = providers_mod.resolve_backend(name)
            assert resolved["key_present"] is True
        else:
            with pytest.raises(RuntimeError, match="needs .* in the environment"):
                providers_mod.resolve_backend(name)
    with pytest.raises(ValueError):
        providers_mod.resolve_backend("sol")


def test_luna_settings_enforce_store_false():
    from codatiel_i2 import runtime_sdk

    assert runtime_sdk.luna_model_settings().store is False


def test_cost_math_never_invents_rates():
    assert providers_mod.estimate_cost_usd(4000, 12, {"input": None,
                                                     "output": 0.5}) is None
    est = providers_mod.estimate_cost_usd(
        4000, 12, {"input": 0.10, "output": 0.50})
    assert est and est > 0
    assert providers_mod.estimate_cost_usd(0, 0, {"input": 0.0,
                                                 "output": 0.0}) == 0.0


def test_budget_refusal_before_inference(repo: dict, tmp_path: Path):
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"],
        model=service_mod.scripted_model_from_scenario(_scenario(
            tmp_path, "b", [], {"status": "silence", "summary": "",
                                "findings": []})),
        model_name="scripted:m4", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work",
        rate={"input": 1000.0, "output": 1000.0},
        rate_status="test-priced", max_usd_per_review=0.000001)
    assert out["status"] == "budget_refused"
    assert "run_dir" not in out


def test_spend_ledger_tokens_only_without_rates(repo: dict,
                                                tmp_path: Path):
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"],
        model=service_mod.scripted_model_from_scenario(_scenario(
            tmp_path, "s",
            [{"tool": "read_file",
              "args": {"path": "app.py", "revision": "head"}}],
            {"status": "silence", "summary": "ok", "findings": []})),
        model_name="scripted:m4", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    assert out["status"] == "complete"
    metrics = json.loads(
        (Path(out["run_dir"]) / "metrics.json").read_text())
    assert metrics["spend"]["tokens_only"] is True
    assert metrics["spend"]["usd_estimate"] is None
    assert metrics["spend"]["exceeds_ceiling"] is False
    assert (Path(out["run_dir"]) / "tool_outputs.jsonl").exists()


def test_tool_budget_fails_loudly(repo: dict, tmp_path: Path):
    from codatiel_i2 import runtime_sdk

    scenario = _scenario(tmp_path, "tb", [
        {"tool": "read_file",
         "args": {"path": "app.py", "revision": "head"}},
        {"tool": "read_file",
         "args": {"path": "app.py", "revision": "base"}}],
        {"status": "silence", "summary": "", "findings": []})
    model = service_mod.scripted_model_from_scenario(scenario)
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model,
        model_name="scripted:m4", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", tool_budget=1)
    assert out["status"] == "complete"
    assert out["tool_calls"] == 1  # second call rejected at the budget
    outputs = [json.loads(line) for line in
               (Path(out["run_dir"]) / "tool_outputs.jsonl")
               .read_text().splitlines() if line.strip()]
    assert any(o["payload"].get("status") == "error" and "budget" in
               o["payload"].get("error", "") for o in outputs)


def test_shadow_delivery_fixture_transport(repo: dict, tmp_path: Path):
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"],
        model=service_mod.scripted_model_from_scenario(_scenario(
            tmp_path, "d",
            [{"tool": "read_file",
              "args": {"path": "app.py", "revision": "head"}}],
            {"status": "silence", "summary": "quiet", "findings": []})),
        model_name="scripted:m4", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    sent: list[dict] = []

    def fixture_transport(to: list[str], subject: str, body: str) -> dict:
        sent.append({"to": to, "subject": subject, "body": body})
        return {"accepted_by_transport": True, "parsed": True,
                "fixture": True, "succeeded": len(to), "failed": 0,
                "email_id": "fixture-no-send"}

    receipt = service_mod.deliver_shadow(
        out["run_dir"], to=["codatiel-test@mitchellcurrie.com"],
        sender="codatiel@mitchellcurrie.com", transport=fixture_transport)
    assert len(sent) == 1
    assert out["run_id"] in sent[0]["body"]
    assert repo["head"][:8] in sent[0]["subject"] or \
        repo["head"][:8] in sent[0]["body"]
    assert receipt["transport_receipt"]["email_id"] == "fixture-no-send"
    delivery = json.loads(
        (Path(out["run_dir"]) / "delivery.json").read_text())
    assert delivery["run_id"] == out["run_id"]
    assert "SMTP2GO_API_KEY" not in json.dumps(delivery)
    # Future-delivery retention: exact body + content hash preserved pre-send.
    import hashlib

    assert delivery["body"] == sent[0]["body"]
    assert delivery["body_sha256"] == hashlib.sha256(
        sent[0]["body"].encode("utf-8")).hexdigest()


def test_cli_deliver_shadow_fixture(repo: dict, tmp_path: Path):
    from codatiel_i2.cli import build_parser, cmd_deliver_shadow

    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"],
        model=service_mod.scripted_model_from_scenario(_scenario(
            tmp_path, "cd", [], {"status": "silence", "summary": "",
                                 "findings": []})),
        model_name="scripted:m4", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    args = build_parser().parse_args([
        "deliver-shadow", "--run-dir", out["run_dir"],
        "--to", "codatiel-test@mitchellcurrie.com", "--sender",
        "codatiel-test@mitchellcurrie.com", "--transport", "fixture"])
    payload = cmd_deliver_shadow(args)
    assert payload["transport_receipt"]["fixture"] is True


def test_scan_identity_binds_base_and_head(repo: dict):
    # Migrated dedupe contract: a retargeted base is a different review.
    from codatiel_i2.core import runs as runs_mod

    config = {"service": "t"}
    same_a = runs_mod.scan_key_for("r", repo["base"], repo["head"], config)
    same_b = runs_mod.scan_key_for("r", repo["base"], repo["head"], config)
    assert same_a == same_b
    other_base = "0" * 40 if repo["base"] != "0" * 40 else "1" * 40
    assert runs_mod.scan_key_for("r", other_base, repo["head"], config) != same_a
    assert runs_mod.scan_key_for("r", repo["base"], other_base, config) != same_a


def test_no_pr_comment_path_canonical(repo: dict, tmp_path: Path):
    # Migrated no-comment contract: no run artifact affords PR comments;
    # the only outbound channel is the digest transport.
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"],
        model=service_mod.scripted_model_from_scenario(_scenario(
            tmp_path, "nc",
            [{"tool": "read_file",
              "args": {"path": "app.py", "revision": "head"}}],
            {"status": "silence", "summary": "", "findings": []})),
        model_name="scripted:m4", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    run_dir = Path(out["run_dir"])
    blob = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in run_dir.glob("*.json"))
    for token in ("post_to_pr", "create_comment", "issues/", "pulls/",
                  "post_comment"):
        assert token not in blob, token
    assert json.loads((run_dir / "shadow_result.json").read_text())[
        "post_to_github"] is False
