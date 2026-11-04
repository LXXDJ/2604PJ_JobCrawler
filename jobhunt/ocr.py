"""이미지 통짜 공고 → 텍스트. 사람인·잡코리아 공고의 약 20% 가 본문 전체가 이미지라 LLM 분석에서 빠지던 문제.

방식: 이미지를 받아 폭 1000px 이하로 줄이고 세로 1100px 씩 잘라(40px 겹침) claude -p 에 Read 도구로 읽힌다.
  - 공고 이미지는 860×5000 처럼 세로로 길어서 통째로 넣으면 축소돼 글자가 뭉개짐
  - Windows 내장 OCR(ko) 도 시험했지만 영문 기술 용어(ChatGPT, RAG, MCP 등)가 깨져서 기술 스택 분석에 부적합 (2026-11-03)
결과는 postings.body 에 넣고 ocr_at 을 찍는다 → 다음 extract 부터 일반 공고처럼 분석된다.
"""
from __future__ import annotations

import base64
import io
import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from curl_cffi import requests
from PIL import Image

from . import batch_status, llm
from .db import get_conn

MAX_WIDTH = 1000
CHUNK_H = 1100
OVERLAP = 40
MAX_CHUNKS = 14             # 공고 1건당 상한 (≈ 세로 15,000px)
MIN_SIDE = 200              # 이보다 작은 이미지는 로고·아이콘으로 보고 버림

SYSTEM = "너는 이미지 속 글자를 정확히 옮겨 적는 OCR 도구다. 영문 기술 용어는 철자 그대로 적는다."


@dataclass
class OcrReport:
    processed: int = 0
    no_image: int = 0
    errors: list[str] = field(default_factory=list)


# 이미지 요청임을 밝혀야 함 — ifh.cc·imgur 같은 외부 호스팅은 일반 요청에 이미지 대신 HTML 뷰어 페이지를 준다 (2026-11-04)
_IMG_HEADERS = {"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "Sec-Fetch-Dest": "image"}


def _download(s: requests.Session, page_url: str, src: str) -> Image.Image | None:
    if src.startswith("data:image/"):   # 본문에 base64 로 박힌 이미지 (잡코리아 일부)
        content = base64.b64decode(src.split(",", 1)[1])
    else:
        url = "https:" + src if src.startswith("//") else urljoin(page_url, src)
        r = s.get(url, headers={"Referer": page_url, **_IMG_HEADERS}, timeout=30)
        if r.status_code != 200:
            return None
        content = r.content
    try:
        im = Image.open(io.BytesIO(content)).convert("RGB")
    except Exception:  # noqa: BLE001  이미지가 아닌 응답
        return None
    return im if min(im.size) >= MIN_SIDE else None


def _slices(images: list[Image.Image], out_dir: Path) -> list[str]:
    names: list[str] = []
    for im in images:
        w, h = im.size
        if w > MAX_WIDTH:
            im = im.resize((MAX_WIDTH, int(h * MAX_WIDTH / w)))
            w, h = im.size
        for top in range(0, h, CHUNK_H):
            if len(names) >= MAX_CHUNKS:
                return names
            name = f"part{len(names):02d}.jpg"
            im.crop((0, max(0, top - OVERLAP), w, min(h, top + CHUNK_H))).save(out_dir / name, quality=88)
            names.append(name)
    return names


def ocr(*, max_postings: int = 40, model: str = llm.DEFAULT_MODEL) -> OcrReport:
    """본문이 이미지뿐인 매칭 대표 공고 중 아직 OCR 안 한 것."""
    rep = OcrReport()
    s = requests.Session(impersonate="chrome124")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, url, body, body_images FROM postings
               WHERE matched=1 AND status='open' AND dup_of IS NULL AND detail_fetched_at IS NOT NULL
                 AND main_tasks IS NULL AND requirements IS NULL AND length(coalesce(body,'')) < 150
                 AND ocr_at IS NULL
               ORDER BY first_seen_at DESC, id DESC LIMIT ?""",
            (max_postings,),
        ).fetchall()
        for n, r in enumerate(rows):
            batch_status.progress(n, len(rows))
            srcs = json.loads(r["body_images"] or "[]")
            images = []
            for src in srcs:
                try:
                    im = _download(s, r["url"], src)
                except Exception:  # noqa: BLE001
                    im = None
                if im:
                    images.append(im)
                time.sleep(0.2)
            if not images:
                # 읽을 이미지가 없음 — 다시 시도해도 같으므로 ocr_at 만 찍고 넘어감
                conn.execute("UPDATE postings SET ocr_at=datetime('now','localtime') WHERE id=?", (r["id"],))
                conn.commit()
                rep.no_image += 1
                continue
            with tempfile.TemporaryDirectory(prefix="jobhunt_ocr_") as d:
                names = _slices(images, Path(d))
                prompt = ("현재 폴더의 이미지 " + ", ".join(names) + " 는 하나의 채용공고를 위에서부터 순서대로 자른 조각이다. "
                          "Read 도구로 순서대로 모두 읽고 공고 텍스트를 빠짐없이 그대로 옮겨 적어라. "
                          "조각 경계에서 겹친 줄은 한 번만. 설명·요약 없이 공고 텍스트만 출력.")
                try:
                    text = llm.read_images(prompt, image_dir=d, system=SYSTEM, model=model)
                except llm.LLMError as e:   # 일시 오류일 수 있어 ocr_at 을 안 찍음 → 다음 회차 재시도
                    rep.errors.append(f"{r['id']}: {str(e)[:200]}")
                    continue
            body = "\n".join(x for x in (r["body"], text.strip()) if x)
            conn.execute("UPDATE postings SET body=?, ocr_at=datetime('now','localtime') WHERE id=?",
                         (body, r["id"]))
            conn.commit()
            rep.processed += 1
    return rep
