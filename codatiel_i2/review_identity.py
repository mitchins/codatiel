"""Immutable review-identity binding (IDENTITY-01).

A review run is identified by (repository slug, PR number, base SHA,
head SHA, run ID). The binding is written once, at review creation,
from the same event arguments that drive the review itself, and every
later consumer — normal-findings render, semdup stamp, digest model,
delivery — verifies the same five values instead of trusting a
caller-supplied label.

A caller-supplied context label (``dev:owner/repo#123``) can only
*confirm* the bound identity; it can never override it. Any mismatch
— wrong repo, wrong PR number, drifted SHAs, missing binding with a
label attached — refuses with ``ValueError`` and zero outbound bytes.

Legacy run dirs without ``identity.json`` keep the old unlabeled
behaviour (local-path subject, no PR links) only when NO context
label is attached. Attaching a label to an unbound run is refused;
bind it explicitly first (``events bind-identity``), which requires
reviews-table corroboration.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

IDENTITY_VERSION = "codatiel_i2.review_identity.v1"
IDENTITY_FILENAME = "identity.json"

_SLUG_RE = re.compile(r"^[\w.\-]+/[\w.\-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def decode_clone_slug(repo_field: str) -> str:
    """Decode a run.json repo field to an owner/repo slug.

    Pipeline runs store the local clone path
    (``.../clones/mitchins__needletail-harness``); GitHub-workspace
    runs store the slug directly. The ``__`` split applies ONLY under
    a ``clones/`` path component (the pipeline clone convention): an
    arbitrary local path whose name merely contains ``__`` decodes to
    "" (nothing to check) instead of risking a false refusal.
    """
    field = (repo_field or "").strip()
    if not field:
        return ""
    if _SLUG_RE.match(field):
        return field
    parts = field.rstrip("/").split("/")
    if "clones" not in parts[:-1]:
        return ""
    base = parts[-1]
    if "__" in base:
        slug = base.replace("__", "/", 1)
        if _SLUG_RE.match(slug):
            return slug
    # Local fixture paths (no owner encoding) say nothing about the
    # PR slug; callers skip the repo cross-check when this is "".
    return ""


def _check_shapes(repo_slug: str, pr_number: Any,
                  base_sha: str, head_sha: str) -> tuple[str, int, str, str]:
    if not isinstance(repo_slug, str) or not _SLUG_RE.match(repo_slug):
        raise ValueError(
            f"invalid repo slug {repo_slug!r}; refusing to bind identity")
    try:
        pr = int(pr_number)
    except (TypeError, ValueError):
        raise ValueError(
            f"invalid PR number {pr_number!r}; refusing to bind identity")
    if pr <= 0:
        raise ValueError(
            f"invalid PR number {pr_number!r}; refusing to bind identity")
    for label, sha in (("base", base_sha), ("head", head_sha)):
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise ValueError(
                f"invalid {label} SHA {str(sha)[:20]!r}; refusing to bind "
                f"identity (full 40-hex required, never a prefix)")
    return repo_slug, pr, base_sha, head_sha


def _run_record(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(
            f"cannot bind identity: run.json unreadable in {run_dir}: "
            f"{type(exc).__name__}") from exc


def _sem_record(run_dir: Path) -> dict:
    try:
        sem = json.loads((run_dir / "semdup.json").read_text(
            encoding="utf-8"))
        return sem if isinstance(sem, dict) else {}
    except Exception:
        return {}


def _pr_record(run_dir: Path) -> dict:
    try:
        pr = json.loads((run_dir / "pr.json").read_text(encoding="utf-8"))
        return pr if isinstance(pr, dict) else {}
    except Exception:
        return {}


def _sem_shas(sem: dict) -> tuple[str | None, str | None]:
    base = sem.get("base_sha") or sem.get("base_rev")
    head = sem.get("head_sha") or sem.get("head_rev")
    return base, head


def _verify_artifacts(run_dir: Path, repo_slug: str, pr: int,
                      base_sha: str, head_sha: str, run_id: str) -> None:
    """Cross-check the requested binding against stored artifacts.

    Every artifact that carries identity must agree; artifacts that
    are absent say nothing. Disagreement is a hard refusal.
    """
    run = _run_record(run_dir)
    decoded = decode_clone_slug(str(run.get("repo") or ""))
    if decoded and decoded != repo_slug:
        raise ValueError(
            f"identity repo {repo_slug!r} does not match run repo "
            f"{decoded!r}; refusing a misaddressed binding")
    for label, got, want in (("base_sha", run.get("base_sha"), base_sha),
                             ("head_sha", run.get("head_sha"), head_sha),
                             ("run_id", run.get("run_id"), run_id)):
        if got and got != want:
            raise ValueError(
                f"identity {label} {str(want)[:12]!r} does not match run "
                f"{label} {str(got)[:12]!r}; refusing a misaddressed "
                f"binding")
    if run.get("pr_number") is not None:
        try:
            run_pr = int(run["pr_number"])
        except (TypeError, ValueError):
            run_pr = None
        if run_pr is not None and run_pr != pr:
            raise ValueError(
                f"identity PR #{pr} does not match run PR #{run_pr}; "
                f"refusing a misaddressed binding")
    sem = _sem_record(run_dir)
    if sem.get("repo") and sem["repo"] != repo_slug:
        raise ValueError(
            f"identity repo {repo_slug!r} does not match semdup repo "
            f"{sem['repo']!r}; refusing a misaddressed binding")
    if sem.get("pr") is not None:
        try:
            sem_pr = int(sem["pr"])
        except (TypeError, ValueError):
            sem_pr = None
        if sem_pr is not None and sem_pr != pr:
            raise ValueError(
                f"identity PR #{pr} does not match semdup PR #{sem_pr}; "
                f"refusing a misaddressed binding")
    sem_base, sem_head = _sem_shas(sem)
    if sem_base and sem_base != base_sha:
        raise ValueError(
            f"identity base {base_sha[:12]!r} does not match semdup base "
            f"{str(sem_base)[:12]!r}; refusing a misaddressed binding")
    if sem_head and sem_head != head_sha:
        raise ValueError(
            f"identity head {head_sha[:12]!r} does not match semdup head "
            f"{str(sem_head)[:12]!r}; refusing a misaddressed binding")
    prec = _pr_record(run_dir)
    if prec:
        for label, got, want in (("base", prec.get("base_sha"), base_sha),
                                 ("head", prec.get("head_sha"), head_sha)):
            if got and got != want:
                raise ValueError(
                    f"identity {label} {str(want)[:12]!r} does not match "
                    f"pr.json {label} {str(got)[:12]!r}; refusing a "
                    f"misaddressed binding")
        prec_repo = str(prec.get("repo") or "")
        decoded_prec = decode_clone_slug(prec_repo)
        if decoded_prec and decoded_prec != repo_slug:
            raise ValueError(
                f"identity repo {repo_slug!r} does not match pr.json "
                f"repo {decoded_prec!r}; refusing a misaddressed binding")
        if prec.get("pr_number") is not None:
            try:
                prec_pr: Any = int(prec["pr_number"])
            except (TypeError, ValueError):
                prec_pr = None
            if prec_pr is not None and prec_pr != pr:
                raise ValueError(
                    f"identity PR #{pr} does not match pr.json PR "
                    f"#{prec_pr}; refusing a misaddressed binding")


def bind_run(run_dir: Path | str, *, repo_slug: str, pr_number: int,
             base_sha: str, head_sha: str, run_id: str,
             delivery_id: str | None = None,
             binder: str = "pipeline") -> dict:
    """Write the immutable identity binding for a run dir.

    Idempotent for the identical binding (safe retry); any differing
    rebinding attempt is refused. The binding is verified against
    every stored artifact before it is written. The write is atomic
    (temp file + hard link): concurrent binders either agree or the
    loser refuses — a binding is never half-written or clobbered.
    A present-but-unreadable identity file is refused explicitly
    (never silently rebound, never recursed).
    """
    run_dir = Path(run_dir)
    repo_slug, pr, base_sha, head_sha = _check_shapes(
        repo_slug, pr_number, base_sha, head_sha)
    if not run_id:
        raise ValueError("cannot bind identity without a run ID")
    existing = load_identity(run_dir)
    target = run_dir / IDENTITY_FILENAME
    if existing is not None:
        for key, want in (("repo_slug", repo_slug), ("pr_number", pr),
                          ("base_sha", base_sha), ("head_sha", head_sha),
                          ("run_id", run_id)):
            if existing.get(key) != want:
                raise ValueError(
                    f"run {run_dir} already bound to "
                    f"{existing.get('repo_slug')}#{existing.get('pr_number')} "
                    f"{str(existing.get('head_sha') or '')[:12]}; refusing "
                    f"rebind to {repo_slug}#{pr}")
        return existing
    if target.exists():
        raise ValueError(
            f"run {run_dir} has a present-but-unreadable identity file; "
            f"refusing to bind over a possibly damaged binding")
    _verify_artifacts(run_dir, repo_slug, pr, base_sha, head_sha, run_id)
    identity = {
        "version": IDENTITY_VERSION,
        "repo_slug": repo_slug, "pr_number": pr,
        "base_sha": base_sha, "head_sha": head_sha,
        "run_id": run_id, "delivery_id": delivery_id,
        "binder": binder, "bound_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload = json.dumps(identity, indent=1).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(run_dir), prefix=".identity-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        try:
            os.link(tmp_name, str(target))
        except FileExistsError:
            pass
        else:
            return identity
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    # A concurrent binder won the link: agree or refuse.
    winner = load_identity(run_dir)
    if winner is None:
        raise ValueError(
            f"run {run_dir}: concurrent binding left an unreadable "
            f"identity file; refusing")
    for key, want in (("repo_slug", repo_slug), ("pr_number", pr),
                      ("base_sha", base_sha), ("head_sha", head_sha),
                      ("run_id", run_id)):
        if winner.get(key) != want:
            raise ValueError(
                f"run {run_dir} concurrently bound to "
                f"{winner.get('repo_slug')}#{winner.get('pr_number')}; "
                f"refusing rebind to {repo_slug}#{pr}")
    return winner


def load_identity(run_dir: Path | str) -> dict | None:
    """Return the bound identity, or None when the run predates binding."""
    try:
        raw = (Path(run_dir) / IDENTITY_FILENAME).read_text(
            encoding="utf-8")
    except OSError:
        return None
    try:
        ident = json.loads(raw)
    except ValueError:
        return None
    return ident if isinstance(ident, dict) else None


_SUBJECT_RE = re.compile(r"([\w.\-]+/[\w.\-]+)#(\d+)")


def identity_named_in_subject(subject: str | None,
                              ident: dict) -> None:
    """Refuse an explicit subject naming a different PR than bound.

    Free-text subjects (no ``owner/repo#nn`` pattern) assert no PR
    identity and pass. Any named pair must equal the binding.
    """
    for slug, pr in _SUBJECT_RE.findall(subject or ""):
        try:
            pr_num: Any = int(pr)
        except (TypeError, ValueError):
            pr_num = None
        if (slug != ident.get("repo_slug") or pr_num is None
                or pr_num != int(ident.get("pr_number") or 0)):
            raise ValueError(
                f"explicit subject names {slug}#{pr} but the run is "
                f"bound to {ident.get('repo_slug')}"
                f"#{ident.get('pr_number')}; refusing a misaddressed "
                f"digest")


def artifact_identities(run_dir: Path | str) -> set[tuple[str, int]]:
    """Every (repo, PR) pair attested by stored artifacts.

    Covers pipeline run dirs (slug or clone path + ``pr_number``),
    semdup-worker report dirs (slug + ``pr``), semdup stamps and
    pr.json. Runs with no recognizable identity attest nothing.
    """
    run_dir = Path(run_dir)
    pairs: set[tuple[str, int]] = set()

    def _add(repo_field: Any, pr: Any) -> None:
        slug = decode_clone_slug(str(repo_field or ""))
        if not slug or pr is None:
            return
        try:
            pairs.add((slug, int(pr)))
        except (TypeError, ValueError):
            pass

    run = _run_record(run_dir) if (run_dir / "run.json").exists() else {}
    if isinstance(run, dict):
        _add(run.get("repo"), run.get("pr_number"))
        # semdup-worker report dirs use "pr" instead of "pr_number".
        _add(run.get("repo"), run.get("pr"))
    sem = _sem_record(run_dir)
    if isinstance(sem, dict):
        _add(sem.get("repo"), sem.get("pr"))
    prec = _pr_record(run_dir)
    if isinstance(prec, dict):
        _add(prec.get("repo"), prec.get("pr_number"))
        _add(prec.get("repo"), prec.get("pr"))
    return pairs


def check_subject_matches_artifacts(run_dir: Path | str,
                                    subject: str | None) -> None:
    """Refuse an explicit subject no stored artifact agrees with.

    Applies to unbound legacy runs (bound runs use the stricter
    binding check). Subjects that name no PR pass; a subject naming a
    PR must intersect the artifact-attested pairs. A subject naming a
    PR when the run attests NOTHING is refused — that is the silent
    substitution shape this module exists to close.
    """
    named = set()
    for slug, pr in _SUBJECT_RE.findall(subject or ""):
        try:
            named.add((slug, int(pr)))
        except (TypeError, ValueError):
            continue
    if not named:
        return
    attested = artifact_identities(run_dir)
    if not attested or named.isdisjoint(attested):
        raise ValueError(
            f"explicit subject names {sorted(named)!r} but stored "
            f"artifacts attest {sorted(attested)!r}; refusing a "
            f"misaddressed digest")


def parse_context(context: str | None) -> dict:
    """owner/repo + PR number from an event label like dev:owner/repo#10."""
    from codatiel_i2 import digest_model as _DM
    return _DM.parse_event_context(context)


def require_bound(run_dir: Path | str,
                  context: str | None = None) -> dict | None:
    """Return the bound identity after verifying caller context.

    - No identity file + no context: None (legacy unlabeled behaviour;
      the caller must render without PR links and must not invent any).
    - A present-but-damaged identity file (unreadable JSON or missing
      fields): always refused, with or without context. A damaged
      binding never downgrades to "unbound".
    - No identity file + context label: refused (a label with nothing
      to check against is exactly the crossed-delivery mechanism).
    - Identity + context label: the label must name the SAME repo slug
      and PR number; SHAs and run ID come from the binding, never the
      label. Every stored artifact is re-verified.
    - Identity + no context: the binding (verified) — labelling from
      stored truth, not from a caller string.
    """
    run_dir = Path(run_dir)
    target = run_dir / IDENTITY_FILENAME
    ident = load_identity(run_dir)
    if ident is None:
        if target.exists():
            raise ValueError(
                f"run {run_dir} has a present-but-damaged identity file; "
                f"refusing delivery until it is repaired")
        if context is None:
            return None
        raise ValueError(
            f"run {run_dir} has no bound identity; refusing context "
            f"{context!r} (bind the run before labelling it)")
    for key in ("repo_slug", "pr_number", "base_sha", "head_sha",
                "run_id"):
        if not ident.get(key):
            raise ValueError(
                f"run {run_dir} has a damaged identity binding (missing "
                f"{key}); refusing delivery")
    try:
        _check_shapes(str(ident["repo_slug"]), ident["pr_number"],
                      str(ident["base_sha"]), str(ident["head_sha"]))
    except ValueError as exc:
        raise ValueError(
            f"run {run_dir} has a malformed identity binding ({exc}); "
            f"refusing delivery") from exc
    if context is not None:
        ctx = parse_context(context)
        if ctx.get("repo_slug") and ctx["repo_slug"] != ident["repo_slug"]:
            raise ValueError(
                f"context repo {ctx['repo_slug']!r} does not match bound "
                f"repo {ident['repo_slug']!r} for run {ident['run_id']}; "
                f"refusing a misaddressed digest")
        if ctx.get("pr_number"):
            try:
                ctx_pr: Any = int(ctx["pr_number"])
            except (TypeError, ValueError):
                ctx_pr = None
            if ctx_pr is not None and ctx_pr != int(ident["pr_number"]):
                raise ValueError(
                    f"context PR #{ctx_pr} does not match bound PR "
                    f"#{ident['pr_number']} for "
                    f"{ident['repo_slug']} run {ident['run_id']}; "
                    f"refusing a misaddressed digest")
    _verify_artifacts(run_dir, str(ident["repo_slug"]),
                      int(ident["pr_number"]), str(ident["base_sha"]),
                      str(ident["head_sha"]), str(ident["run_id"]))
    return ident
