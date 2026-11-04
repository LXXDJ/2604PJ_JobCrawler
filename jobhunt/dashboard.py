"""로컬 대시보드 — DB 를 읽어 단일 HTML 생성.

개인 적합도·갭 데이터라 외부 게시 없이 로컬에서만 본다.
- server.py 가 요청마다 render() 로 DB 최신 상태를 그려서 응답 (127.0.0.1 전용)
- build() 는 파일로 떨궈두는 용도 (data/jobhunt_dashboard.html)
집계는 브라우저에서 하므로 필터(적합도·직무·기술)에 따라 모든 차트가 같이 바뀐다.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .career import company_scores
from .db import DB_PATH, get_conn
from .report import ROLE_LABEL, _label_key

OUT_PATH = DB_PATH.parent / "jobhunt_dashboard.html"

# extract.SYSTEM 의 "기술 외 갭" 고정 라벨 — 차트에서 기술 갭과 구분해 회색으로 표시
NON_TECH_GAPS = ["경력 연차", "석사 이상 학위", "논문 실적", "도메인 경험", "영어", "리더십 경험"]
SOURCE_LABEL = {"wanted": "원티드", "jobkorea": "잡코리아", "saramin": "사람인"}


def _load() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.id, p.source, p.url, p.title, p.company, p.location, p.due_date,
                      p.first_seen_at, i.role_type, i.summary, i.domain, i.english_required,
                      i.salary_text, i.fit_score, i.fit_reason, i.required_skills,
                      i.preferred_skills, i.gaps, i.years_min, i.years_max,
                      k.depth, k.breadth, k.ai_core, k.benefits, k.growth_note, k.reason AS career_reason,
                      c.tags AS company_tags, c.founded_year, c.open_positions, c.employees,
                      c.revenue_eok, c.op_income_eok, c.revenue_growth, c.company_type, c.fin_source
               FROM postings p JOIN insights i ON i.posting_id = p.id
               LEFT JOIN career k ON k.posting_id = p.id
               LEFT JOIN companies c ON c.source = p.source AND c.external_id = p.company_id
               WHERE p.status = 'open'"""
        ).fetchall()
        counts = {
            "open": conn.execute("SELECT COUNT(*) FROM postings WHERE status='open'").fetchone()[0],
            "matched": conn.execute(
                "SELECT COUNT(*) FROM postings WHERE matched=1 AND status='open' AND dup_of IS NULL").fetchone()[0],
            "bySource": {r[0]: r[1] for r in conn.execute(
                "SELECT source, COUNT(*) FROM postings WHERE status='open' GROUP BY source")},
            "dups": conn.execute(
                "SELECT COUNT(*) FROM postings WHERE status='open' AND dup_of IS NOT NULL").fetchone()[0],
            # 본문이 이미지뿐이라 LLM 분석에서 빠진 공고
            "imageOnly": conn.execute(
                """SELECT COUNT(*) FROM postings WHERE matched=1 AND status='open' AND dup_of IS NULL
                     AND detail_fetched_at IS NOT NULL AND main_tasks IS NULL AND requirements IS NULL
                     AND length(coalesce(body,'')) < 150""").fetchone()[0],
        }
        also: dict[int, list] = {}   # 대표 공고 id → 다른 사이트의 같은 공고 [(source, url)]
        for r in conn.execute("SELECT dup_of, source, url FROM postings WHERE status='open' AND dup_of IS NOT NULL"):
            also.setdefault(r["dup_of"], []).append([r["source"], r["url"]])

    # 표기 차이("Hugging Face"/"HuggingFace") 를 전체 데이터 기준 최빈 표기로 통일
    fields = ("required_skills", "preferred_skills", "gaps")
    forms: dict[str, Counter] = {}
    for r in rows:
        for f in fields:
            for s in json.loads(r[f] or "[]"):
                if s.strip():
                    forms.setdefault(_label_key(s), Counter())[s.strip()] += 1
    display = {k: c.most_common(1)[0][0] for k, c in forms.items()}

    def norm(raw: str | None) -> list[str]:
        out: list[str] = []
        for s in json.loads(raw or "[]"):
            if s.strip():
                label = display[_label_key(s)]
                if label not in out:
                    out.append(label)
        return out

    def company(r) -> dict | None:
        if r["company_tags"] is None:
            return None
        tags = json.loads(r["company_tags"])
        fin = {k: r[k] for k in ("employees", "revenue_eok", "op_income_eok", "revenue_growth", "company_type")}
        sc = company_scores(tags, r["founded_year"], fin)
        return {**sc, "tags": tags, "founded": r["founded_year"], "open": r["open_positions"],
                "finsrc": (r["fin_source"] or "").split(":")[0] or None}

    def career(r) -> dict | None:
        if r["depth"] is None:
            return None
        return {"depth": r["depth"], "breadth": r["breadth"], "ai": r["ai_core"],
                "ben": r["benefits"], "note": r["growth_note"], "why": r["career_reason"]}

    postings = [{
        "id": r["id"], "src": r["source"], "u": r["url"], "t": r["title"], "c": r["company"],
        "loc": r["location"], "due": r["due_date"], "seen": (r["first_seen_at"] or "")[:10],
        "r": r["role_type"] or "other", "sum": r["summary"], "d": r["domain"],
        "eng": bool(r["english_required"]), "sal": r["salary_text"], "s": r["fit_score"] or 0,
        "why": r["fit_reason"], "req": norm(r["required_skills"]),
        "pref": norm(r["preferred_skills"]), "gaps": norm(r["gaps"]),
        "ymin": r["years_min"], "ymax": r["years_max"],
        "co": company(r), "car": career(r), "also": also.get(r["id"], []),
    } for r in rows]

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "version": version(),
        "counts": counts,
        "roles": ROLE_LABEL,
        "sources": SOURCE_LABEL,
        "nonTech": NON_TECH_GAPS,
        "postings": postings,
    }


