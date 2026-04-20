"""
크롤러 디스패처

sites.json / REGISTERED_CRAWLS 엔트리를 받아서 적절한 크롤러를 호출.

라우팅 기준: entry["extraction_method"] → dom / api / embedded_json

Phase 1: DOM 신 경로 (dom_crawler) 구현
Phase 2: EmbeddedJSON 신 경로 (embedded_crawler) 구현
Phase 3: API 신 경로 (api_crawler) + camhr 이주 완료
Phase 이후: gnuboard_crawler / camhr_crawler 레거시 분기 제거. 모든 엔트리가
           extraction_method 로만 라우팅됨.
"""


def dispatch(entry: dict, db_path: str, http_config: dict):
    """
    단일 엔트리를 크롤러로 라우팅.

    Returns: 크롤러가 반환한 값 (보통 {"new": N, "updated": M})
    Raises:  크롤링 중 예외는 그대로 전파 (main.py 가 catch)
    """
    site_id = entry["site_id"]
    config = entry.get("config") or entry  # 신 스키마는 엔트리 자체가 config

    method = config.get("extraction_method") or entry.get("extraction_method")

    if method == "dom":
        import dom_crawler
        max_pages = (
            (config.get("pagination") or {}).get("max_pages")
            or (entry.get("config") or {}).get("max_pages")
        )
        return dom_crawler.crawl(
            site_id=site_id,
            config=config,
            db_path=db_path,
            max_pages=max_pages,
            http_config=http_config,
        )

    if method == "api":
        import api_crawler
        max_pages = (
            (config.get("pagination") or {}).get("max_pages")
            or (entry.get("config") or {}).get("max_pages")
        )
        return api_crawler.crawl(
            site_id=site_id,
            config=config,
            db_path=db_path,
            max_pages=max_pages,
            http_config=http_config,
        )

    if method == "embedded_json":
        import embedded_crawler
        max_pages = (
            (config.get("pagination") or {}).get("max_pages")
            or (entry.get("config") or {}).get("max_pages")
        )
        return embedded_crawler.crawl(
            site_id=site_id,
            config=config,
            db_path=db_path,
            max_pages=max_pages,
            http_config=http_config,
        )

    raise ValueError(
        f"[{site_id}] 어느 크롤러로도 라우팅 실패 — extraction_method={method!r} "
        f"(신 스키마의 dom/api/embedded_json 중 하나여야 함)"
    )
