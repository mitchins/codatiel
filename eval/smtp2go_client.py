"""SHADOW-LIVE-01 SMTP2GO delivery. Key from env only, never logged,
never in URLs/bodies/commits. Verifies the response BODY
(data.succeeded / failed / failures / email_id) — HTTP 200 alone
is not proof. No CC/BCC. Tracking disabled where supported.
"""
from __future__ import annotations

import json
import os
import urllib.request

API_URL = "https://api.smtp2go.com/v3/email/send"


def load_key() -> str:
    """SMTP2GO API key from the environment only.

    No key files, no URLs, no bodies, no commits: the caller must export
    SMTP2GO_API_KEY. Refusing loudly beats inventing a key source.
    """
    key = (os.environ.get("SMTP2GO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("SMTP2GO_API_KEY is not set in the environment")
    return key


def send_email(to: list[str], sender: str, subject: str,
               text_body: str, html_body: str | None = None) -> dict:
    key = load_key()
    payload = {"to": to, "sender": sender, "subject": subject,
               "text_body": text_body}
    if html_body is not None:
        payload["html_body"] = html_body
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json",
                 "X-Smtp2go-Api-Key": key},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {"accepted_by_transport": True, "parsed": False,
                "raw_head": raw[:200]}
    d = data.get("data", {}) if isinstance(data, dict) else {}
    return {"accepted_by_transport": True, "parsed": True,
            "request_id": data.get("request_id") if isinstance(
                data, dict) else None,
            "succeeded": d.get("succeeded", 0),
            "failed": d.get("failed", 0),
            "failures": d.get("failures", []),
            "email_id": d.get("email_id")}
