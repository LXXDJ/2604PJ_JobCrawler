"""
헬스체크: crawl_runs 이력 기반으로 "사이트 크롤러가 조용히 고장났는지" 감지.

감지하는 네 가지 상황:
  1. 에러: 최근 N회 run 중 exception이 찍힌 run이 있음
  2. 0건: 가장 최근 완주한 run이 new=0 AND updated=0 (셀렉터 죽었을 가능성)
  3. 수집량 급감: 최근 (new+updated)가 이전 성공 평균의 X% 미만
     → new_count가 아니라 new+updated를 보는 이유: 초기 수집 후엔 대부분 updated로
       분류돼서 new는 0에 가까워짐. "그 run에서 본 게시글 총 수"가 안정적인 지표.
  4. 스테일: 마지막 성공 run이 N일 이상 전 (스케줄러가 안 도는 중)

이 모듈은 **순수 분석만** 함 — DB 읽어서 HealthReport 반환.
출력/Slack 전송 등은 호출하는 쪽이 결정. (나중에 Slack 어댑터 붙일 때
HealthReport 그대로 재사용하고 포매터만 바꾸면 됨.)
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


# ============================================================
# 결과 모델
# ============================================================

@dataclass
class SiteHealth:
    site_id: str
    status: str                       # "ok" | "warn" | "error"
    issues: list                      # 사람 읽기용 이슈 문자열 리스트
    # 판정에 쓴 원본 수치들 (디버깅/Slack 포매터용)
    recent_new: Optional[int] = None
    recent_updated: Optional[int] = None
    recent_total: Optional[int] = None       # new + updated (그 run에서 본 게시글 총수)
    avg_total_prior: Optional[float] = None  # 이전 성공 run들의 total 평균
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None
    recent_error_count: int = 0
    total_runs: int = 0


@dataclass
class HealthReport:
    generated_at: str
    sites: list = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return any(s.status != "ok" for s in self.sites)

    @property
    def by_status(self) -> dict:
        out = {"ok": [], "warn": [], "error": []}
        for s in self.sites:
            out[s.status].append(s)
        return out


# ============================================================
# 판정
# ============================================================

def analyze(
    db,
    expected_site_ids: list,
    *,
    drop_threshold: float = 0.5,
    stale_days: int = 1,
    history_runs: int = 7,
    error_window: int = 3,
) -> HealthReport:
    """
    expected_site_ids: crawl 대상으로 등록된 모든 site_id (REGISTERED_CRAWLS + sites.json).
                       DB에 run이 한 번도 없어도 등록돼 있으면 스테일/미실행으로 잡아야 함.

    drop_threshold: 이번 new_count가 이전 성공 평균의 이 배수 미만이면 "급감" 경고 (0.5 = 절반)
    stale_days    : 마지막 성공이 이 일수 이상 전이면 스테일
    history_runs  : 평균 계산에 사용할 최근 run 수
    error_window  : 에러 감지에 볼 최근 run 수
    """
    sites = [
        _analyze_site(db, site_id, drop_threshold, stale_days, history_runs, error_window)
        for site_id in expected_site_ids
    ]
    return HealthReport(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        sites=sites,
    )


def _analyze_site(db, site_id, drop_threshold, stale_days, history_runs, error_window):
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT started_at, finished_at, new_count, updated_count, error
            FROM crawl_runs
            WHERE source = ?
            ORDER BY started_at DESC
            LIMIT ?
        """, (site_id, history_runs)).fetchall()
    recent = [dict(r) for r in rows]

    issues = []
    latest = recent[0] if recent else None

    # ---- 1) 스테일 / 미실행 ----
    successful = [r for r in recent if r["error"] is None and r["finished_at"]]
    last_success_at = successful[0]["started_at"] if successful else None

    if not recent:
        issues.append("등록됐지만 한 번도 crawl 이력이 없음")
    elif not last_success_at:
        issues.append(f"성공한 run이 하나도 없음 (총 {len(recent)}회 시도, 모두 실패)")
    else:
        last_success_dt = datetime.fromisoformat(last_success_at)
        age = datetime.now() - last_success_dt
        if age > timedelta(days=stale_days):
            # timedelta → "Xd Yh" 포맷
            days, seconds = age.days, age.seconds
            hours = seconds // 3600
            issues.append(
                f"스테일: 마지막 성공이 {days}일 {hours}시간 전 (기준 {stale_days}일)"
            )

    # ---- 2) 에러 빈도 ----
    error_window_runs = recent[:error_window]
    error_count = sum(1 for r in error_window_runs if r["error"])
    last_error = next((r["error"] for r in recent if r["error"]), None)

    if error_count > 0:
        issues.append(
            f"최근 {len(error_window_runs)}회 중 {error_count}회 에러 "
            f"(최근 에러: {_trim(last_error, 80)})"
        )

    # ---- 3) 0건 감지 ----
    # 가장 최근 run이 완주했는데 new/updated가 전부 0이면 파싱 실패 가능성 큼
    # (주의: 정말 게시판이 비어있는 신규 사이트일 수도 있으니 warn 수준)
    if latest and latest["finished_at"] and not latest["error"]:
        if (latest["new_count"] or 0) == 0 and (latest["updated_count"] or 0) == 0:
            issues.append("최근 run: 신규/재확인 모두 0건 (셀렉터/API 깨졌을 가능성)")

    # ---- 4) 수집량 급감 ----
    # "수집량" = new + updated (그 run에서 본 게시글 총수).
    # new만 보면 초기 수집 이후 항상 0 근처가 돼서 거짓 경고.
    def _total(r):
        return (r["new_count"] or 0) + (r["updated_count"] or 0)

    latest_total = _total(successful[0]) if successful else None
    avg_total_prior = None
    if len(successful) >= 2:
        prior_totals = [_total(r) for r in successful[1:]]
        avg_total_prior = sum(prior_totals) / len(prior_totals)
        if avg_total_prior > 0 and latest_total < avg_total_prior * drop_threshold:
            issues.append(
                f"수집량 급감: 이번 {latest_total}건(신규+재확인) "
                f"< 이전 평균 {avg_total_prior:.1f}건의 {int(drop_threshold * 100)}%"
            )

    # ---- 최종 상태 ----
    if not issues:
        status = "ok"
    elif error_count > 0:
        status = "error"  # 예외 찍힌 건 더 강한 신호
    else:
        status = "warn"

    return SiteHealth(
        site_id=site_id,
        status=status,
        issues=issues,
        recent_new=(latest or {}).get("new_count"),
        recent_updated=(latest or {}).get("updated_count"),
        recent_total=latest_total,
        avg_total_prior=avg_total_prior,
        last_success_at=last_success_at,
        last_error=last_error,
        recent_error_count=error_count,
        total_runs=len(recent),
    )


