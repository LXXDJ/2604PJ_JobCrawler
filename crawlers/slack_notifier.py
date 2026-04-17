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
            lines = [f"{badge} *{site.site_id}*"]
            for issue in site.issues:
                lines.append(f"• {issue}")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            })

    # 알림 프리뷰(푸시 알림 텍스트)용 fallback
    fallback = f"{header_text} — {summary}"
    return {"text": fallback, "blocks": blocks}
