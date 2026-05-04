"""운영/테스트 대시보드 (Streamlit).

실행:
  streamlit run dashboard/app.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import base64
import mimetypes

from crawlers.batch.runner import run_site
from crawlers.infra.db import get_conn, init_db
from crawlers.infra.media_store import absolute_path as _media_abs
from crawlers.infra.sites_repo import list_sites, update_status
from crawlers.registration.register import register


st.set_page_config(page_title="JobCrawler", layout="wide")
init_db()


# ---------- Helpers ----------

# SQLite datetime('now') 는 UTC. dashboard 표시는 KST (UTC+9).
_KST_COLS = ("started_at", "ended_at", "first_seen_at", "last_seen_at",
             "closed_at", "last_success_at", "last_attempt_at")


def _to_kst(df: pd.DataFrame) -> pd.DataFrame:
    """df 의 알려진 시간 컬럼들을 UTC → KST 문자열로 변환."""
    for c in _KST_COLS:
        if c in df.columns:
            ts = pd.to_datetime(df[c], errors="coerce", utc=True)
            df[c] = ts.dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")
    return df

def _df_sites() -> pd.DataFrame:
    sites = list_sites()
    if not sites:
        return pd.DataFrame()

    with get_conn() as conn:
        # 사이트별 누적 공고 수
        counts = {
            r["site_id"]: int(r["jobs_total"]) for r in conn.execute(
                "SELECT site_id, COUNT(*) AS jobs_total FROM jobs GROUP BY site_id"
            ).fetchall()
        }
        # 사이트별 최근 종료된 batch run 의 신규 공고 수 (정렬 기준)
        last_added = {
            r["site_id"]: int(r["jobs_added"] or 0) for r in conn.execute(
                """
                SELECT site_id, jobs_added
                  FROM crawl_runs
                 WHERE id IN (
                       SELECT MAX(id) FROM crawl_runs
                        WHERE ended_at IS NOT NULL
                        GROUP BY site_id
                 )
                """
            ).fetchall()
        }

    rows = []
    for s in sites:
        rows.append({
            "name": s.get("name") or "",
            "site_id": s["id"],
            "status": s["status"],
            "last_added": last_added.get(s["id"], 0),
            "home_url": s["home_url"],
            "sources": len(s.get("sources") or []),
            "jobs_total": counts.get(s["id"], 0),
            "consecutive_failures": s["consecutive_failures"],
            "last_success_at": s["last_success_at"] or "",
            "last_attempt_at": s["last_attempt_at"] or "",
            "status_reason": s["status_reason"] or "",
        })
    df = pd.DataFrame(rows)
    # 정렬: status (active/pending/paused 먼저, dead 마지막) → last_added desc
    _status_rank = {"active": 0, "pending": 1, "paused": 2, "dead": 3}
    df["_rank"] = df["status"].map(_status_rank).fillna(9).astype(int)
    df = (df.sort_values(by=["_rank", "last_added"], ascending=[True, False], kind="stable")
            .drop(columns=["_rank"])
            .reset_index(drop=True))
    return _to_kst(df)


def _df_jobs(site_id: str | None = None, limit: int = 200) -> pd.DataFrame:
    sql = """
        SELECT id, site_id, external_id, title, url, raw,
               first_seen_at, last_seen_at, closed_at
          FROM jobs
    """
    params: tuple = ()
    if site_id:
        sql += " WHERE site_id = ?"
        params = (site_id,)
    sql += " ORDER BY last_seen_at DESC LIMIT ?"
    params = (*params, limit)
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return _to_kst(pd.DataFrame(rows))


def _parse_raw(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


@st.cache_data(show_spinner=False)
def _read_media_data_uri(rel_path: str) -> str | None:
    """로컬 미디어 파일을 data: URI 로 인코딩 (iframe 안에서 렌더용)."""
    try:
        p = _media_abs(rel_path)
        if not p.exists():
            return None
        ct, _ = mimetypes.guess_type(p.name)
        ct = ct or "application/octet-stream"
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:{ct};base64,{b64}"
    except Exception:  # noqa: BLE001
        return None


def _embed_local_in_html(body_html: str, images: list) -> str:
    """body_html 안의 `/data/media/...` 참조를 data URI 로 치환."""
    if not body_html:
        return body_html
    out = body_html
    for img in images or []:
        if not isinstance(img, dict):
            continue
        local = img.get("local_path")
        src = img.get("src")
        if not local:
            continue
        data_uri = _read_media_data_uri(local)
        if not data_uri:
            continue
        out = out.replace("/" + local, data_uri)
        if src:
            out = out.replace(src, data_uri)
    return out


def _df_runs(site_id: str | None = None, limit: int = 50) -> pd.DataFrame:
    sql = """
        SELECT id, site_id, kind, started_at, ended_at, result,
               rows_seen,
               jobs_added, jobs_updated, jobs_unchanged, jobs_closed,
               (COALESCE(jobs_added,0)+COALESCE(jobs_updated,0)+COALESCE(jobs_unchanged,0)) AS jobs_seen,
               error
          FROM crawl_runs
    """
    params: tuple = ()
    if site_id:
        sql += " WHERE site_id = ?"
        params = (site_id,)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params = (*params, limit)
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return _to_kst(pd.DataFrame(rows))


def _site_sources(site_id: str) -> list[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT sources FROM sites WHERE id = ?", (site_id,)).fetchone()
    if not row:
        return []
    return json.loads(row["sources"] or "[]")


# ---------- Sidebar: actions ----------

st.sidebar.title("JobCrawler")

with st.sidebar.expander("➕ 사이트 등록", expanded=False):
    new_url = st.text_input("home_url", key="new_url",
                            placeholder="https://www.example.com")
    use_snippet = st.checkbox("snippet (정확↑/속도↓)", value=False)
    if st.button("등록 실행", type="primary", use_container_width=True):
        if not new_url:
            st.warning("home_url 을 입력하세요")
        else:
            with st.spinner(f"등록 중: {new_url}"):
                try:
                    rep = register(new_url, use_snippet=use_snippet)
                    st.success(f"[{rep.final_status}] {rep.site_id}")
                    st.json({
                        "discovery_count": len(rep.discovery.candidates) if rep.discovery else 0,
                        "classify": {
                            "full": sum(1 for c in rep.classifications if c.label == "full"),
                            "filtered": sum(1 for c in rep.classifications if c.label == "filtered"),
                            "personal": sum(1 for c in rep.classifications if c.label == "personal"),
                            "unknown": sum(1 for c in rep.classifications if c.label == "unknown"),
                        },
                        "validate_passed": sum(1 for v in rep.validations if v.ok),
                        "sources": rep.sources,
                        "notes": rep.notes,
                    })
                except Exception as e:  # noqa: BLE001
                    st.error(f"{type(e).__name__}: {e}")


# ---------- Top: status summary ----------

st.title("JobCrawler Dashboard")

df_sites = _df_sites()

c1, c2, c3, c4 = st.columns(4)
status_counts = df_sites["status"].value_counts().to_dict() if not df_sites.empty else {}
c1.metric("active", status_counts.get("active", 0))
c2.metric("pending", status_counts.get("pending", 0))
c3.metric("paused", status_counts.get("paused", 0))
c4.metric("dead", status_counts.get("dead", 0))

with get_conn() as conn:
    total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
st.metric("jobs total", total_jobs)
st.caption(
    "💡 배치 실시간 진행 보려면 CMD 에서 직접 실행하거나, "
    "스케줄 배치는 `tail -f logs/hourly_batch.log`"
)

st.divider()


# ---------- Tabs ----------

tab_sites, tab_jobs, tab_runs = st.tabs(["사이트", "공고", "크롤 이력"])


# ----- 사이트 -----
with tab_sites:
    if df_sites.empty:
        st.info("등록된 사이트가 없습니다. 사이드바에서 등록하세요.")
    else:
        status_filter = st.multiselect(
            "status 필터",
            options=["active", "pending", "paused", "dead"],
            default=[],
        )
        view = df_sites if not status_filter else df_sites[df_sites["status"].isin(status_filter)]
        st.dataframe(view, use_container_width=True, hide_index=True)

        st.subheader("사이트 상세")
        # name 같이 보여주는 라벨
        _name_by_id = dict(zip(df_sites["site_id"], df_sites["name"]))
        def _fmt(sid: str) -> str:
            n = _name_by_id.get(sid) or ""
            return f"{n}  ({sid})" if n else sid
        site_id = st.selectbox(
            "site_id",
            options=df_sites["site_id"].tolist(),
            format_func=_fmt,
            index=0,
        )
        if site_id:
            sources = _site_sources(site_id)
            st.write(f"**sources ({len(sources)})**")
            if sources:
                st.dataframe(pd.DataFrame(sources), use_container_width=True, hide_index=True)
            else:
                st.caption("(no sources)")

            colA, colB, colC = st.columns(3)
            with colA:
                if st.button("배치 즉시 실행", key=f"run_{site_id}", type="primary"):
                    with st.spinner(f"crawling {site_id}..."):
                        sr = run_site(site_id, fetch_details=False)
                    if sr.success:
                        st.success(
                            f"OK: rows={sr.rows_seen} +{sr.inserted} ~{sr.updated} "
                            f"closed={sr.closed}"
                        )
                    else:
                        st.error(f"FAIL: {sr.error}")
            with colB:
                site_row = next((s for s in list_sites() if s["id"] == site_id), None)
                cur_status = site_row["status"] if site_row else ""
                new_status = st.selectbox(
                    "status 변경",
                    options=["active", "pending", "paused", "dead"],
                    index=["active", "pending", "paused", "dead"].index(cur_status)
                          if cur_status in ["active","pending","paused","dead"] else 0,
                    key=f"status_{site_id}",
                )
                if st.button("적용", key=f"apply_{site_id}"):
                    update_status(site_id, new_status, reason="manual")
                    st.success(f"{site_id}: {cur_status} → {new_status}")
                    st.rerun()
            with colC:
                st.caption(
                    f"failures: {site_row['consecutive_failures'] if site_row else '-'}"
                )


# ----- 공고 -----
with tab_jobs:
    _name_by_id_jobs = dict(zip(df_sites["site_id"], df_sites["name"])) if not df_sites.empty else {}
    def _fmt_jobs(sid: str) -> str:
        if sid == "(전체)":
            return sid
        n = _name_by_id_jobs.get(sid) or ""
        return f"{n}  ({sid})" if n else sid
    site_options = ["(전체)"] + (df_sites["site_id"].tolist() if not df_sites.empty else [])
    sel = st.selectbox("site_id 필터", options=site_options, format_func=_fmt_jobs,
                        index=0, key="jobs_site")
    limit = st.slider("표시 건수", 50, 1000, 200, 50)
    df_jobs = _df_jobs(None if sel == "(전체)" else sel, limit=limit)
    if df_jobs.empty:
        st.info("공고 없음")
    else:
        st.caption(f"{len(df_jobs)} rows (open + closed 모두) — 행 클릭 시 본문 표시")
        df_view = df_jobs.drop(columns=["raw"])
        evt = st.dataframe(
            df_view,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="jobs_table",
        )
        sel_rows = getattr(evt, "selection", {}).get("rows", []) if evt else []
        if sel_rows:
            row = df_jobs.iloc[sel_rows[0]]
            raw = _parse_raw(row.get("raw"))
            st.subheader(row["title"] or "(제목 없음)")
            meta_cols = st.columns(3)
            meta_cols[0].caption(f"site: {row['site_id']}")
            meta_cols[1].caption(f"first_seen: {row['first_seen_at']}")
            meta_cols[2].caption(
                f"closed: {row['closed_at']}" if row["closed_at"] else "open"
            )
            if row.get("url"):
                st.markdown(f"🔗 [원문 보기]({row['url']})")
            images = raw.get("images") or []
            attachments = raw.get("attachments") or []

            body_html_raw = raw.get("body_html_local") or raw.get("body_html") or ""
            body_text = (raw.get("body") or raw.get("description")
                         or raw.get("snippet") or "")
            if body_html_raw:
                body_html_render = _embed_local_in_html(body_html_raw, images)
                st.markdown("**본문 (HTML)**")
                st.components.v1.html(body_html_render, height=600, scrolling=True)
            elif body_text:
                st.markdown("**본문**")
                st.write(body_text)
            else:
                st.caption("(본문 없음 — detail 미수집)")

            if images:
                with st.expander(f"이미지 ({len(images)})"):
                    cols = st.columns(min(3, len(images)))
                    for i, img in enumerate(images[:9]):
                        if isinstance(img, dict):
                            local = img.get("local_path")
                            src = img.get("src")
                        else:
                            local, src = None, img
                        if local:
                            p = _media_abs(local)
                            if p.exists():
                                cols[i % len(cols)].image(
                                    str(p), use_container_width=True,
                                    caption=f"{img.get('size',0)//1024} KB"
                                    if isinstance(img, dict) else None,
                                )
                                continue
                        cols[i % len(cols)].markdown(
                            f'<a href="{src}" target="_blank">'
                            f'<img src="{src}" style="width:100%;border:1px solid #ddd"/></a>',
                            unsafe_allow_html=True,
                        )
                    if len(images) > 9:
                        st.caption(f"... +{len(images) - 9} 더 있음 (raw JSON 참고)")

            if attachments:
                st.markdown(f"**첨부 파일 ({len(attachments)})**")
                for a in attachments:
                    if not isinstance(a, dict):
                        continue
                    label = a.get("text") or a.get("src") or ""
                    ext = a.get("ext_orig") or a.get("ext") or ""
                    local = a.get("local_path")
                    if local and _media_abs(local).exists():
                        size_kb = (a.get("size") or 0) // 1024
                        st.markdown(
                            f"- {label}  `.{ext}` ({size_kb} KB) — "
                            f"[원문]({a.get('src','')})"
                        )
                        with open(_media_abs(local), "rb") as f:
                            st.download_button(
                                label=f"📥 {Path(local).name}",
                                data=f.read(),
                                file_name=Path(local).name,
                                key=f"dl_{a.get('sha256', label)}",
                            )
                    else:
                        err = a.get("error") or "(미다운로드)"
                        st.markdown(
                            f"- [{label}]({a.get('src','')}) `.{ext}` — {err}"
                        )

            iframes = raw.get("iframes") or []
            videos = raw.get("videos") or []
            if iframes or videos:
                with st.expander(f"임베드 ({len(iframes)} iframe / {len(videos)} video)"):
                    for u in iframes:
                        st.markdown(f"- iframe: {u}")
                    for u in videos:
                        st.markdown(f"- video: {u}")
                        try:
                            st.video(u)
                        except Exception:  # noqa: BLE001
                            pass

            tables = raw.get("tables") or []
            if tables:
                with st.expander(f"표 ({len(tables)})"):
                    for i, tbl in enumerate(tables, 1):
                        st.caption(f"table {i}")
                        try:
                            st.dataframe(pd.DataFrame(tbl), use_container_width=True,
                                         hide_index=True)
                        except Exception:  # noqa: BLE001
                            st.write(tbl)

            emails = raw.get("emails") or []
            phones = raw.get("phones") or []
            if emails or phones:
                cc = st.columns(2)
                if emails:
                    cc[0].markdown("**이메일**\n" + "\n".join(f"- {e}" for e in emails))
                if phones:
                    cc[1].markdown("**전화**\n" + "\n".join(f"- {p}" for p in phones))

            links = raw.get("links") or []
            if links:
                with st.expander(f"본문 링크 ({len(links)})"):
                    for ln in links:
                        u = ln.get("url", "") if isinstance(ln, dict) else str(ln)
                        t = ln.get("text", "") if isinstance(ln, dict) else ""
                        st.markdown(f"- [{t or u}]({u})")

            jsonld = raw.get("jsonld") or []
            meta_dict = raw.get("meta") or {}
            if jsonld:
                with st.expander(f"JSON-LD ({len(jsonld)})"):
                    st.json(jsonld)
            if meta_dict:
                with st.expander(f"meta tags ({len(meta_dict)})"):
                    st.json(meta_dict)

            with st.expander("raw JSON 전체"):
                st.json(raw or {"_": "(empty)"})


# ----- 크롤 이력 -----
with tab_runs:
    _name_by_id_runs = dict(zip(df_sites["site_id"], df_sites["name"])) if not df_sites.empty else {}
    def _fmt_runs(sid: str) -> str:
        if sid == "(전체)":
            return sid
        n = _name_by_id_runs.get(sid) or ""
        return f"{n}  ({sid})" if n else sid
    site_options = ["(전체)"] + (df_sites["site_id"].tolist() if not df_sites.empty else [])
    sel = st.selectbox("site_id 필터", options=site_options, format_func=_fmt_runs,
                        index=0, key="runs_site")
    df_runs = _df_runs(None if sel == "(전체)" else sel, limit=100)
    if df_runs.empty:
        st.info("아직 크롤 이력 없음")
    else:
        st.dataframe(df_runs, use_container_width=True, hide_index=True)
