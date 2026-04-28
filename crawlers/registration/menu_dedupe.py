"""검증 통과한 메뉴들 사이 중복 제거.

원리:
  각 메뉴의 sample_links (= 공고 상세 URL set) 을 비교.

  - 메뉴A ⊇ 메뉴B  (B의 모든 링크가 A에 포함) → B 제거 (A가 상위)
  - 메뉴A ∩ 메뉴B 가 N% 이상 → 중복으로 보고 점수 낮은 쪽 제거
  - 겹침 없음 → 둘 다 남김
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .menu_validator import ValidationResult


OVERLAP_THRESHOLD = 0.7  # 70% 이상 겹치면 중복


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
    # 점수 높은 순으로 정렬 (큰 메뉴부터 시작 → 작은 메뉴를 그 안에 흡수 가능)
    passed.sort(key=_score, reverse=True)

    kept: list[ValidationResult] = []
    kept_sets: list[set[str]] = []
    dropped: list[tuple[ValidationResult, str]] = []

    for cand in passed:
        cand_set = _normalize(cand.sample_links)
        if not cand_set:
            # sample_links 가 비어있으면 비교 불가 → 그냥 keep
            kept.append(cand)
            kept_sets.append(cand_set)
            continue

        merged = False
        for i, ks in enumerate(kept_sets):
            if not ks:
                continue
            inter = cand_set & ks
            # 후보가 기존 메뉴에 거의 다 포함되면 drop
            cov_in_kept = len(inter) / max(len(cand_set), 1)
            if cov_in_kept >= OVERLAP_THRESHOLD:
                dropped.append((cand, f"covered by {kept[i].url} ({cov_in_kept:.0%})"))
                merged = True
                break
        if not merged:
            kept.append(cand)
            kept_sets.append(cand_set)

    return DedupeDecision(kept=kept, dropped=dropped)
