"""환경 변수 로딩."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"


def _load_dotenv() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv()


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")


# Proxy 풀 — anti-scraping 강한 사이트 (슈퍼루키 등) 의 source 가 use_proxy=True 면 회전 사용.
# 형식: ["http://user:pass@host:port", ...]
# .env 에 PROXIES="url1,url2,url3" 형태로 콤마 구분 리스트 허용.
def _load_proxies() -> list[str]:
    raw = os.getenv("PROXIES", "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]

PROXIES = _load_proxies()
