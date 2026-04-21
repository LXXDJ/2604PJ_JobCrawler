"""
Slack Incoming Webhook으로 헬스체크 결과 전송.

외부 API:
    send(webhook_url, report, only_issues=True) -> bool (전송 여부)

설계 원칙:
  - 순수 함수: HealthReport 객체를 받아서 포매팅+전송만 함
    (healthcheck.py와 느슨하게 결합, 같은 report를 다른 채널로 보내려면 포매터만 추가하면 됨)
  - Slack 실패는 조용히 삼킴: webhook 쪽이 죽어도 crawl은 끝까지 돌아야 함
  - only_issues=True가 기본: 매일 OK 알림이 오면 피로감 → 진짜 문제 있을 때만 푸시
"""

import json
import requests

from site_labels import label


def send(webhook_url: str, report, *, only_issues: bool = True, timeout: int = 10) -> bool:
    """
    헬스체크 리포트를 Slack으로 전송.

    Returns:
        True  — 실제로 메시지를 보냄
        False — only_issues 조건으로 스킵했거나, 전송 실패
    """
    if only_issues and not report.has_issues:
        return False

    payload = _build_payload(report)

    try:
        resp = requests.post(webhook_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return True
    except Exception as e:
        # Slack 실패가 crawl 전체를 깨면 안 됨 — 로그에만 남기고 넘어감
        print(f"      [WARN] Slack 전송 실패: {type(e).__name__}: {e}")
        return False


def send_crawl_summary(
    webhook_url: str,
    per_site_stats: list,
    started_at: str,
    finished_at: str,
    elapsed: str,
    *,
    timeout: int = 10,
) -> bool:
    """
    배치 런 종료 요약을 Slack 으로 전송.

    per_site_stats: list of dict with keys:
        site_id        (str)
        status         ("ok" | "error")
        new_count      (int)
        updated_count  (int)
        error          (str | None)
    """
    payload = _build_crawl_summary_payload(
        per_site_stats, started_at, finished_at, elapsed
    )
    try:
        resp = requests.post(webhook_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"      [WARN] Slack 전송 실패: {type(e).__name__}: {e}")
        return False


# ============================================================
# Slack Block Kit 페이로드 구성
# ============================================================

def _build_payload(report) -> dict:
    buckets = report.by_status
    error_count = len(buckets["error"])
    warn_count = len(buckets["warn"])
    ok_count = len(buckets["ok"])

    if error_count > 0:
        header_text = f":rotating_light: 크롤러 오류 {error_count}건"
    elif warn_count > 0:
        header_text = f":warning: 크롤러 경고 {warn_count}건"
    else:
        header_text = ":white_check_mark: 크롤러 정상"

    summary = f"OK {ok_count} / WARN {warn_count} / ERROR {error_count}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"*{summary}*  ·  {report.generated_at}"},
        ]},
    ]

    # 문제가 있는 사이트들을 error → warn 순으로 섹션 추가
    problem_sites = buckets["error"] + buckets["warn"]
    if problem_sites:
        blocks.append({"type": "divider"})
        for site in problem_sites:
            badge = {"error": ":x:", "warn": ":warning:"}[site.status]
            lines = [f"{badge} *{label(site.site_id)}*"]
            for issue in site.issues:
                lines.append(f"• {issue}")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            })

    # 알림 프리뷰(푸시 알림 텍스트)용 fallback
    fallback = f"{header_text} — {summary}"
    return {"text": fallback, "blocks": blocks}


def _build_crawl_summary_payload(per_site_stats, started_at, finished_at, elapsed) -> dict:
    ok_sites = [s for s in per_site_stats if s["status"] == "ok"]
    err_sites = [s for s in per_site_stats if s["status"] == "error"]
    total = len(per_site_stats)
    total_new = sum(s.get("new_count") or 0 for s in per_site_stats)
    total_upd = sum(s.get("updated_count") or 0 for s in per_site_stats)

    if err_sites:
        header_text = f":rotating_light: 크롤 완료: 성공 {len(ok_sites)} / 실패 {len(err_sites)} (전체 {total})"
    else:
        header_text = f":white_check_mark: 크롤 완료: {len(ok_sites)}/{total} 성공"

    context_text = (
        f"소요 *{elapsed}*  ·  신규 *{total_new}* / 재확인 *{total_upd}*  ·  {started_at} → {finished_at}"
    )

    # 성공 사이트 + 실패 사이트를 한 덩어리 mrkdwn 으로 — 사이트 수가 많아져도 블록 수 폭발 안 함
    lines = []
    for s in per_site_stats:
        name = label(s["site_id"])
        if s["status"] == "ok":
            lines.append(
                f":white_check_mark: *{name}* — 신규 {s.get('new_count') or 0}, "
                f"재확인 {s.get('updated_count') or 0}"
            )
        else:
            err = (s.get("error") or "").strip().replace("\n", " ")
            if len(err) > 200:
                err = err[:200] + "…"
            lines.append(f":x: *{name}* — {err or 'unknown error'}")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": context_text}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines) or "_(사이트 없음)_"}},
    ]
    fallback = f"{header_text} · 신규 {total_new} / 재확인 {total_upd}"
    return {"text": fallback, "blocks": blocks}
