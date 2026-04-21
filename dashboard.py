"""
JobCrawler 대시보드 (Streamlit).

실행:
    streamlit run dashboard.py

읽는 것: data/jobs.db 의 jobs / crawl_runs 테이블.
쓰는 것: 없음 (읽기 전용).
"""

import os
import sys
import sqlite3
import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data", "jobs.db")

# site_id → 한글 라벨 매핑은 crawlers/site_labels.py 에 중앙화 (slack_notifier 와 공유).
sys.path.insert(0, os.path.join(ROOT, "crawlers"))
from site_labels import SITE_LABELS, label  # noqa: E402 — sys.path 세팅 이후여야 함


st.set_page_config(page_title="JobCrawler Dashboard", layout="wide")


@st.cache_data(ttl=60)
def load_runs() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql(
            "SELECT id, source, started_at, finished_at, new_count, updated_count, error "
            "FROM crawl_runs ORDER BY started_at DESC",
            conn,
        )
    if df.empty:
        return df
    df["started_at"] = pd.to_datetime(df["started_at"], errors="coerce")
    df["finished_at"] = pd.to_datetime(df["finished_at"], errors="coerce")
    df["date"] = df["started_at"].dt.date
    # NULL 은 NaN 으로 읽혀 truthy 로 잡히므로 pd.isna + 빈 문자열 체크 모두 필요
    df["status"] = df["error"].apply(
        lambda e: "ok" if (pd.isna(e) or not str(e).strip()) else "error"
    )
    df["site"] = df["source"].map(label)
    return df


@st.cache_data(ttl=60)
def load_jobs_summary() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql(
            "SELECT source, COUNT(*) AS total, "
            "MIN(first_seen_at) AS first_seen, MAX(last_seen_at) AS last_seen "
            "FROM jobs GROUP BY source ORDER BY total DESC",
            conn,
        )
    if not df.empty:
        df["site"] = df["source"].map(label)
    return df


@st.cache_data(ttl=60)
def load_jobs_daily() -> pd.DataFrame:
    """사이트별 일별 신규 공고 수 — first_seen_at 기준."""
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql(
            "SELECT source, DATE(first_seen_at) AS date, COUNT(*) AS new_jobs "
            "FROM jobs GROUP BY source, DATE(first_seen_at) ORDER BY date",
            conn,
        )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df["site"] = df["source"].map(label)
    return df


# ============================================================
# 헤더
# ============================================================

st.title("JobCrawler 대시보드")

if not os.path.exists(DB_PATH):
    st.error(f"DB 파일을 찾을 수 없음: {DB_PATH}")
    st.stop()

runs = load_runs()
jobs = load_jobs_summary()
jobs_daily = load_jobs_daily()

# ============================================================
# 상단 KPI
# ============================================================

total_jobs = int(jobs["total"].sum()) if not jobs.empty else 0
total_sites = len(jobs) if not jobs.empty else 0
today = pd.Timestamp.now().normalize().date()
today_runs = runs[runs["date"] == today] if not runs.empty else pd.DataFrame()
today_new = int(today_runs["new_count"].sum()) if not today_runs.empty else 0

# 사이트별 '오늘의 최신 run' 상태로 에러 집계 — 같은 사이트가 실패→성공이면 성공으로 본다
if not today_runs.empty:
    latest_today = today_runs.sort_values("started_at").groupby("source").tail(1)
    today_errors = int((latest_today["status"] == "error").sum())
else:
    today_errors = 0

c1, c2, c3, c4 = st.columns(4)
c1.metric("총 공고 수", f"{total_jobs:,}")
c2.metric("수집 사이트", f"{total_sites}")
c3.metric("오늘 신규", f"{today_new:,}")
c4.metric("오늘 에러 사이트", f"{today_errors}", delta_color="inverse")

st.divider()

# ============================================================
# 사이트별 누적 공고 수 + 일별 신규 공고 추이
# ============================================================

col_left, col_right = st.columns([1, 2])

with col_left:
    st.subheader("사이트별 누적")
    if jobs.empty:
        st.info("아직 수집된 공고가 없음.")
    else:
        fig = px.bar(
            jobs,
            x="total",
            y="site",
            orientation="h",
            text="total",
            labels={"total": "공고 수", "site": ""},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=400)
        fig.update_traces(textposition="outside")
        st.plotly_chart(fig, use_container_width=True)

with col_right:
    st.subheader("사이트별 일별 신규 공고")
    if jobs_daily.empty:
        st.info("데이터 없음.")
    else:
        days_back = st.slider("최근 N일", 3, 60, 14, key="daily_window")
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=days_back)
        recent = jobs_daily[jobs_daily["date"] >= cutoff]
        fig = px.bar(
            recent,
            x="date",
            y="new_jobs",
            color="site",
            labels={"new_jobs": "신규 공고", "date": "날짜", "site": ""},
            barmode="stack",
        )
        fig.update_layout(height=400, legend_title="")
        st.plotly_chart(fig, use_container_width=True)

st.divider()

# ============================================================
# 사이트별 일별 run 요약 — crawl_runs 기반 히트맵
# ============================================================

st.subheader("사이트별 일별 신규/재확인 (crawl_runs)")
if runs.empty:
    st.info("아직 crawl_runs 이 비어있음.")
else:
    window = st.slider("최근 N일", 3, 60, 14, key="runs_window")
    cutoff = (pd.Timestamp.now() - pd.Timedelta(days=window)).date()
    recent_runs = runs[runs["date"] >= cutoff].copy()

    # 하루에 여러 run 이 있으면 합산
    agg = (
        recent_runs.groupby(["date", "site"], as_index=False)
        .agg(new_count=("new_count", "sum"), updated_count=("updated_count", "sum"),
             errors=("status", lambda s: (s == "error").sum()))
    )
    agg["total"] = agg["new_count"] + agg["updated_count"]

    pivot = agg.pivot(index="site", columns="date", values="new_count").fillna(0)
    fig = px.imshow(
        pivot,
        labels={"x": "날짜", "y": "사이트", "color": "신규"},
        color_continuous_scale="Blues",
        aspect="auto",
        text_auto=True,
    )
    fig.update_layout(height=max(300, 40 * len(pivot)))
    st.plotly_chart(fig, use_container_width=True)

st.divider()

# ============================================================
# 최근 crawl_runs 테이블
# ============================================================

st.subheader("최근 crawl_runs")
if runs.empty:
    st.info("데이터 없음.")
else:
    show_errors_only = st.checkbox("에러만 보기", value=False)
    limit = st.number_input("최대 행 수", min_value=10, max_value=500, value=50, step=10)
    table = runs[runs["status"] == "error"] if show_errors_only else runs
    table = table.head(int(limit))[
        ["started_at", "site", "status", "new_count", "updated_count", "error"]
    ].rename(columns={
        "started_at": "시작",
        "site": "사이트",
        "status": "상태",
        "new_count": "신규",
        "updated_count": "재확인",
        "error": "에러",
    })
    st.dataframe(table, use_container_width=True, hide_index=True)

st.caption("데이터는 60초 캐시됨. 상단 우측 ⋯ → Rerun 으로 강제 갱신.")
