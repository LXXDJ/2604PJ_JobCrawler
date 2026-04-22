"""
크롤러 디스패처

sites.json / REGISTERED_CRAWLS 엔트리를 받아서 적절한 크롤러를 호출.

스키마:
  - 신(v2): entry.sources = [{menu_name, extraction_method, source, pagination}, ...]
           한 사이트의 여러 메뉴(전체/정직원/알바/지역별 등) 를 각각 수집.
  - 구(v1): entry.extraction_method + entry.source + entry.pagination (단일 메뉴)
           v1 엔트리는 _normalize_sources 가 sources=[{...}] 배열로 감싸서 v2 처럼 취급.

라우팅 기준: sub["extraction_method"] → dom / api / embedded_json (sub 마다 독립)

한 sub 가 실패해도 다음 sub 로 진행 — 한 메뉴가 깨져도 다른 메뉴는 계속 수집되도록.
"""


def _normalize_sources(entry: dict) -> list[dict]:
    """entry 에서 sources 배열 추출 (v1 legacy 는 single source 로 감쌈)."""
    sources = entry.get("sources")
    if isinstance(sources, list) and sources:
        return sources
    return [{
        "menu_name": entry.get("menu_name") or "main",
        "extraction_method": entry.get("extraction_method")
            or (entry.get("config") or {}).get("extraction_method"),
        "source": entry.get("source")
            or (entry.get("config") or {}).get("source") or {},
        "pagination": entry.get("pagination")
            or (entry.get("config") or {}).get("pagination") or {},
    }]


def _run_single_source(site_id: str, sub: dict, db_path: str, http_config: dict):
    """sources[] 의 한 항목(sub) 을 크롤러에 전달. 크롤러가 기대하는 구/신 공통 형태."""
    method = sub.get("extraction_method")
    config = {
        "site_id": site_id,
        "extraction_method": method,
        "source": sub.get("source") or {},
        "pagination": sub.get("pagination") or {},
    }
    pagination = config["pagination"]
    max_pages = pagination.get("max_pages")

    if method == "dom":
        import dom_crawler
        return dom_crawler.crawl(
            site_id=site_id, config=config, db_path=db_path,
            max_pages=max_pages, http_config=http_config,
        )
    if method == "api":
        import api_crawler
        return api_crawler.crawl(
            site_id=site_id, config=config, db_path=db_path,
            max_pages=max_pages, http_config=http_config,
        )
    if method == "embedded_json":
        import embedded_crawler
        return embedded_crawler.crawl(
            site_id=site_id, config=config, db_path=db_path,
            max_pages=max_pages, http_config=http_config,
        )
    raise ValueError(
        f"[{site_id}] 어느 크롤러로도 라우팅 실패 — extraction_method={method!r} "
        f"(신 스키마의 dom/api/embedded_json 중 하나여야 함)"
    )


def dispatch(entry: dict, db_path: str, http_config: dict):
    """
    단일 사이트 entry 를 크롤러로 라우팅. v2 스키마이면 sources 배열 전부 순회.

    Returns: {"new": 총 신규 건수, "updated": 총 재확인 건수}
    """
    site_id = entry["site_id"]
    sources = _normalize_sources(entry)

    total_new = 0
    total_updated = 0
    errors: list[tuple[str, str]] = []

    for idx, sub in enumerate(sources, 1):
        menu_label = sub.get("menu_name") or f"source#{idx}"
        if len(sources) > 1:
            print(f"\n  ──── [{site_id}] 메뉴 '{menu_label}' ({idx}/{len(sources)}) ────")

        try:
            result = _run_single_source(site_id, sub, db_path, http_config) or {}
            total_new += result.get("new", 0) or 0
            total_updated += result.get("updated", 0) or 0
        except Exception as e:
            # 한 메뉴 실패해도 다른 메뉴는 계속. 전체 실패 판정은 모두 실패했을 때만.
            msg = f"{type(e).__name__}: {e}"
            print(f"  [ERROR] 메뉴 '{menu_label}' 실패 — {msg}")
            errors.append((menu_label, msg))

    if len(sources) > 1:
        print(
            f"\n  ==== [{site_id}] sources={len(sources)} "
            f"— 성공 {len(sources) - len(errors)}/실패 {len(errors)} / "
            f"총 신규 {total_new} 재확인 {total_updated} ===="
        )

    # 모든 sub 가 실패했으면 첫 에러를 raise (기존 동작 유지)
    if errors and len(errors) == len(sources):
        raise RuntimeError(
            f"[{site_id}] 모든 source 실패 — 첫 에러: {errors[0][1]}"
        )

    return {"new": total_new, "updated": total_updated}