def _trim(s, n):
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


# ============================================================
# 포매터 (콘솔/로그용)
# ============================================================

def format_text(report: HealthReport, show_ok: bool = True) -> str:
    """
    사람이 읽을 텍스트 리포트.
    show_ok=False면 문제 있는 사이트만 출력 (간결한 자동 리포트용).
    """
    lines = []
    lines.append("=" * 60)
    lines.append(f"Health Report @ {report.generated_at}")
    lines.append("=" * 60)

    if not report.sites:
        lines.append("  (검사할 사이트 없음)")
        return "\n".join(lines)

    buckets = report.by_status
    lines.append(
        f"  OK: {len(buckets['ok'])}, "
        f"WARN: {len(buckets['warn'])}, "
        f"ERROR: {len(buckets['error'])}"
    )
    lines.append("")

    # error → warn → ok 순으로 (눈에 띄는 것부터)
    for bucket_name in ("error", "warn", "ok"):
        if bucket_name == "ok" and not show_ok:
            continue
        for s in buckets[bucket_name]:
            symbol = {"ok": "[OK]   ", "warn": "[WARN] ", "error": "[ERROR]"}[s.status]
            lines.append(f"  {symbol} {s.site_id}")
            for issue in s.issues:
                lines.append(f"           - {issue}")

    if not show_ok and buckets["ok"]:
        lines.append(f"  (+ 정상 {len(buckets['ok'])}개 생략)")

    return "\n".join(lines)
