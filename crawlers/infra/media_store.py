"""원격 미디어(이미지/첨부) 를 로컬 디스크에 다운로드 — 사이트가 공고를 내려도 보존.

저장 경로:
    data/media/<site_id>/<sha256[:2]>/<sha256>.<ext>

이미 있으면 재다운로드 안 함 (sha256 기반 dedupe).
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[2]
MEDIA_ROOT = ROOT / "data" / "media"

DEFAULT_MAX_BYTES = 20_000_000  # 20MB / 파일
DEFAULT_TIMEOUT = 20

_EXT_RE = re.compile(r"\.([a-zA-Z0-9]{2,6})(?:\?|$)")
# server-side dynamic page 확장자 — file extension 으로 취급하지 않음
_SERVER_EXTS = {"php", "asp", "aspx", "jsp", "jspx", "do", "action", "cgi", "py", "rb"}
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class Stored:
    src: str
    local_path: Optional[str] = None  # ROOT 기준 상대경로
    sha256: Optional[str] = None
    size: Optional[int] = None
    content_type: Optional[str] = None
    ext: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _ext_from_url(url: str) -> str:
    m = _EXT_RE.search(urlparse(url).path)
    if not m:
        return ""
    ext = m.group(1).lower()[:6]
    if ext in _SERVER_EXTS:
        return ""  # PHP/JSP 등은 file ext 가 아님
    return ext


def _ext_from_ct(ct: str) -> str:
    if not ct:
        return ""
    ct = ct.split(";")[0].strip().lower()
    ext = mimetypes.guess_extension(ct) or ""
    return ext.lstrip(".")[:6]


def download(
    url: str,
    site_id: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    referer: Optional[str] = None,
) -> Stored:
    """url 의 바이너리를 디스크에 저장. 이미 같은 sha256 파일이 있으면 그대로 사용."""
    out = Stored(src=url)
    if not url or not url.startswith(("http://", "https://")):
        out.error = "invalid url"
        return out

    headers = {"User-Agent": _UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer

    try:
        with requests.get(url, headers=headers, timeout=timeout, stream=True) as r:
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            buf = bytearray()
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        out.error = f"exceeds max_bytes ({max_bytes})"
                        return out
        data = bytes(buf)
        if not data:
            out.error = "empty body"
            return out
        sha = hashlib.sha256(data).hexdigest()
        ext = _ext_from_url(url) or _ext_from_ct(ct) or "bin"
        site_id_safe = re.sub(r"[^A-Za-z0-9_.-]", "_", site_id)[:64]
        rel_dir = Path("data") / "media" / site_id_safe / sha[:2]
        abs_dir = ROOT / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)
        rel_path = rel_dir / f"{sha}.{ext}"
        abs_path = ROOT / rel_path
        if not abs_path.exists():
            abs_path.write_bytes(data)
        out.local_path = rel_path.as_posix()
        out.sha256 = sha
        out.size = len(data)
        out.content_type = ct.split(";")[0].strip() or None
        out.ext = ext
    except requests.HTTPError as e:
        out.error = f"HTTP {e.response.status_code if e.response else '?'}"
    except Exception as e:  # noqa: BLE001
        out.error = f"{type(e).__name__}: {e}"
    return out


def absolute_path(rel: str) -> Path:
    """raw 에 저장된 ROOT-relative 경로를 절대 경로로."""
    return ROOT / rel
