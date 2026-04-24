"""
회전형 proxy 풀.

data/proxies.json 에서 인증 정보 로드. 각 요청마다 랜덤 proxy 한 개 선택.
403 받으면 해당 proxy 를 일정 시간 cool-down 큐에 넣고 다른 proxy 로 재시도.

파일 없거나 비어있으면 풀 비활성 — fetch() 는 직접 연결로 동작.
"""

import json
import os
import random
import time
from pathlib import Path
from typing import Optional


_PROXIES_PATH = (
    Path(os.environ.get("PROXIES_PATH", ""))
    if os.environ.get("PROXIES_PATH")
    else Path(__file__).resolve().parent.parent / "data" / "proxies.json"
)

# 403 받은 proxy 를 이 시간만큼 잠시 빼둠 (초)
COOLDOWN_SECONDS = float(os.environ.get("PROXY_COOLDOWN_SECONDS", "180"))


class ProxyPool:
    def __init__(self, proxies: list[dict]):
        self._proxies = list(proxies)
        # host:port → 다시 사용할 시각 (epoch)
        self._cooldown_until: dict[str, float] = {}

    @classmethod
    def from_file(cls, path: Path = _PROXIES_PATH) -> "ProxyPool":
        if not path.exists():
            return cls([])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return cls([])
            return cls(data)
        except Exception:
            return cls([])

    def __bool__(self) -> bool:
        return bool(self._proxies)

    def __len__(self) -> int:
        return len(self._proxies)

    def _key(self, p: dict) -> str:
        return f"{p['host']}:{p['port']}"

    @staticmethod
    def to_url(p: dict) -> str:
        u, pw = p.get("username"), p.get("password")
        auth = f"{u}:{pw}@" if u else ""
        return f"http://{auth}{p['host']}:{p['port']}"

    def pick(self, exclude: Optional[set[str]] = None) -> Optional[dict]:
        """사용 가능한 proxy 중 하나 랜덤 선택. exclude 는 host:port 셋."""
        now = time.time()
        exclude = exclude or set()
        available = [
            p for p in self._proxies
            if self._key(p) not in exclude
            and self._cooldown_until.get(self._key(p), 0) <= now
        ]
        if not available:
            # 전부 cool-down 또는 exclude — 그냥 exclude 만 빼고 강제 선택
            available = [p for p in self._proxies if self._key(p) not in exclude]
        if not available:
            return None
        return random.choice(available)

    def mark_failed(self, p: dict, seconds: float = COOLDOWN_SECONDS) -> None:
        """이 proxy 를 잠시 풀에서 빼둠."""
        self._cooldown_until[self._key(p)] = time.time() + seconds


# 모듈 단위 싱글턴 — 호출자는 그냥 GLOBAL 만 import
GLOBAL = ProxyPool.from_file()