def version() -> str:
    """데이터가 바뀌었는지 판단용 지문 — 열어둔 페이지가 주기적으로 비교해 새로고침 안내."""
    with get_conn() as conn:
        r = conn.execute(
            """SELECT (SELECT MAX(last_seen_at) FROM postings),
                      (SELECT COUNT(*) FROM postings WHERE status='open'),
                      (SELECT MAX(extracted_at) FROM insights),
                      (SELECT COUNT(*) FROM insights),
                      (SELECT COUNT(*) FROM career),
                      (SELECT MAX(fetched_at) FROM companies)"""
        ).fetchone()
    return "|".join(str(x) for x in r)


def render() -> str:
    data = json.dumps(_load(), ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("__DATA__", data)


def build(out_path: Path = OUT_PATH) -> Path:
    out_path.write_text(render(), encoding="utf-8")
    return out_path


_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>jobhunt 대시보드</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-1-soft: #cde2fb; --neutral-bar: #c3c2b7; --wash: rgba(42,120,214,0.07);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-1-soft: #184f95; --neutral-bar: #5a5955; --wash: rgba(57,135,229,0.12);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-1-soft: #184f95; --neutral-bar: #5a5955; --wash: rgba(57,135,229,0.12);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", "Malgun Gothic", sans-serif; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 24px 20px 64px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin-bottom: 20px; }
h1 { font-size: 20px; margin: 0; }
.sub { color: var(--muted); font-size: 13px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 12px 14px; }
.tile .k { color: var(--ink-2); font-size: 12px; }
.tile .v { font-size: 26px; font-weight: 600; margin-top: 2px; }
.tile .n { color: var(--muted); font-size: 12px; }
.filters { position: sticky; top: 0; z-index: 5; background: var(--page); padding: 10px 0;
  display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center; border-bottom: 1px solid var(--grid); margin-bottom: 16px; }
.filters label { color: var(--ink-2); font-size: 13px; display: inline-flex; align-items: center; gap: 6px; }
.filters input[type=range] { width: 140px; }
.filters input[type=search] { padding: 5px 8px; border: 1px solid var(--axis); border-radius: 6px;
  background: var(--surface); color: var(--ink); width: 200px; font: inherit; }
.chips { display: inline-flex; flex-wrap: wrap; gap: 6px; }
.chip { border: 1px solid var(--axis); background: var(--surface); color: var(--ink-2); border-radius: 999px;
  padding: 3px 10px; font: inherit; font-size: 12px; cursor: pointer; }
.chip[aria-pressed="true"] { background: var(--series-1); border-color: var(--series-1); color: #fff; }
.chip.skill { background: var(--wash); border-color: var(--series-1); color: var(--ink); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 16px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 14px 16px; min-width: 0; }
.card h2 { font-size: 14px; margin: 0 0 2px; }
.card .desc { color: var(--muted); font-size: 12px; margin: 0 0 10px; }
.wide { grid-column: 1 / -1; }
.bars { display: grid; grid-template-columns: minmax(90px, 160px) 1fr auto; gap: 4px 10px; align-items: center; }
.bars .lb { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.bars .lb:hover { text-decoration: underline; }
.bars .tr { height: 14px; position: relative; }
.bars .fill { position: absolute; left: 0; top: 2px; height: 10px; border-radius: 0 4px 4px 0; background: var(--series-1); min-width: 2px; }
.bars .fill.nt { background: var(--neutral-bar); }
.bars .val { font-size: 12px; color: var(--ink-2); font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
.bars .val b { color: var(--ink); font-weight: 600; }
.bars .row:hover { background: var(--wash); }
.empty { color: var(--muted); font-size: 13px; }
.hist { display: flex; align-items: flex-end; gap: 2px; height: 140px; border-bottom: 1px solid var(--axis); }
.hist .col { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; height: 100%; position: relative; }
.hist .bar { background: var(--series-1); border-radius: 4px 4px 0 0; min-height: 1px; }
.hist .bar.off { background: var(--neutral-bar); }
.hist .cnt { font-size: 11px; color: var(--ink-2); text-align: center; font-variant-numeric: tabular-nums; }
.hist-x { display: flex; gap: 2px; }
.hist-x span { flex: 1; text-align: center; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.legend { display: flex; gap: 14px; font-size: 12px; color: var(--ink-2); margin-top: 8px; flex-wrap: wrap; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.tablewrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 760px; }
th { text-align: left; color: var(--muted); font-weight: 500; font-size: 12px; padding: 6px 8px; border-bottom: 1px solid var(--axis); cursor: pointer; white-space: nowrap; }
td { padding: 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
tr.p { cursor: pointer; }
tr.p:hover td { background: var(--wash); }
td.sc { font-size: 15px; font-variant-numeric: tabular-nums; width: 48px; }
td.sc.hi { font-weight: 700; }
.t a { color: var(--ink); font-weight: 600; text-decoration: none; }
.t a:hover { text-decoration: underline; }
.t .co { color: var(--ink-2); font-size: 12px; }
.tag { display: inline-block; font-size: 11px; padding: 0 6px; border-radius: 4px; border: 1px solid var(--axis); color: var(--ink-2); margin: 1px 2px 1px 0; white-space: nowrap; }
.tag.g { border-color: var(--series-1); }
.tag.nt { border-style: solid; color: var(--muted); }
tr.det td { background: var(--page); color: var(--ink-2); font-size: 13px; }
tr.det dl { display: grid; grid-template-columns: 80px 1fr; gap: 4px 12px; margin: 0; }
tr.det dt { color: var(--muted); }
tr.det dd { margin: 0; }
.pager { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.pager .btn { min-width: 34px; padding: 4px 9px; font-variant-numeric: tabular-nums; }
.pager .btn.cur { background: var(--series-1); border-color: var(--series-1); color: #fff; font-weight: 600; }
.pager .btn:disabled { opacity: .4; cursor: default; }
.pager .gap { color: var(--muted); padding: 0 2px; }
.pager .info { color: var(--muted); font-size: 12px; margin-left: 8px; }
button.btn { border: 1px solid var(--axis); background: var(--surface); color: var(--ink); border-radius: 6px; padding: 5px 12px; font: inherit; cursor: pointer; }
.seg { display: inline-flex; border: 1px solid var(--axis); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; background: var(--surface); color: var(--ink-2); padding: 5px 12px; font: inherit; font-size: 13px; cursor: pointer; }
.seg button + button { border-left: 1px solid var(--axis); }
.seg button[aria-pressed="true"] { background: var(--series-1); color: #fff; font-weight: 600; }
.weights { margin-bottom: 16px; }
.weights .wg { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 6px 20px; margin-top: 8px; }
.weights label { display: grid; grid-template-columns: 1fr 90px 26px; align-items: center; gap: 8px; font-size: 13px; color: var(--ink-2); }
.weights label b { color: var(--ink); text-align: right; font-variant-numeric: tabular-nums; }
.weights summary { cursor: pointer; font-weight: 600; font-size: 14px; }
.weights .desc { margin: 6px 0 0; }
.meter { display: inline-flex; gap: 2px; vertical-align: -1px; margin-left: 4px; }
.meter i { width: 8px; height: 8px; border-radius: 2px; background: var(--grid); }
.meter i.on { background: var(--series-1); }
td.sc.dim { color: var(--muted); font-size: 13px; }
.banner { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; background: var(--wash); border: 1px solid var(--series-1);
  border-radius: 8px; padding: 8px 12px; margin-bottom: 12px; font-size: 13px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>jobhunt 대시보드</h1>
    <span class="sub" id="gen"></span>
  </header>

  <div class="banner" id="newdata" hidden>새 데이터가 들어왔어요 (매일 배치 반영). <button class="btn" onclick="location.reload()">새로고침</button></div>

  <section class="tiles" id="tiles"></section>

  <div class="filters">
    <span class="seg" id="mode" role="group" aria-label="보기 기준">
      <button data-m="fit" title="지금 내 이력으로 붙을 가능성 + 내 목표와 맞는 정도">나에게 맞는 순</button>
      <button data-m="rec" title="회사·포지션이 내 커리어에 얼마나 도움이 되는가 (내 현재 이력과 무관)">추천순</button>
    </span>
    <label><span id="fminlabel">적합도</span> <input type="range" id="fmin" min="0" max="90" step="5"> <b id="fminv"></b>점 이상</label>
    <span class="chips" id="roles" aria-label="직무 유형"></span>
    <span class="chips" id="srcs" aria-label="사이트"></span>
    <label><input type="checkbox" id="noeng"> 영어 필수 제외</label>
    <input type="search" id="q" placeholder="회사·제목·기술 검색">
    <span id="skillchip"></span>
  </div>

  <details class="card weights" id="wpanel" hidden>
    <summary>추천도 가중치 — 무엇을 중시할지 조절 (<span id="wsum"></span>)</summary>
    <p class="desc">추천도 = 아래 항목 점수(0~100)의 가중 평균. 정보가 없는 항목은 빼고 계산해요.
      포지션 항목은 LLM이 공고 본문을 읽고 매긴 값, 회사 항목은 원티드 회사 태그(연봉 백분위·투자·퇴사율·복지)와
      사람인·잡코리아 기업정보(사원수·매출액·영업이익·매출 증가율·기업형태)로 계산한 값이에요. 원티드 회사의 재무는 사람인에서 이름+설립연도로 찾아 채워요.</p>
    <div class="wg" id="wgrid"></div>
    <p class="desc"><button class="btn" id="wreset">기본값으로</button></p>
  </details>

  <div class="grid">
    <section class="card wide">
      <h2>내 부족한 점 TOP 20 <span class="sub" id="gapscope"></span></h2>
      <p class="desc">필터된 공고 중 이 항목이 부족하다고 나온 비율. <b>단독</b> = 이것만 채우면 부족한 점이 없어지는 공고 수. 항목을 누르면 아래 목록이 그 항목으로 걸러져요.</p>
      <div class="bars" id="gaps"></div>
      <div class="legend"><span><i style="background:var(--series-1)"></i>기술 (준비로 채울 수 있음)</span><span><i style="background:var(--neutral-bar)"></i>기술 외 (경력·학위·도메인 등)</span></div>
    </section>
    <section class="card">
      <h2>필수 기술 TOP 15</h2>
      <p class="desc">필터된 공고 중 자격요건에 나온 비율</p>
      <div class="bars" id="req"></div>
    </section>
    <section class="card">
      <h2>우대 기술 TOP 15</h2>
      <p class="desc">필터된 공고 중 우대사항에 나온 비율</p>
      <div class="bars" id="pref"></div>
    </section>
    <section class="card">
      <h2 id="histtitle">적합도 분포</h2>
      <p class="desc">직무·검색 필터 기준. 회색 = 점수 하한 미만이라 집계에서 빠진 구간</p>
      <div class="hist" id="hist"></div>
      <div class="hist-x" id="histx"></div>
    </section>
    <section class="card">
      <h2>직무 유형</h2>
      <p class="desc">적합도·검색 필터 기준. 회색 = 직무 필터에서 꺼진 유형</p>
      <div class="bars" id="rolebars"></div>
    </section>
    <section class="card">
      <h2>도메인 TOP 10</h2>
      <p class="desc">필터된 공고의 산업 분야</p>
      <div class="bars" id="domains"></div>
    </section>
    <section class="card">
      <h2>회사 TOP 10</h2>
      <p class="desc">필터된 공고가 많은 회사</p>
      <div class="bars" id="companies"></div>
    </section>

    <section class="card wide">
      <h2 id="listtitle">공고 목록</h2>
      <p class="desc">행을 누르면 요약·적합도 이유·기술이 펼쳐져요. 제목은 해당 사이트 공고 링크예요. 같은 공고가 여러 사이트에 있으면 하나로 묶어 보여줘요.</p>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th data-sort="s" title="지금 내 이력과 목표 기준">적합도</th><th data-sort="rec" title="커리어 가치 기준 (가중치 조절 가능)">추천도</th><th data-sort="t">공고 · 회사</th><th data-sort="r">직무</th>
            <th data-sort="d">도메인</th><th data-sort="due">마감</th><th>부족한 점</th>
          </tr></thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
      <nav class="pager" id="pager" aria-label="페이지"></nav>
    </section>
  </div>
</div>

<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const P = D.postings;
const NT = new Set(D.nonTech);
const ROLE_ORDER = ['llm_app', 'ai_fullstack', 'ml_engineer', 'backend', 'data', 'research', 'other'];
const DEFAULT_ROLES = ['llm_app', 'ai_fullstack', 'ml_engineer', 'backend', 'data'];
const PAGE = 100;
const TODAY = new Date().toLocaleDateString('sv');   // YYYY-MM-DD (로컬 시간)

// 추천도 구성 요소 — 각 0~100 (정보 없으면 null). 포지션 4개는 LLM 1~5점, 회사 3개는 원티드 태그 기반
const L5 = (x) => x == null ? null : (x - 1) * 25;
const COMPONENTS = [
  { k: 'depth',  name: '전문성 성장',        grp: '포지션', v: (p) => L5(p.car?.depth) },
  { k: 'breadth', name: '업무 범위·리드 경로', grp: '포지션', v: (p) => L5(p.car?.breadth) },
  { k: 'ai',     name: 'AI 핵심도',          grp: '포지션', v: (p) => L5(p.car?.ai) },
  { k: 'pay',    name: '연봉 수준',          grp: '회사',   v: (p) => p.co?.pay ?? null },
  { k: 'welfare', name: '보상·복지',         grp: '회사',   v: (p) => {
      const xs = [L5(p.car?.ben), p.co ? Math.min(100, 15 + p.co.welfare.length * 7) : null].filter((x) => x != null);
      return xs.length ? xs.reduce((a, b) => a + b) / xs.length : null; } },
  { k: 'size',   name: '회사 규모(사원수)',   grp: '회사',   v: (p) => p.co?.size ?? null },
  { k: 'rev',    name: '매출 규모',          grp: '회사',   v: (p) => p.co?.revenue ?? null },
  { k: 'stab',   name: '안정성·성장',        grp: '회사',   v: (p) => p.co?.stability ?? null },
];
const DEFAULT_W = { depth: 25, breadth: 15, ai: 15, pay: 15, welfare: 10, size: 10, rev: 10, stab: 10 };

const st = {
  mode: 'fit', min: 50, roles: new Set(DEFAULT_ROLES), srcs: new Set(Object.keys(D.sources)), noeng: false, q: '', skill: null,
  sort: 's', dir: -1, page: 0, open: new Set(), w: { ...DEFAULT_W },
};
try {
  const saved = JSON.parse(localStorage.getItem('jobhunt.filters') || 'null');
  if (saved) {
    st.min = saved.min ?? st.min; st.roles = new Set(saved.roles || DEFAULT_ROLES); st.noeng = !!saved.noeng;
    st.mode = saved.mode === 'rec' ? 'rec' : 'fit'; st.w = { ...DEFAULT_W, ...(saved.w || {}) };
    if (saved.srcs) st.srcs = new Set(saved.srcs);
    st.sort = st.mode === 'rec' ? 'rec' : 's';
  }
} catch (e) {}

// 커리어 평가(LLM)가 없는 공고는 추천도 없음 — 회사 태그만으로 매기면 포지션 가치가 빠져 왜곡됨
function computeRec() {
  for (const p of P) {
    if (!p.car) { p.rec = null; continue; }
    let sum = 0, wsum = 0;
    for (const c of COMPONENTS) {
      const v = c.v(p), w = st.w[c.k] || 0;
      if (v != null && w > 0) { sum += v * w; wsum += w; }
    }
    p.rec = wsum ? Math.round(sum / wsum) : null;
  }
}
const val = (p) => st.mode === 'rec' ? p.rec : p.s;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const pct = (n, d) => d ? Math.round(n / d * 100) + '%' : '-';
const roleName = (r) => D.roles[r] || r;

function matchText(p) {
  if (!st.q) return true;
  const q = st.q.toLowerCase();
  return [p.t, p.c, p.d, ...p.req, ...p.pref, ...p.gaps].some((x) => (x || '').toLowerCase().includes(q));
}
function matchSkill(p) {
  return !st.skill || p.req.includes(st.skill) || p.pref.includes(st.skill) || p.gaps.includes(st.skill);
}
// 차트마다 자기 차원의 필터만 빼고 나머지를 적용 (분포를 보여주기 위해)
function filtered({ ignoreMin = false, ignoreRole = false, ignoreSkill = false } = {}) {
  return P.filter((p) =>
    val(p) != null &&
    (ignoreMin || val(p) >= st.min) &&
    (ignoreRole || st.roles.has(p.r)) &&
    st.srcs.has(p.src) &&
    (!st.noeng || !p.eng) &&
    matchText(p) &&
    (ignoreSkill || matchSkill(p)));
}

function count(list, key) {
  const c = new Map();
  for (const p of list) for (const k of (typeof key === 'function' ? key(p) : p[key])) c.set(k, (c.get(k) || 0) + 1);
  return [...c.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function bars(el, entries, total, { top = 15, cls = () => '', extra = null, clickable = true, title = null } = {}) {
  const rows = entries.slice(0, top);
  if (!rows.length) { el.innerHTML = '<div class="empty">해당 공고 없음</div>'; return; }
  const max = rows[0][1];
  el.innerHTML = rows.map(([k, v]) => {
    const tip = title ? title(k, v) : `${k}: ${v}건 / ${total}건 (${pct(v, total)})`;
    return `<div class="lb" title="${esc(tip)}" data-k="${esc(k)}" ${clickable ? '' : 'style="cursor:default;text-decoration:none"'}>${esc(k)}</div>` +
      `<div class="tr" title="${esc(tip)}"><div class="fill ${cls(k)}" style="width:${(v / max * 100).toFixed(1)}%"></div></div>` +
      `<div class="val"><b>${pct(v, total)}</b> · ${v}건${extra ? extra(k) : ''}</div>`;
  }).join('');
  if (clickable) el.querySelectorAll('.lb').forEach((n) => n.addEventListener('click', () => {
    st.skill = n.dataset.k; st.page = 0; render();
    $('listtitle').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
}

function renderTiles(list) {
  const hi = P.filter((p) => p.s >= 75).length;
  const rated = P.filter((p) => p.car).length;
  const lastSeen = P.reduce((m, p) => p.seen > m ? p.seen : m, '');
  const recent = P.filter((p) => p.seen >= daysAgo(7)).length;
  const bySrc = Object.entries(D.sources).map(([k, n]) => `${n} ${(D.counts.bySource[k] || 0).toLocaleString()}`).join(' · ');
  $('tiles').innerHTML = [
    ['수집 중인 공고', D.counts.open.toLocaleString(), bySrc],
    ['관심 공고', D.counts.matched.toLocaleString(),
      `1차 필터 통과 · 중복 ${D.counts.dups}건 묶음 · 이미지 공고 ${D.counts.imageOnly}건 분석 제외`],
    ['LLM 분석 완료', P.length.toLocaleString(), `관심 공고의 ${pct(P.length, D.counts.matched)}`],
    ['커리어 평가 완료', rated.toLocaleString(), `추천도 계산 가능 (${pct(rated, P.length)})`],
    ['적합도 75+', hi.toLocaleString(), '바로 볼 만한 공고'],
    ['최근 7일 신규', recent.toLocaleString(), `가장 최근 신규 ${lastSeen || '-'}`],
    ['현재 필터', list.length.toLocaleString(), '아래 차트·목록 기준'],
  ].map(([k, v, n]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${esc(n)}</div></div>`).join('');
}
function daysAgo(n) {
  const d = new Date(); d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

function renderGaps(list) {
  const entries = count(list, 'gaps');
  // 단독 = 그 항목이 유일한 부족한 점인 공고 수
  const solo = new Map();
  for (const p of list) if (p.gaps.length === 1) solo.set(p.gaps[0], (solo.get(p.gaps[0]) || 0) + 1);
  $('gapscope').textContent = `— ${st.mode === 'rec' ? '추천도' : '적합도'} ${st.min}점 이상 공고 ${list.length}건 기준`;
  bars($('gaps'), entries, list.length, {
    top: 20, cls: (k) => NT.has(k) ? 'nt' : '',
    extra: (k) => solo.get(k) ? ` · 단독 ${solo.get(k)}` : '',
  });
}

function renderHist() {
  const list = filtered({ ignoreMin: true });
  const bins = Array.from({ length: 10 }, (_, i) => ({ lo: i * 10, n: 0 }));
  for (const p of list) bins[Math.min(9, Math.floor(val(p) / 10))].n++;
  $('histtitle').textContent = st.mode === 'rec' ? '추천도 분포' : '적합도 분포';
  const max = Math.max(1, ...bins.map((b) => b.n));
  $('hist').innerHTML = bins.map((b) => {
    const off = b.lo + 9 < st.min;
    const tip = `${b.lo}~${b.lo === 90 ? 100 : b.lo + 9}점: ${b.n}건`;
    return `<div class="col" title="${tip}"><div class="cnt">${b.n || ''}</div><div class="bar ${off ? 'off' : ''}" style="height:${b.n / max * 85}%"></div></div>`;
  }).join('');
  $('histx').innerHTML = bins.map((b) => `<span>${b.lo}</span>`).join('');
}

function renderRoles() {
  const list = filtered({ ignoreRole: true });
  const c = new Map(ROLE_ORDER.map((r) => [r, 0]));
  for (const p of list) c.set(p.r, (c.get(p.r) || 0) + 1);
  const entries = [...c.entries()].filter(([, v]) => v).sort((a, b) => b[1] - a[1]).map(([r, v]) => [roleName(r), v, r]);
  const off = new Set(entries.filter(([, , r]) => !st.roles.has(r)).map(([n]) => n));
  bars($('rolebars'), entries, list.length, { top: 10, cls: (k) => off.has(k) ? 'nt' : '', clickable: false });
}

function renderTable(list) {
  const key = st.sort, dir = st.dir;
  const sorted = [...list].sort((a, b) => {
    let x = a[key] ?? '', y = b[key] ?? '';
    if (key === 'rec') { x = a.rec ?? -1; y = b.rec ?? -1; }   // 평가 전은 맨 뒤
    if (key === 'r') { x = roleName(x); y = roleName(y); }
    if (x < y) return -dir; if (x > y) return dir;
    return (val(b) ?? -1) - (val(a) ?? -1) || (b.seen > a.seen ? 1 : -1);
  });
  const pages = Math.max(1, Math.ceil(list.length / PAGE));
  st.page = Math.min(st.page, pages - 1);
  const view = sorted.slice(st.page * PAGE, (st.page + 1) * PAGE);
  $('listtitle').textContent = `공고 목록 (${list.length}건${st.skill ? ` · "${st.skill}" 포함` : ''})`;
  $('rows').innerHTML = view.map((p) => {
    const gaps = p.gaps.map((g) => `<span class="tag ${NT.has(g) ? 'nt' : 'g'}">${esc(g)}</span>`).join('') || '<span class="tag">없음</span>';
    const main = `<tr class="p" data-id="${p.id}">
      <td class="sc ${p.s >= 75 ? 'hi' : ''} ${st.mode === 'rec' ? 'dim' : ''}">${p.s}</td>
      <td class="sc ${p.rec >= 70 ? 'hi' : ''} ${st.mode === 'fit' ? 'dim' : ''}" title="${p.rec == null ? '커리어 평가 전' : ''}">${p.rec ?? '-'}</td>
      <td class="t"><a href="${esc(p.u)}" target="_blank" rel="noopener">${esc(p.t)}</a>${p.eng ? ' <span class="tag">영어 필수</span>' : ''}<div class="co">${esc(p.c)}${p.loc ? ' · ' + esc(p.loc) : ''}
        · <span class="tag">${esc(D.sources[p.src] || p.src)}</span>${p.also.map(([s, u]) =>
          `<a class="tag" href="${esc(u)}" target="_blank" rel="noopener" title="같은 공고">${esc(D.sources[s] || s)}</a>`).join('')}</div></td>
      <td>${esc(roleName(p.r))}</td>
      <td>${esc(p.d || '-')}</td>
      <td>${p.due ? esc(p.due) + (p.due < TODAY ? ' <span class="tag nt">지남</span>' : '') : '상시'}</td>
      <td>${gaps}</td></tr>`;
    if (!st.open.has(p.id)) return main;
    const yrs = (p.ymin == null && p.ymax == null) ? '-' : `${p.ymin ?? '?'}~${p.ymax ?? '?'}년`;
    const tags = (xs) => xs.map((x) => `<span class="tag">${esc(x)}</span>`).join('') || '-';
    const meter = (n) => n == null ? '-' : `${n}/5<span class="meter">${[1, 2, 3, 4, 5].map((i) => `<i class="${i <= n ? 'on' : ''}"></i>`).join('')}</span>`;
    const car = p.car ? `
      <dt>추천 이유</dt><dd>${esc(p.car.why)}</dd>
      <dt>쌓이는 역량</dt><dd>${esc(p.car.note)}</dd>
      <dt>포지션 평가</dt><dd>전문성 ${meter(p.car.depth)} · 범위·리드 ${meter(p.car.breadth)} · AI 핵심 ${meter(p.car.ai)} · 보상·복지 ${meter(p.car.ben)}</dd>`
      : '<dt>추천도</dt><dd>커리어 평가 전 (다음 배치에서 평가)</dd>';
    const eok = (x) => x == null ? null : Math.abs(x) >= 10000 ? `${(x / 10000).toFixed(1)}조` : `${Math.round(x).toLocaleString()}억`;
    const fin = p.co ? [
      p.co.revenue_eok != null ? `매출 ${eok(p.co.revenue_eok)}` : null,
      p.co.op_income_eok != null ? `영업이익 ${eok(p.co.op_income_eok)}` : null,
      p.co.revenue_growth != null ? `매출 증가율 ${p.co.revenue_growth}%` : null,
    ].filter(Boolean) : [];
    const co = p.co ? `
      <dt>회사</dt><dd>${[p.co.company_type, p.co.size_label, p.co.pay_label, p.co.founded ? `${p.co.founded}년 설립` : null,
        p.co.open ? `진행 중 공고 ${p.co.open}건` : null].filter(Boolean).map(esc).join(' · ') || '-'}</dd>
      ${fin.length ? `<dt>재무</dt><dd>${fin.map(esc).join(' · ')}${p.co.finsrc ? ` <span class="tag nt">${esc(D.sources[p.co.finsrc] || p.co.finsrc)} 기업정보</span>` : ''}</dd>` : ''}
      ${p.co.tags.length ? `<dt>회사 태그</dt><dd>${tags(p.co.tags)}</dd>` : ''}` : '';
    return main + `<tr class="det"><td></td><td colspan="6"><dl>
      <dt>요약</dt><dd>${esc(p.sum)}</dd>
      <dt>적합도 이유</dt><dd>${esc(p.why)}</dd>${car}${co}
      <dt>필수 기술</dt><dd>${tags(p.req)}</dd>
      <dt>우대 기술</dt><dd>${tags(p.pref)}</dd>
      <dt>경력</dt><dd>${yrs}</dd>
      ${p.sal ? `<dt>연봉</dt><dd>${esc(p.sal)}</dd>` : ''}
      <dt>처음 수집</dt><dd>${esc(p.seen)}</dd>
    </dl></td></tr>`;
  }).join('') || '<tr><td colspan="7" class="empty">조건에 맞는 공고가 없어요</td></tr>';
  renderPager(pages, list.length);
}

// 페이지 번호: 처음·끝 + 현재 앞뒤 2개, 사이는 …
function renderPager(pages, total) {
  const el = $('pager');
  if (pages <= 1) { el.innerHTML = ''; return; }
  const cur = st.page;
  const nums = [...new Set([0, pages - 1, ...[-2, -1, 0, 1, 2].map((d) => cur + d)])]
    .filter((i) => i >= 0 && i < pages).sort((a, b) => a - b);
  let html = `<button class="btn" data-p="${cur - 1}" ${cur === 0 ? 'disabled' : ''}>‹ 이전</button>`;
  nums.forEach((i, idx) => {
    if (idx && i - nums[idx - 1] > 1) html += '<span class="gap">…</span>';
    html += `<button class="btn ${i === cur ? 'cur' : ''}" data-p="${i}" ${i === cur ? 'aria-current="page"' : ''}>${i + 1}</button>`;
  });
  html += `<button class="btn" data-p="${cur + 1}" ${cur === pages - 1 ? 'disabled' : ''}>다음 ›</button>`;
  html += `<span class="info">${cur * PAGE + 1}–${Math.min(total, (cur + 1) * PAGE)} / ${total}건</span>`;
  el.innerHTML = html;
}

function renderFilters() {
  $('mode').querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', b.dataset.m === st.mode));
  $('fminlabel').textContent = st.mode === 'rec' ? '추천도' : '적합도';
  $('wpanel').hidden = st.mode !== 'rec';
  $('wsum').textContent = COMPONENTS.filter((c) => st.w[c.k] > 0).map((c) => `${c.name} ${st.w[c.k]}`).join(' · ');
  $('fmin').value = st.min; $('fminv').textContent = st.min;
  $('noeng').checked = st.noeng;
  $('roles').innerHTML = ROLE_ORDER.map((r) =>
    `<button class="chip" data-r="${r}" aria-pressed="${st.roles.has(r)}">${esc(roleName(r))}</button>`).join('');
  $('roles').querySelectorAll('.chip').forEach((b) => b.addEventListener('click', () => {
    const r = b.dataset.r;
    st.roles.has(r) ? st.roles.delete(r) : st.roles.add(r);
    st.page = 0; render();
  }));
  $('srcs').innerHTML = Object.entries(D.sources).map(([k, n]) =>
    `<button class="chip" data-s="${k}" aria-pressed="${st.srcs.has(k)}">${esc(n)}</button>`).join('');
  $('srcs').querySelectorAll('.chip').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.s;
    st.srcs.has(k) ? st.srcs.delete(k) : st.srcs.add(k);
    st.page = 0; render();
  }));
  $('skillchip').innerHTML = st.skill
    ? `<button class="chip skill" id="clearskill" title="기술 필터 해제">${esc(st.skill)} ✕</button>` : '';
  if (st.skill) $('clearskill').addEventListener('click', () => { st.skill = null; st.page = 0; render(); });
  try {
    localStorage.setItem('jobhunt.filters', JSON.stringify(
      { min: st.min, roles: [...st.roles], srcs: [...st.srcs], noeng: st.noeng, mode: st.mode, w: st.w }));
  } catch (e) {}
}

function renderWeights() {
  $('wgrid').innerHTML = COMPONENTS.map((c) => `<label title="${c.grp} 항목">${c.grp} · ${esc(c.name)}
    <input type="range" min="0" max="40" step="5" value="${st.w[c.k]}" data-k="${c.k}"><b>${st.w[c.k]}</b></label>`).join('');
  $('wgrid').querySelectorAll('input').forEach((inp) => inp.addEventListener('input', (e) => {
    st.w[e.target.dataset.k] = +e.target.value;
    e.target.nextElementSibling.textContent = e.target.value;
    computeRec(); st.page = 0; render();
  }));
}

function render() {
  renderFilters();
  const list = filtered();
  const noSkill = filtered({ ignoreSkill: true });   // 기술 차트는 기술 필터 영향 없이
  renderTiles(list);
  renderGaps(noSkill);
  bars($('req'), count(noSkill, 'req'), noSkill.length);
  bars($('pref'), count(noSkill, 'pref'), noSkill.length);
  renderHist();
  renderRoles();
  bars($('domains'), count(list, (p) => p.d ? [p.d] : []), list.length, { top: 10, clickable: false });
  bars($('companies'), count(list, (p) => [p.c]), list.length, { top: 10, clickable: false });
  renderTable(list);
}

$('gen').textContent = `생성 ${D.generated} · 분석 ${P.length}건 · 데이터는 이 파일 안에만 있어요`;
$('fmin').addEventListener('input', (e) => { st.min = +e.target.value; st.page = 0; render(); });
$('noeng').addEventListener('change', (e) => { st.noeng = e.target.checked; st.page = 0; render(); });
$('q').addEventListener('input', (e) => { st.q = e.target.value.trim(); st.page = 0; render(); });
$('pager').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-p]'); if (!b || b.disabled) return;
  st.page = +b.dataset.p; renderTable(filtered());
  $('listtitle').scrollIntoView({ block: 'start' });
});
$('mode').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
  if (st.mode === b.dataset.m) return;
  st.mode = b.dataset.m; st.sort = st.mode === 'rec' ? 'rec' : 's'; st.dir = -1; st.page = 0;
  render();
}));
$('wreset').addEventListener('click', () => { st.w = { ...DEFAULT_W }; renderWeights(); computeRec(); render(); });
$('rows').addEventListener('click', (e) => {
  if (e.target.closest('a')) return;
  const tr = e.target.closest('tr.p'); if (!tr) return;
  const id = +tr.dataset.id;
  st.open.has(id) ? st.open.delete(id) : st.open.add(id);
  renderTable(filtered());
});
document.querySelectorAll('th[data-sort]').forEach((th) => th.addEventListener('click', () => {
  const k = th.dataset.sort;
  st.dir = st.sort === k ? -st.dir : (k === 's' || k === 'rec' ? -1 : 1);
  st.sort = k; renderTable(filtered());
}));
renderWeights();
computeRec();
render();

// 로컬 서버로 열었을 때만: 5분마다 데이터 지문을 비교해 바뀌었으면 새로고침 안내
if (location.protocol.startsWith('http')) {
  setInterval(async () => {
    try {
      const v = await (await fetch('/api/version', { cache: 'no-store' })).text();
      if (v && v !== D.version) $('newdata').hidden = false;
    } catch (e) {}
  }, 5 * 60 * 1000);
}
</script>
</body>
</html>
"""
