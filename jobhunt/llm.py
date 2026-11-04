"""LLM 호출 — Claude Code headless (`claude -p`) 를 구독 인증으로 사용.

로컬 전용 개인 프로젝트라 API 키 대신 claude.ai 구독 한도를 쓴다.
API 로 바꾸고 싶으면 이 파일의 call_json() 만 anthropic SDK 로 교체하면 된다.

주의:
- --bare 는 OAuth(구독) 를 안 읽으므로 쓰지 않는다.
- --system-prompt 로 기본 Claude Code 시스템 프롬프트(~10K 토큰)를 교체해 사용량 절약.
- 프로젝트 CLAUDE.md 등을 안 읽도록 빈 임시 디렉터리에서 실행.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_MODEL = os.getenv("JOBHUNT_LLM_MODEL", "haiku")


class LLMError(RuntimeError):
    pass


def _fallback_exe() -> str | None:
    """작업 스케줄러 환경은 PATH 가 다를 수 있어 기본 설치 위치도 확인."""
    for p in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if p.exists():
            return str(p)
    return None


def _run(prompt: str, *, system: str, model: str, extra: list[str], cwd: str, timeout: int) -> dict:
    """claude -p 1회 실행 → 결과 JSON (output-format json 의 최상위 객체)."""
    exe = shutil.which("claude") or _fallback_exe()
    if not exe:
        raise LLMError("claude CLI 를 찾을 수 없음 (Claude Code 설치/로그인 필요)")
    cmd = [exe, "-p", "--model", model, "--output-format", "json",
           "--system-prompt", system, "--no-session-persistence", *extra]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
            cwd=cwd, timeout=timeout,
            # 작업 스케줄러(pythonw)에서 돌 때 호출마다 콘솔 창이 뜨지 않게
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as e:
        # 응답 없는 호출 하나 때문에 회차 전체가 죽지 않도록 — 해당 배치만 건너뛰고 다음 회차에 재시도
        raise LLMError(f"claude 응답 없음 ({timeout}s 초과)") from e
    if proc.returncode != 0:
        raise LLMError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"claude 출력 파싱 실패: {proc.stdout[:300]}") from e
    if out.get("is_error"):
        raise LLMError(f"claude error: {out.get('result') or out.get('subtype')}")
    return out


def call_json(prompt: str, *, system: str, schema: dict, model: str = DEFAULT_MODEL,
              timeout: int = 300) -> dict:
    """프롬프트를 보내고 schema 에 맞는 JSON(dict) 을 돌려받는다."""
    with tempfile.TemporaryDirectory(prefix="jobhunt_llm_") as cwd:
        out = _run(prompt, system=system, model=model, cwd=cwd, timeout=timeout,
                   extra=["--json-schema", json.dumps(schema, ensure_ascii=False), "--tools", ""])
    data = out.get("structured_output")
    if data is None:
        raise LLMError(f"structured_output 없음: {str(out.get('result'))[:300]}")
    return data


def read_images(prompt: str, *, image_dir: str, system: str, model: str = DEFAULT_MODEL,
                timeout: int = 600) -> str:
    """image_dir 안의 이미지를 Read 도구로 읽게 해서 텍스트 응답을 받는다 (Read 외 도구는 막음)."""
    out = _run(prompt, system=system, model=model, cwd=image_dir, timeout=timeout,
               extra=["--tools", "Read", "--allowedTools", "Read"])
    return out.get("result") or ""
