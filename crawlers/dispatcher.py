"""
크롤러 디스패처

sites.json / REGISTERED_CRAWLS 엔트리를 받아서 적절한 크롤러를 호출.

라우팅 우선순위:
    1. entry["extraction_method"] (신 스키마) — dom/api/embedded_json
    2. entry["crawler"]          (레거시 필드) — camhr_crawler / gnuboard_crawler 이름 매핑

Phase 1:
    - DOM 신 경로 (dom_crawler) 구현
    - API/EmbeddedJSON 신 경로는 NotImplementedError 로 남겨둠
    - 레거시 경로 (camhr / gnuboard) 유지 — 마이그레이션 전까지 죽지 않게

Phase 3 목표: 레거시 경로 전부 제거. 모든 엔트리가 extraction_method 로 라우팅.
"""


def dispatch(entry: dict, db_path: str, http_config: dict):
    """
    단일 엔트리를 크롤러로 라우팅.

    Returns: 크롤러가 반환한 값 (보통 {"new": N, "updated": M})
    Raises:  크롤링 중 예외는 그대로 전파 (main.py 가 catch)
    """
    site_id = entry["site_id"]
    config = entry.get("config") or entry  # 신 스키마는 엔트리 자체가 config

    # --- 신 경로: extraction_method 기준 ---
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
        raise NotImplementedError(
            f"[{site_id}] extraction_method='api' 는 Phase 3 에서 구현 예정"
        )

    if method == "embedded_json":
        raise NotImplementedError(
            f"[{site_id}] extraction_method='embedded_json' 는 Phase 2 에서 구현 예정"
        )

    # --- 레거시 경로: entry["crawler"] 문자열 매핑 ---
    legacy_name = entry.get("crawler")

    if legacy_name == "camhr_crawler":
        import camhr_crawler
        legacy_config = entry.get("config") or {}
        return camhr_crawler.crawl(
            db_path=db_path,
            max_pages=legacy_config.get("max_pages"),
        )

    if legacy_name == "gnuboard_crawler":
        # 구 스키마의 gnuboard_crawler 호출 — 아직 마이그레이션 안 된 엔트리용.
        # Phase 1 에서 sites.json 은 전부 migrate 되지만, 구버전 sites.json 백업본을
        # 돌릴 때를 대비해 유지.
        import gnuboard_crawler
        legacy_config = entry.get("config") or {}
        return gnuboard_crawler.crawl(
            site_id=site_id,
            config=legacy_config,
            db_path=db_path,
            max_pages=legacy_config.get("max_pages"),
            http_config=http_config,
        )

    raise ValueError(
        f"[{site_id}] 어느 크롤러로도 라우팅 실패 — "
        f"extraction_method={method!r}, crawler={legacy_name!r}"
    )
