"""매일 배치 진행 상태 — data/logs/daily_status.json 한 파일에 덮어쓴다.

배치(daily)가 단계 시작·진행·실패·완료를 기록하고, 오전 8시 점검(check)이 이 파일을 읽어
"진행 중 / 오류 / 미실행" 을 판정한다. progress() 는 daily 밖(단독 실행)에서 부르면 아무것도 안 한다.
"""
from __future__ import annotations

import ctypes
import json
import os
from datetime import datetime
from pathlib import Path

STATUS_PATH = Path(__file__).resolve().parents[1] / "data" / "logs" / "daily_status.json"

STEPS = [("collect", "수집"), ("companies", "회사정보"), ("ocr", "이미지"), ("extract", "공고분석"),
         ("career", "커리어"), ("dashboard", "대시보드"), ("notify", "알림")]
STEP_LABEL = dict(STEPS)

_active = False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATUS_PATH)


def update(**kw) -> None:
    st = load()
    st.update(kw)
    _save(st)


def start() -> None:
    global _active
    _active = True
    prev = load()
    _save({
        "state": "running", "pid": os.getpid(), "started_at": _now(), "finished_at": None,
        "step": None, "step_started_at": None, "step_done": None, "step_total": None, "progress_at": _now(),
        "completed_steps": [], "failures": [],
        # 미실행 알림에 "마지막 정상 완료" 를 쓰려고 이전 회차 값을 넘겨받음
        "last_done_at": prev.get("finished_at") if prev.get("state") == "done" else prev.get("last_done_at"),
    })


def step(name: str) -> None:
    update(step=name, step_started_at=_now(), step_done=None, step_total=None, progress_at=_now())


def progress(done: int, total: int) -> None:
    if _active:
        update(step_done=done, step_total=total, progress_at=_now())


def step_ok(name: str) -> None:
    st = load()
    update(completed_steps=st.get("completed_steps", []) + [name])


def step_failed(name: str, error: str) -> None:
    st = load()
    update(failures=st.get("failures", []) + [{"step": name, "error": error[:300], "at": _now(),
                                               "done": st.get("step_done"), "total": st.get("step_total")}])


def finish() -> dict:
    global _active
    _active = False
    st = load()
    st.update(state="failed" if st.get("failures") else "done", finished_at=_now(), step=None)
    _save(st)
    return st


def pid_alive(pid: int | None) -> bool:
    """Windows — os.kill(pid, 0) 은 Windows 에서 프로세스를 죽이므로 OpenProcess 로 확인."""
    if not pid:
        return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259     # STILL_ACTIVE
