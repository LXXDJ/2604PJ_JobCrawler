"""Slack 웹훅 알림."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import SLACK_WEBHOOK_URL


def slack_notify(text: str, *, blocks: list | None = None, timeout: int = 10) -> bool:
    if not SLACK_WEBHOOK_URL:
        return False
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.URLError:
        return False
