"""GitHub App authentication for the v0.1.0 watcher (read-only).

Account-wide private App installed on the owner account with read-only
repository metadata, contents and pull-request permissions. Nothing here
can write: installation tokens inherit the App's read-only grants, and
every caller in this repo uses GET / clone / fetch only.

Credential inputs (environment only, never committed):
  GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY (inline PEM, \\n-escaped ok) or
  GITHUB_APP_PRIVATE_KEY_PATH, GITHUB_ACCOUNT (default: mitchins),
  GITHUB_INSTALLATION_ID (optional; otherwise auto-discovered for the
  account).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

API = "https://api.github.com"


def load_private_key() -> str:
    inline = (os.environ.get("GITHUB_APP_PRIVATE_KEY") or "").strip()
    if inline:
        if "\\n" in inline and "\n" not in inline:
            inline = inline.replace("\\n", "\n")
        return inline
    path = (os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH") or "").strip()
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    raise RuntimeError("GITHUB_APP_PRIVATE_KEY[_PATH] is not set")


def build_jwt(app_id: str, private_key_pem: str,
              now: float | None = None) -> str:
    """Short-lived RS256 JWT for App-level API calls (max 10 min)."""
    import jwt
    at = int(now if now is not None else time.time())
    payload = {"iat": at - 60, "exp": at + 540, "iss": str(app_id)}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def _api(method: str, path: str, *,
         bearer: str, data: dict | None = None) -> Any:
    body = json.dumps(data or {}).encode() if data is not None else None
    req = urllib.request.Request(
        API + path, data=body, method=method,
        headers={"Authorization": f"Bearer {bearer}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def find_installation_id(account: str, app_jwt: str) -> int:
    rows = _api("GET", "/app/installations", bearer=app_jwt)
    for inst in rows if isinstance(rows, list) else []:
        if str((inst.get("account") or {}).get("login") or "") == account:
            return int(inst["id"])
    raise RuntimeError(f"no installation for account {account!r}")


def mint_installation_token(installation_id: int, app_jwt: str) -> dict:
    return _api("POST", f"/app/installations/{installation_id}/access_tokens",
                bearer=app_jwt, data={})


def list_installation_repos(install_token: str) -> list[dict]:
    """All repos visible to the installation (paginated)."""
    out: list[dict] = []
    page = 1
    while True:
        data = _api("GET", f"/installation/repositories?per_page=100&page={page}",
                    bearer=install_token)
        repos = data.get("repositories", []) if isinstance(data, dict) else []
        out.extend(repos)
        if len(repos) < 100:
            break
        page += 1
    return out


def active_repo_names(repos: list[dict]) -> list[str]:
    """Active = visible, not archived, not disabled. Forks included."""
    names = []
    for r in repos:
        if r.get("archived") or r.get("disabled"):
            continue
        name = r.get("full_name")
        if name:
            names.append(str(name))
    return sorted(names)


@dataclass
class AppAuth:
    app_id: str
    account: str = "mitchins"
    installation_id: int | None = None
    _token: str = ""
    _expires_at: float = 0.0

    @classmethod
    def from_env(cls) -> "AppAuth":
        app_id = (os.environ.get("GITHUB_APP_ID") or "").strip()
        if not app_id:
            raise RuntimeError("GITHUB_APP_ID is not set")
        account = (os.environ.get("GITHUB_ACCOUNT") or "mitchins").strip()
        raw = (os.environ.get("GITHUB_INSTALLATION_ID") or "").strip()
        return cls(app_id=app_id, account=account,
                   installation_id=int(raw) if raw else None)

    def token(self) -> str:
        """Cached installation token, refreshed with a 10-minute margin."""
        if self._token and time.time() < self._expires_at - 600:
            return self._token
        key = load_private_key()
        app_jwt = build_jwt(self.app_id, key)
        inst = self.installation_id
        if inst is None:
            inst = find_installation_id(self.account, app_jwt)
            self.installation_id = inst
        minted = mint_installation_token(inst, app_jwt)
        self._token = str(minted["token"])
        try:
            import datetime as _dt
            exp = _dt.datetime.fromisoformat(
                str(minted["expires_at"]).replace("Z", "+00:00")).timestamp()
        except Exception:
            exp = time.time() + 3600
        self._expires_at = exp
        return self._token
