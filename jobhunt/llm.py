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

DEFAULT_MODEL = os.getenv("JOBHUNT_LLM_MODEL", "haiku")


class LLMError(RuntimeError):
    pass


def call_json(prompt: str, *, system: str, schema: dict, model: str = DEFAULT_MODEL,
              timeout: int = 300) -> dict:
    """프롬프트를 보내고 schema 에 맞는 JSON(dict) 을 돌려받는다."""
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("claude CLI 를 찾을 수 없음 (Claude Code 설치/로그인 필요)")
    cmd = [
        exe, "-p",
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--system-prompt", system,
        "--tools", "",
        "--no-session-persistence",
    ]
    with tempfile.TemporaryDirectory(prefix="jobhunt_llm_") as cwd:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
            cwd=cwd, timeout=timeout,
        )
    if proc.returncode != 0:
        raise LLMError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"claude 출력 파싱 실패: {proc.stdout[:300]}") from e
    if out.get("is_error"):
        raise LLMError(f"claude error: {out.get('result') or out.get('subtype')}")
    data = out.get("structured_output")
    if data is None:
        raise LLMError(f"structured_output 없음: {str(out.get('result'))[:300]}")
    return data
