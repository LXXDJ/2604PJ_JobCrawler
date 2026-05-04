"""home_url → site_id 추출.

규칙:
  - ccTLD (co.kr, or.kr, ne.kr 등) 도메인: SLD (cctld 직전 라벨, 가장 오른쪽)
    가 보통 등록 브랜드 → SLD 채택.
    예: eps.hrdkorea.or.kr → 'hrdkorea' (subdomain eps 는 sub-system 이름)
  - 일반 TLD (.com/.net/.io 등): leftmost (가장 구별성 있는 subdomain) 채택.
    단 흔한 보일러 라벨 (www, m, mobile, recruit, job 등) 은 skip.
    예: siemreap.korean.net → 'siemreap'
        recruit.kakao.com   → 'kakao'

예:
  https://www.career.co.kr        → "career"
  https://job.career.co.kr/jobs/  → "career"
  https://eps.hrdkorea.or.kr      → "hrdkorea"
  https://recruit.kakao.com       → "kakao"
  https://siemreap.korean.net     → "siemreap"
  https://example.com             → "example"
"""
from __future__ import annotations

from urllib.parse import urlparse


_CCTLD_2LEVEL_SUFFIXES = {
    "co.kr", "or.kr", "ne.kr", "go.kr", "re.kr", "pe.kr", "ac.kr", "hs.kr",
    "ms.kr", "es.kr", "sc.kr", "kg.kr",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "co.uk", "ac.uk", "gov.uk", "org.uk",
    "com.cn", "net.cn", "org.cn", "gov.cn",
}

_SKIP_LABELS = {
    "www", "m", "mobile", "ww", "www2",
    "recruit", "recruits", "recruiting", "recruitment",
    "job", "jobs", "career", "careers", "hr", "hiring",
    "apply", "people",
}


def extract_site_id(url: str) -> str:
    p = urlparse(url)
    netloc = p.netloc.lower().strip()
    if not netloc:
        return ""

    host = netloc.split(":")[0]

    # 네이버 카페: 카페별로 다른 site, 슬러그를 site_id 로
    if host in ("cafe.naver.com", "m.cafe.naver.com"):
        segs = [s for s in p.path.split("/") if s]
        if segs and segs[0] != "f-e":
            return segs[0]

    parts = host.split(".")
    if not parts:
        return ""

    # ccTLD 2-level 인지 판정
    is_cctld = len(parts) >= 3 and ".".join(parts[-2:]) in _CCTLD_2LEVEL_SUFFIXES
    if is_cctld:
        labels = parts[:-2]   # 예: eps.hrdkorea.or.kr → ['eps','hrdkorea']
    elif len(parts) >= 2:
        labels = parts[:-1]   # 예: siemreap.korean.net → ['siemreap','korean']
    else:
        labels = parts

    # ccTLD: SLD (rightmost) 가 등록 브랜드. 'www' 만 떼고 가장 오른쪽 채택.
    if is_cctld:
        cleaned = [x for x in labels if x not in _SKIP_LABELS] or labels
        # go.kr (정부): 한 부처(SLD)가 다수 독립 사이트 운영 → leftmost 채택
        # 예: whic.mofa.go.kr → whic, unrecruit.mofa.go.kr → unrecruit
        # 단 SLD 자체뿐 (mofa.go.kr) 이면 SLD 사용
        if ".".join(parts[-2:]) == "go.kr" and len(cleaned) >= 2:
            return cleaned[0]
        return cleaned[-1] if cleaned else (parts[-3] if len(parts) >= 3 else parts[0])

    # 일반 TLD: 보일러 라벨 skip 후 leftmost.
    # 전부 skip 되면 SLD (orig rightmost) 로 fallback.
    orig = list(labels)
    while labels and labels[0] in _SKIP_LABELS:
        labels = labels[1:]
    if not labels:
        return orig[-1] if orig else (parts[-1] if parts else "")
    return labels[0]
