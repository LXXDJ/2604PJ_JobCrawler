"""HeyKorean Job (https://job.heykorean.com/) 등록.

API 가 표준 페이지네이션 JSON 으로 잘 노출됨 → /api/job/list 직접 사용.
상세 페이지는 SPA shell 만 반환하므로 skip_detail=True (list 의 title 로 충분).

  - list endpoint: GET /api/job/list?page=N   (50건/페이지)
  - 응답 구조: { "list": [...], "platinum": [...], "page", "total", "last_page" }
  - row 필드:  id, title, ...
  - 사람용 detail URL: https://job.heykorean.com/job/view/{id}
"""
from __future__ import annotations

from crawlers.extractors.api_schema import ApiSchema
from crawlers.infra.sites_repo import upsert_site


SITE_ID = "heykorean"
HOME_URL = "https://job.heykorean.com/"
SITE_NAME = "HeyKorean Job"


def main() -> None:
    schema = ApiSchema(
        api_url_pattern="https://job.heykorean.com/api/job/list?page={page}",
        base_url="https://job.heykorean.com/api/job/list",
        list_path="list",
        id_field="id",
        title_field="title",
        page_param="page",
        size_param=None,
        page_size=50,
        detail_url_template="https://job.heykorean.com/job/view/{id}",
    )
    source = {
        "url": "https://job.heykorean.com/api/job/list",
        "label": "full",
        "fetcher": "api",
        "api_schema": schema.to_dict(),
        "skip_detail": True,
        # 초기 시드용 — 18.4만 건 (last_page=3682) 한 번에 적재.
        # 시드 끝나면 증분이라 page 1~2 에서 break 됨. 한도 남겨도 무해하지만
        # 안전 천장으로 남겨둠.
        "max_pages": 4000,
    }
    upsert_site(
        SITE_ID, HOME_URL, name=SITE_NAME,
        status="active", status_reason=None, sources=[source],
    )
    print(f"registered: {SITE_ID} ({SITE_NAME})  source api → /api/job/list")


if __name__ == "__main__":
    main()
