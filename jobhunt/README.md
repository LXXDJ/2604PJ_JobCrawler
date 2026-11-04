# step4 — 개인 이직용 공고 수집·분석 (jobhunt)

step3 크롤러(`crawlers/`)를 바탕으로, **내가 노리는 포지션만** 모아서
적합도를 매기고 "무엇을 준비해야 하는지"를 집계하는 개인 파이프라인.
팀 프로젝트 DB(`data/crawler.db`)와 분리된 `data/jobhunt.db` 를 쓴다.

## 흐름

```
collect   원티드   개발직군 리스트 전체 (≈2,600건) — 리스트에서 사라지면 마감
          잡코리아 키워드 검색 등록일순 (20건/페이지, 키워드당 최대 30p) — RSC payload JSON
          사람인   키워드 검색 등록일순 (100건/페이지, 키워드당 최대 10p) — HTML
            검색형 두 곳은 새 공고 없는 페이지에서 멈춤 → 매일은 1~2페이지
            마감: 마감일 지남 / 상시 공고는 30일 안 보이면
            └ 1차 필터 (profile.py: 사이트 AI 직무 분류 or 제목 키워드, 제외어, 연차 범위)
            └ 사이트 간 중복 묶기 (회사명·제목 정규화) — 대표 우선순위 원티드 > 잡코리아 > 사람인
            └ 매칭 대표 공고만 상세 본문 (원티드는 섹션별, 나머지는 통짜 텍스트 + 이미지 URL)
            └ 본문이 이미지뿐인 공고(사람인·잡코리아 ~20%) → ocr 단계에서 텍스트로
ocr       이미지를 폭 1000px·세로 1100px 조각으로 잘라 claude -p (Read 도구만 허용) 로 받아쓰기 → body 에 저장
            Windows 내장 OCR 은 영문 기술 용어가 깨져서 제외. 1건 ≈ 30~80초
extract   claude -p (구독 인증, haiku) 로 공고 6건씩 묶어 구조화 추출 + 적합도
            → insights: role_type, 필수/우대 기술(정규화), 갭, fit_score
notify    적합도 65+ 신규 공고 → 개인 Slack
report    적합도 60+ 공고 기준 필수/우대 기술 빈도, 내 갭 TOP
```

## 명령

```bash
python -m scripts.jobhunt.run collect
python -m scripts.jobhunt.run extract --max 60      # 회차당 상한 (구독 한도 보호)
python -m scripts.jobhunt.run notify --dry-run      # 메시지 미리보기
python -m scripts.jobhunt.run notify
python -m scripts.jobhunt.run report
python -m scripts.jobhunt.run stats
python -m scripts.jobhunt.run serve                 # 대시보드 http://127.0.0.1:8787/
python -m scripts.jobhunt.run companies             # 회사 태그 갱신 (30일 지난 것만)
python -m scripts.jobhunt.run career --max 80       # 커리어 관점 포지션 평가 (추천도용)
python -m scripts.jobhunt.run daily                 # collect → companies → extract → career → notify → dashboard 파일
```

## 적합도 vs 추천도

| | 적합도 (`insights.fit_score`) | 추천도 (대시보드에서 계산) |
|---|---|---|
| 질문 | 지금 내 이력으로 붙을 수 있고 내 목표와 맞나 | 이 자리가 내 커리어에 얼마나 도움이 되나 (내 이력과 무관) |
| 근거 | LLM + `private/profile.md` | 포지션: LLM 1~5점 (`career` — 전문성·업무 범위·AI 핵심도·보상복지)<br>회사: 원티드 회사 태그 규칙 (`companies` — 인원·연봉 백분위·설립·투자·퇴사율·복지) |
| 프로필 바뀌면 | 다시 돌려야 함 | 다시 돌릴 필요 없음 |

추천도 = 7개 항목(0~100)의 가중 평균. 가중치는 대시보드 "추천순"에서 조절 (브라우저에 저장).
추천순 + 점수 하한으로 거른 뒤 "내 부족한 점"을 보면 = 좋은 자리에 가려면 뭘 채워야 하는지.
매출·이익은 원티드에 없어 미반영.

## 자동 실행 (Windows 작업 스케줄러)

| 작업 | 트리거 | 내용 |
|---|---|---|
| `JobHunt-Dashboard` | 로그인 시 | `pythonw -m scripts.jobhunt.run serve` — 127.0.0.1 전용, 요청마다 DB 최신 상태로 렌더 |
| `JobHunt-Daily` | 매일 01:00 (놓치면 켜질 때 바로) | `pythonw -m scripts.jobhunt.run daily` — 최대 4시간 |

- 로그: `data/logs/jobhunt.log` (pythonw 는 콘솔이 없어서 run.py 가 파일로 돌림)
- 열어둔 대시보드는 5분마다 데이터 변경을 확인해 새로고침 안내를 띄움
- 끄기: `Disable-ScheduledTask -TaskName JobHunt-Daily` / 삭제: `Unregister-ScheduledTask -TaskName JobHunt-* -Confirm:$false`

## 설정

- `.env`
  - `JOBHUNT_SLACK_WEBHOOK_URL=` — 개인 채널용. 팀 `SLACK_WEBHOOK_URL` 로는 절대 보내지 않음 (fallback 없음)
  - `JOBHUNT_LLM_MODEL=haiku` — 선택. `sonnet` 등으로 변경 가능
- `private/profile.md` — LLM 적합도 판단용 내 경력 요약 (**gitignore**, 없으면 `profile.example.md`)
- `jobhunt/profile.py` — 1차 필터 키워드 / 제외어 / 연차 범위

## LLM 을 구독으로 쓰는 방식

- `claude -p --output-format json --json-schema ...` → 응답의 `structured_output` 사용
- `--system-prompt` 로 Claude Code 기본 시스템 프롬프트(~10K 토큰)를 교체해 사용량 절약
- `--bare` 는 OAuth(구독)를 읽지 않으므로 사용 불가
- 빈 임시 디렉터리에서 실행해 프로젝트 CLAUDE.md 등을 읽지 않게 함
- API 로 바꾸려면 `jobhunt/llm.py` 의 `call_json()` 만 교체

## 프로필이 바뀌면

`insights` 만 지우고 `extract` 를 다시 돌리면 된다 (수집 데이터는 그대로).
