"""검증 통과한 메뉴들 사이 중복 제거.

원리:
  1. URL 구조 기반 — 같은 path 인데 query-filter 가 더 많은 쪽은 subset.
     예: /jobs ⊇ /jobs?type=hot ⊇ /jobs?type=hot&career_level=1
     → /jobs (no-filter) 만 keep. peoplenjob 의 11개 type/career_level 변종 자동 drop.
  2. sample_links external_id 비교 — 70% 이상 겹치면 점수 낮은 쪽 drop.
  3. 겹침 없음 — 둘 다 남김.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import parse_qsl, urlparse

from .menu_validator import ValidationResult


OVERLAP_THRESHOLD = 0.7  # 70% 이상 겹치면 중복


def _is_query_filter_subset(cand_url: str, kept_url: str) -> bool:
    """cand 가 kept 의 query-filter subset 인지.
    같은 host + path + kept 의 모든 query 가 cand 에 포함 + cand 에 추가 query 있음.
    """
    cp = urlparse(cand_url)
    kp = urlparse(kept_url)
    if cp.netloc != kp.netloc or cp.path != kp.path:
        return False
    cq = dict(parse_qsl(cp.query, keep_blank_values=True))
    kq = dict(parse_qsl(kp.query, keep_blank_values=True))
    if not all(cq.get(k) == v for k, v in kq.items()):
        return False
    return len(cq) > len(kq)


@dataclass
class DedupeDecision:
    kept: list[ValidationResult] = field(default_factory=list)
    dropped: list[tuple[ValidationResult, str]] = field(default_factory=list)  # (item, reason)


def _normalize(urls: Iterable[str]) -> set[str]:
    """URL → 비교 가능한 키. ID 기반 비교가 가능하도록 정규화.

    - external_id 가 추출되면 (path-마지막-숫자 / id-suffix query / _jsid 등)
      그 ID 만 키로 사용 (예: '10434', 'E20260428005').
      → 같은 채용 공고가 다른 menuId 로 노출돼도 dedupe 가능.
    - external_id 추출 실패 시 fallback 으로 URL 자체.
    """
    from ..extractors.external_id import extract_external_id

    out = set()
    for u in urls:
        if not u:
            continue
        u = u.strip()
        try:
            eid = extract_external_id(u)
        except Exception:  # noqa: BLE001
            eid = u
        out.add(eid or u)
    return out


def _score(v: ValidationResult) -> tuple[int, float]:
    """더 큰 list / 더 높은 subject_link_ratio 인 것을 우선."""
    return (v.list_rows, v.subject_link_ratio)


def dedupe(items: list[ValidationResult]) -> DedupeDecision:
    # PASS 한 것만 대상
    passed = [v for v in items if v.ok]
    # 정렬 우선순위:
    #   1) query 파라미터 적은 쪽 (filter 안 걸린 'all' 메뉴 우선)
    #   2) list_rows 큰 쪽
    #   3) subject_link_ratio 높은 쪽
    passed.sort(key=lambda v: (
        len(parse_qsl(urlparse(v.url).query)),  # 적을수록 먼저
        -v.list_rows,
        -v.subject_link_ratio,
    ))

    kept: list[ValidationResult] = []
    kept_sets: list[set[str]] = []
    dropped: list[tuple[ValidationResult, str]] = []

    for cand in passed:
        # 1단계: URL-구조 기반 subset 검사 — 이미 kept 된 어떤 URL 의 filter-subset
        # 이면 즉시 drop (sample_links 동등성 무관)
        url_subset_of = next(
            (k for k in kept if _is_query_filter_subset(cand.url, k.url)),
            None,
        )
        if url_subset_of is not None:
            dropped.append((cand, f"query-filter subset of {url_subset_of.url}"))
            continue

        cand_set = _normalize(cand.sample_links)
        if not cand_set:
            kept.append(cand)
            kept_sets.append(cand_set)
            continue

        # 2단계: sample_links external_id 70% 이상 겹치면 drop
        merged = False
        for i, ks in enumerate(kept_sets):
            if not ks:
                continue
            inter = cand_set & ks
            cov_in_kept = len(inter) / max(len(cand_set), 1)
            if cov_in_kept >= OVERLAP_THRESHOLD:
                dropped.append((cand, f"covered by {kept[i].url} ({cov_in_kept:.0%})"))
                merged = True
                break
        if not merged:
            kept.append(cand)
            kept_sets.append(cand_set)

    return DedupeDecision(kept=kept, dropped=dropped)
