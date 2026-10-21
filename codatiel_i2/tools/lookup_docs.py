"""lookup_docs: EXTERNAL/vendor documentation only (policy boundary).

Absolute rule: project documentation (README, docs/, comments) is base/head
REVISION SOURCE EVIDENCE and must be read through revision-bound tools
(read_file/search) so the envelope names the exact SHA. lookup_docs never
touches the working tree and never mints project evidence.

Vendor documentation requires an immutable cached source artifact:
vendor_cache_dir/<package>/<version>/ files. No cache, no claim — remote
fetch and model memory are never evidence.

Staleness is NOT detected: the cache is caller-provided and trusted as
pinned. revision_sha binds the exact file bytes + identity so a swapped
cache file invalidates previously minted evidence digests, but live
re-reading of the cache is out of scope until the contract-detector phase.
"""

from __future__ import annotations

import re
from pathlib import Path

from codatiel_i2.common.hashing import sha256_text
from codatiel_i2.common.time import utc_now_iso
from codatiel_i2.core.calls import traced
from codatiel_i2.core.evidence import make_evidence

SCHEMA_VERSION = "codatiel_i2.docs_lookup.v1"

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


@traced("lookup_docs")
def lookup_docs(query: str, *, package: str | None = None, version: str | None = None,
                vendor_cache_dir: Path | str | None = None,
                max_results: int = 3) -> dict:
    sources: list[dict] = []
    warnings: list[str] = []
    if vendor_cache_dir is not None and package:
        if not (_SAFE_COMPONENT.match(package) and (not version or _SAFE_COMPONENT.match(version))):
            warnings.append(f"refusing unsafe package/version: {package!r}/{version!r}")
        else:
            sources.extend(_cached_vendor_docs(Path(vendor_cache_dir), query, package,
                                               version or "", max_results))
    else:
        warnings.append("vendor cache unavailable (pass vendor_cache_dir + package)")
    warnings.append("web fallback disabled; project docs use read_file/search with explicit revision")
    # revision_sha binds artifact IDENTITY + full file bytes, never the
    # excerpt window: a swapped cache file invalidates the binding.
    evidence = []
    for src in sources[:max_results]:
        ev = make_evidence(kind="external_document", tool="lookup_docs",
                           base_sha="", head_sha="", revision="external",
                           revision_sha=sha256_text(src.get("artifact_identity", "")),
                           path=src.get("url", ""), line_start=1, line_end=1,
                           excerpt=src.get("excerpt", "")[:4000],
                           tool_args={"package": package, "version": src.get("version", ""),
                                      "artifact_digest": src.get("artifact_digest", "")})
        evidence.append(ev.to_dict())
    return {
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "package": package,
        "version": version,
        "status": "ok" if sources else "not_found",
        "sources": sources[:max_results],
        "evidence": evidence,
        "warnings": warnings,
    }


def _cached_vendor_docs(cache: Path, query: str, package: str, version: str,
                        limit: int) -> list[dict]:
    terms = [t.lower() for t in query.split() if len(t) > 2]
    if not terms:
        return []
    cache = Path(cache).resolve()

    def _subdir(*parts: str) -> Path | None:
        target = cache
        for part in parts:
            target = target / part
        try:
            resolved = target.resolve()
        except OSError:
            return None
        if resolved != cache and cache not in resolved.parents:
            return None
        return resolved

    roots: list[Path] = []
    if version:
        sub = _subdir(package, version)
        if sub is not None:
            roots = [sub]
    else:
        pkg_dir = _subdir(package)
        if pkg_dir is not None and pkg_dir.is_dir():
            for child in sorted(pkg_dir.iterdir()):
                sub = _subdir(package, child.name)
                if sub is not None:
                    roots.append(sub)
    hits: list[dict] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file() or len(hits) >= limit * 3:
                continue
            try:
                rel = path.relative_to(root)
                text = path.read_text(encoding="utf-8", errors="strict")
            except Exception:
                continue
            lowered = text.lower()
            score = sum(1 for t in terms if t in lowered)
            if score == 0:
                continue
            idx = min((lowered.find(t) for t in terms if t in lowered))
            excerpt = text[max(0, idx - 200):idx + 500]
            # revision binds file IDENTITY + full bytes, not the window:
            # a swapped cache file invalidates the digest.
            file_digest = sha256_text(text)
            identity = f"{package}@{root.name}/{rel.as_posix()}:{file_digest}"
            hits.append({
                "title": f"{package}@{root.name}/{rel.as_posix()}",
                "url": f"vendor-cache://{package}/{root.name}/{rel.as_posix()}",
                "source_type": "cached_vendor_docs",
                "authority": "vendor",
                "version": root.name,
                "version_uncertainty": "cache caller-pinned; staleness not detected",
                "excerpt": excerpt,
                "retrieved_at": utc_now_iso(),
                "content_digest": sha256_text(excerpt),
                "artifact_digest": file_digest,
                "artifact_identity": identity,
                "relevance": "high" if score >= 2 else "medium",
            })
    hits.sort(key=lambda r: (0 if r["relevance"] == "high" else 1, r["title"]))
    return hits[:limit]
