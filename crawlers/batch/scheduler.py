"""active site 들 순회하며 batch 실행."""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

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


def _default_progress(msg: str) -> None:
    print(msg, flush=True)
    # 일부 환경 (run_batch_hourly.bat 의 redirect) 에서는 stdout buffering 회피
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def run_batch(
    *,
    site_ids: Optional[Iterable[str]] = None,
    fetch_details: bool = True,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> BatchReport:
    progress = progress_cb or _default_progress
    if site_ids:
        sites = [s for s in list_sites() if s["id"] in set(site_ids)]
    else:
        sites = list_sites(status="active")

    rep = BatchReport(total_sites=len(sites))
    progress(f"[batch] {len(sites)} site(s) — start")

    for i, site in enumerate(sites):
        progress(f"[{i+1}/{len(sites)}] {site['id']} ({site.get('name') or '-'})  start...")
        t0 = time.time()
        sr = run_site(site["id"], fetch_details=fetch_details, progress_cb=progress)
        dt = time.time() - t0
        rep.site_reports.append(sr)

        if sr.success:
            rep.succeeded += 1
            progress(f"[{i+1}/{len(sites)}] {site['id']}  ok  rows={sr.rows_seen} +{sr.inserted} "
                     f"~{sr.updated} ={sr.unchanged}  ({dt:.1f}s)")
        else:
            rep.failed += 1
            progress(f"[{i+1}/{len(sites)}] {site['id']}  FAIL  {sr.error}  ({dt:.1f}s)")
        rep.inserted += sr.inserted
        rep.updated += sr.updated
        rep.closed += sr.closed

        if i < len(sites) - 1 and interval_sec > 0:
            time.sleep(interval_sec)

    progress(f"[batch] done  ok={rep.succeeded}  fail={rep.failed}  +{rep.inserted}")
    return rep
