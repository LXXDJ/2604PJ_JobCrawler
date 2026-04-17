"""
DB 확인용 간단한 스크립트
사용: python check_db.py
"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = "data/jobs.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# 사이트별 통계
print("=" * 60)
print("DB 전체 현황")
print("=" * 60)
stats = conn.execute("""
    SELECT source, COUNT(*) as count,
           SUM(CASE WHEN content != '' THEN 1 ELSE 0 END) as with_content
    FROM jobs
    GROUP BY source
""").fetchall()
for s in stats:
    print(f"[{s['source']}] 총 {s['count']}건 (본문 있음: {s['with_content']}건)")

# 공고 3건 전체 내용 출력
print(f"\n{'=' * 60}")
print("공고 상세 내용 샘플 3건")
print("=" * 60)
samples = conn.execute("""
    SELECT source, external_id, title, company, location, salary,
           job_type, pub_date, link, content
    FROM jobs
    WHERE content != ''
    ORDER BY first_seen_at DESC
    LIMIT 3
""").fetchall()

for i, job in enumerate(samples):
    print(f"\n{'─' * 60}")
    print(f"[{i+1}] {job['title']}")
    print(f"{'─' * 60}")
    print(f"소스       : {job['source']}")
    print(f"공고 ID    : {job['external_id']}")
    print(f"회사       : {job['company']}")
    print(f"지역       : {job['location']}")
    print(f"급여       : {job['salary']}")
    print(f"고용형태   : {job['job_type']}")
    print(f"게시일     : {job['pub_date']}")
    print(f"링크       : {job['link']}")
    print(f"\n[본문 내용]")
    content = job['content'] or '(본문 없음)'
    # 본문 500자만 미리보기
    if len(content) > 500:
        print(content[:500] + "\n...(생략)")
    else:
        print(content)

# 크롤링 실행 이력
print(f"\n{'=' * 60}")
print("크롤링 실행 이력")
print("=" * 60)
runs = conn.execute("""
    SELECT source, started_at, new_count, updated_count
    FROM crawl_runs
    ORDER BY started_at DESC
    LIMIT 5
""").fetchall()
for run in runs:
    print(f"  [{run['source']}] {run['started_at'][:19]} "
          f"→ 신규 {run['new_count']}, 재확인 {run['updated_count']}")

conn.close()
