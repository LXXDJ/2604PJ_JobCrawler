"""active site 들 순회하며 batch 실행."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..infra.sites_repo import list_sites
from .runner import SiteRunReport, run_site


DEFAULT_INTERVAL_SEC = 1.0  # 사이트 간 휴식


@dataclass
class BatchReport:
    total_sites: int = 0
    succeeded: int = 0
    failed: int = 0
    inserted: int = 0
    updated: int = 0
    closed: int = 0
    site_reports: list[SiteRunReport] = field(default_factory=list)


def run_batch(
    *,
    site_ids: Optional[Iterable[str]] = None,
    fetch_details: bool = True,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
) -> BatchReport:
    if site_ids:
        sites = [s for s in list_sites() if s["id"] in set(site_ids)]
    else:
        sites = list_sites(status="active")

    rep = BatchReport(total_sites=len(sites))
    for i, site in enumerate(sites):
        sr = run_site(site["id"], fetch_details=fetch_details)
        rep.site_reports.append(sr)

        if sr.success:
            rep.succeeded += 1
        else:
            rep.failed += 1
        rep.inserted += sr.inserted
        rep.updated += sr.updated
        rep.closed += sr.closed

        if i < len(sites) - 1 and interval_sec > 0:
            time.sleep(interval_sec)

    return rep
