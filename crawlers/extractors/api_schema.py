"""Captured API 응답 JSON 에서 list 경로/id/title 필드를 자동 학습.

원리:
  1. JSON 안에서 "dict 의 list" 가장 큰 array 찾기 (>= 2 dict, 각 dict 에 id-like + title-like)
  2. id 후보 필드 (id, jobId, seq, no, key, postId, ...) 중 row 들에서 unique 한 값
  3. title 후보 필드 (title, jobTitle, name, subject, ...) — 길이 ≥ 5 string
  4. detail URL pattern 추론: 페이지의 dynamic detail URL 이 주어졌다면 거기서
     id 가 등장하는 위치 → '{prefix}{id}{suffix}' 형태 학습
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional


_ID_FIELDS = ["id", "jobId", "job_id", "postId", "post_id", "seq", "no",
              "num", "key", "code", "uid", "pk", "wr_id", "articleId"]
_TITLE_FIELDS = ["title", "jobTitle", "job_title", "name", "subject",
                 "wr_subject", "headline", "position", "role"]


@dataclass
class ApiSchema:
    api_url_pattern: str   # 예: https://api.../jobs?page={page}&size={size}
    base_url: str          # 예: https://api.../jobs
    list_path: str         # 예: data.result
    id_field: str
    title_field: str
    page_param: str = "page"
    size_param: Optional[str] = "size"
    page_size: int = 50
    detail_url_template: Optional[str] = None  # 예: https://www.camhr.com/a/job/{id}
    # detail JSON API (있으면 SPA 사이트도 본문 수집 가능)
    detail_api_url_template: Optional[str] = None  # 예: https://api.camhr.com/v1.0.0/jobs/{id}
    detail_path: Optional[str] = None  # 예: 'data' — JSON 안 detail object 위치
    detail_content_field: Optional[str] = None  # body text 필드 (e.g. 'description')
    detail_html_field: Optional[str] = None  # body HTML 필드 (e.g. 'contentHtml')
    detail_title_field: Optional[str] = None  # detail 의 진짜 title (없으면 list title 사용)

    def to_dict(self) -> dict:
        return {
            "api_url_pattern": self.api_url_pattern,
            "base_url": self.base_url,
            "list_path": self.list_path,
            "id_field": self.id_field,
            "title_field": self.title_field,
            "page_param": self.page_param,
            "size_param": self.size_param,
            "page_size": self.page_size,
            "detail_url_template": self.detail_url_template,
            "detail_api_url_template": self.detail_api_url_template,
            "detail_path": self.detail_path,
            "detail_content_field": self.detail_content_field,
            "detail_html_field": self.detail_html_field,
            "detail_title_field": self.detail_title_field,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ApiSchema":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _walk(node: Any, path: str = ""):
    """JSON 트리 순회 → (path, value) 산출."""
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            # 리스트 내부는 경로에 [i] 표기 안 하고 첫 dict 만 학습 시 사용
            yield from _walk(v, f"{path}[{i}]")


def _find_largest_list_of_dicts(data: Any) -> Optional[tuple[str, list[dict]]]:
    """JSON 안에서 dict 만 들어있는 리스트 중 가장 큰 것 + 그 path."""
    best_path = None
    best_list: list[dict] = []
    if isinstance(data, dict):
        # 1depth + 2depth 우선 검색
        for k, v in data.items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                if len(v) > len(best_list):
                    best_path, best_list = k, v
            elif isinstance(v, dict):
                for k2, v2 in v.items():
                    if isinstance(v2, list) and v2 and all(isinstance(x, dict) for x in v2):
                        if len(v2) > len(best_list):
                            best_path, best_list = f"{k}.{k2}", v2
        if best_list:
            return best_path or "", best_list
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return "", data
    return None


def _pick_field(rows: list[dict], candidates: list[str]) -> Optional[str]:
    """row dict 들에서 후보 키 중 가장 적합한 것 선택."""
    if not rows:
        return None
    keys = set(rows[0].keys())
    for cand in candidates:
        if cand in keys:
            # 값이 거의 다 truthy 한지
            non_null = sum(1 for r in rows if r.get(cand) not in (None, "", 0))
            if non_null >= len(rows) // 2:
                return cand
    return None


def _learn_detail_template(known_url: Optional[str], any_id: Any) -> Optional[str]:
    if not known_url or any_id is None:
        return None
    s = str(any_id)
    if not s:
        return None
    if s in known_url:
        return known_url.replace(s, "{id}")
    return None


def _api_url_template(api_url: str, page_param: str = "page", size_param: Optional[str] = "size") -> tuple[str, str]:
    """captured api_url 에서 page/size 파라미터를 placeholder 로 치환.

    Returns: (api_url_pattern, base_url)
    """
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
    p = urlparse(api_url)
    base = urlunparse(p._replace(query=""))
    qs = dict(parse_qsl(p.query, keep_blank_values=True))
    qs[page_param] = "{page}"
    if size_param and size_param in qs:
        qs[size_param] = "{size}"
    pattern = urlunparse(p._replace(query=urlencode(qs, safe="{}")))
    return pattern, base


def detect_schema(
    api_calls: Iterable[dict],
    *,
    sample_detail_url: Optional[str] = None,
    desired_page_size: int = 50,
) -> Optional[ApiSchema]:
    """capture_api 결과로부터 가장 그럴듯한 list API 의 schema 학습.

    sample_detail_url: dynamic 으로 잡힌 row 의 detail_url 1개. id 위치 학습용.
    """
    best: Optional[tuple[int, dict, str, list[dict]]] = None  # (size, call, list_path, rows)
    for call in api_calls:
        data = call.get("data")
        found = _find_largest_list_of_dicts(data)
        if not found:
            continue
        list_path, rows = found
        if len(rows) < 2:
            continue
        if best is None or len(rows) > best[0]:
            best = (len(rows), call, list_path, rows)
    if not best:
        return None

    _, call, list_path, rows = best
    id_field = _pick_field(rows, _ID_FIELDS)
    title_field = _pick_field(rows, _TITLE_FIELDS)
    if not id_field or not title_field:
        return None

    pattern, base = _api_url_template(call["url"])
    detail_template = _learn_detail_template(sample_detail_url, rows[0].get(id_field))

    return ApiSchema(
        api_url_pattern=pattern,
        base_url=base,
        list_path=list_path,
        id_field=id_field,
        title_field=title_field,
        page_param="page",
        size_param="size" if "size=" in call["url"] else None,
        page_size=desired_page_size,
        detail_url_template=detail_template,
    )


def get_at_path(data: Any, path: str) -> Any:
    """list_path 따라 JSON 내부 list 추출."""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur
