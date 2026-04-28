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

from crawlers.batch.runner import run_site
from crawlers.infra.db import get_conn, init_db
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

    # 사이트별 누적 공고 수
    with get_conn() as conn:
        counts = {
            r["site_id"]: int(r["jobs_total"]) for r in conn.execute(
                "SELECT site_id, COUNT(*) AS jobs_total FROM jobs GROUP BY site_id"
            ).fetchall()
        }

    rows = []
    for s in sites:
        rows.append({
            "name": s.get("name") or "",
            "site_id": s["id"],
            "status": s["status"],
            "home_url": s["home_url"],
            "sources": len(s.get("sources") or []),
            "jobs_total": counts.get(s["id"], 0),
            "consecutive_failures": s["consecutive_failures"],
            "last_success_at": s["last_success_at"] or "",
            "last_attempt_at": s["last_attempt_at"] or "",
            "status_reason": s["status_reason"] or "",
        })
    return _to_kst(pd.DataFrame(rows))


def _df_jobs(site_id: str | None = None, limit: int = 200) -> pd.DataFrame:
    sql = """
        SELECT id, site_id, external_id, title, url,
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
        st.caption(f"{len(df_jobs)} rows (open + closed 모두)")
        st.dataframe(df_jobs, use_container_width=True, hide_index=True)


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
