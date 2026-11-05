# jobhunt — 개인 이직용 공고 수집·분석 파이프라인

원티드·잡코리아·사람인에서 AI·개발 공고를 매일 모으고, LLM 으로 공고를 구조화한 뒤
**"지금 내 이력으로 맞는 자리인가(적합도)"** 와 **"내 커리어에 도움이 되는 자리인가(추천도)"** 를 따로 매긴다.
결과는 로컬 대시보드와 개인 Slack 으로 받는다.

- step3 팀 크롤러(`crawlers/`, 저장소 루트 README)를 바탕으로 만든 별도 파이프라인. DB 는 분리,
  `.env` 로딩·Slack 전송(`crawlers/infra`)만 공유
- 개인 데이터(DB·대시보드·로그)는 프로젝트 폴더(OneDrive 동기화) 밖 `~/jobhunt-data/` 에 둔다
- LLM 은 API 결제 없이 Claude 구독 + `claude -p` (headless) 로 호출

## 흐름 (매일 00:00 배치 `daily`)

```
collect     원티드   개발직군 리스트 전체 — 리스트에서 사라지면 마감
            잡코리아 키워드 검색 등록일순 (RSC payload JSON) — 지역 코드로 시·도 보정
            사람인   키워드 검색 등록일순 (HTML)
              └ 1차 필터 (profile.py): 사이트 AI 직무 분류 or 제목 키워드 / 제외어 / 연차 범위  → 약 20% 통과
              └ 사이트 간 중복 묶기 (회사명·제목 정규화) — 대표 우선순위 원티드 > 잡코리아 > 사람인
              └ 통과한 대표 공고만 상세 본문 수집 (나머지는 목록 정보만 → 비용 거의 없음)
companies   회사 정보 — 원티드 태그(연봉 백분위·투자·퇴사율), 사람인·잡코리아 기업정보(사원수·매출·영업이익)
ocr         본문이 이미지뿐인 공고 → 이미지를 폭 1000px·세로 1100px 조각으로 잘라 claude -p (Read) 로 받아쓰기
              이미지 다운로드 실패는 다음 배치에서 재시도, 3회 실패하면 포기 → 완료 알림에 "자동 분석 불가" 로 링크 표시
extract     LLM 구조화 추출 (6건씩 묶어 1회 호출, sonnet) → insights
              업무 범위가 60~85점(경계 구간)인 공고는 한 번 더 판단해 평균 — 같은 공고도 ±5~7점 흔들려서
              직무 유형·요약·필수/우대 기술(정규화)·요구 연차·도메인·영어 필수·연봉 원문
              + 업무 범위 적합도(scope_score) — 기술 스택은 빼고 "하는 일이 내 경력·목표와 맞나" 만
skills      기술 표기 분류 — 공고에서 나온 표기마다 보유/일부/없음/기술 아님 을 한 번만 판단 (sonnet)
              → 적합도·부족한 점은 코드가 재계산 (아래 "적합도 계산")
career      커리어 평가 (8건씩, haiku) — 전문성 성장·업무 범위·AI 핵심도·보상 1~5점 + 쌓이는 역량
dashboard   HTML 파일 갱신
notify      적합도 70+ 신규 공고 → 개인 Slack (JOBHUNT_SLACK_WEBHOOK_URL) — 기준은 report.HIGH_FIT 하나로 대시보드와 공유
```

- 단계 하나가 실패해도 다음 단계는 진행. 진행 상태는 `logs/daily_status.json`
- 08:00 점검(`check`): 배치가 안 끝났으면 진행 중 / 오류 / 미실행 을 Slack 으로 알림
- 회차당 처리 상한 없음 (새벽 배치라). 단독 실행 시에는 `--max` 로 제한

## 적합도 vs 추천도

| | 적합도 (`insights.fit_score`) | 추천도 (대시보드에서 계산) |
|---|---|---|
| 질문 | 지금 내 이력으로 붙을 수 있고 내 목표와 맞나 | 이 자리가 내 커리어에 얼마나 도움이 되나 |
| 내 프로필 | 사용 | 연봉·근무지만 사용 (나머지는 공고·회사 자체 평가) |
| 프로필 바뀌면 | 경력·목표 → 재분석 / 보유 기술 → 즉시 재계산 | 재분석 불필요 |

### 적합도 계산 — LLM 판단과 코드 계산 분리

```
fit_score = 0.75 × 업무 범위(LLM) + 0.25 × 기술 점수(코드)
기술 점수 = 0.75 × 필수 기술 충족률 + 0.25 × 우대 기술 충족률   (일부 = 0.5, 기술 아님 = 제외)
부족한 점 = 미보유 기술(코드) + 기술 외 갭(LLM, 고정 라벨: 경력 연차·학위·논문·도메인·영어·리더십)
```

- LLM 에는 `## 보유 기술` 섹션을 보내지 않는다 → 기술 스택을 고쳐도 LLM 재분석이 필요 없음
- 기술 표기 분류표(`skill_labels`)는 표기 단위로 한 번만 LLM 판단 → 동의어·상위 개념("pgvector → Vector DB")은
  LLM 이, 점수·갭 산술은 코드가. 새 표기만 매일 분류하고, 보유 기술 섹션이 바뀌면 전체 재분류 (약 30분)
- 기술 표기가 3개 미만인 공고(기획직 등)는 기술 점수를 쓰지 않음 — 1~2개가 다 보유면 100점이 되는 왜곡 방지
- 연차는 프로필 "연차" 줄의 값으로만 비교 (AI 직무는 AI 연차, 백엔드는 백엔드 연차 …). LLM 날짜 계산 오류 방지
- 분류가 틀리면 직접 고정: `skills --pin "Docker Compose=has"`

### 추천도 — 항목별 0~100 점수의 가중 평균 (대시보드에서 가중치 조절, 브라우저에 저장)

| 그룹 | 항목 | 근거 |
|---|---|---|
| 포지션 | 전문성 성장 · 업무 범위·리드 경로 · AI 핵심도 | `career` LLM 1~5점 |
| 회사 | 연봉 수준 | 공고에 금액이 있으면 **내 현재 연봉 대비** (미달 0 / 인상 10% 미만 40 / 10~20% 75 / 20%+ 100), 없으면 원티드 연봉 백분위 |
| 회사 | 보상·복지 · 회사 규모 · 매출 규모 · 안정성·성장 | LLM 보상 점수 + 회사 태그·재무 |
| 회사 | 근무지 | 서울 100 / 경기·인천 50 / 그 외 0 |

연봉 미달 공고도 빼지 않고 추천도에서만 밀리게 한다 (목록에 "연봉 미달" 태그).

## 명령

```bash
python -m scripts.jobhunt.run daily                    # 매일 배치 전체
python -m scripts.jobhunt.run collect [--source saramin]
python -m scripts.jobhunt.run extract --max 60          # 미분석 공고 분석 (+ 새 기술 표기 분류)
python -m scripts.jobhunt.run extract --refit 50 --max 1000   # 경력·목표 수정 후: 적합도 50+ · 마감 전 공고 재분석
python -m scripts.jobhunt.run skills [--all]            # 기술 표기 분류 + 점수 재계산 (보유 기술만 바꿨을 때)
python -m scripts.jobhunt.run skills --show na          # 분류 보기 (has / partial / none / na)
python -m scripts.jobhunt.run skills --pin "Azure=partial"
python -m scripts.jobhunt.run ocr --max 40
python -m scripts.jobhunt.run career --max 80
python -m scripts.jobhunt.run notify --dry-run
python -m scripts.jobhunt.run serve                     # 대시보드 http://127.0.0.1:8787/
python -m scripts.jobhunt.run stats / report / check
```

## 대시보드

- `serve` 가 127.0.0.1 전용으로 띄움. 요청마다 DB 최신 상태로 렌더
- 열어둔 페이지는 5분마다(+ 탭으로 돌아올 때) `/api/version` 을 비교해, 바뀌었으면 `/api/data` 로 받아 다시 그림
  — 필터·페이지·펼친 행·스크롤 유지
- 개인 적합도·연봉 데이터라 외부에 게시하지 않음

## 자동 실행 (Windows 작업 스케줄러)

| 작업 | 트리거 | 내용 |
|---|---|---|
| `JobHunt-Daily` | 매일 00:00 (놓치면 켜질 때 바로) | `pythonw -m scripts.jobhunt.run daily` — 최대 12시간 |
| `JobHunt-Check` | 매일 08:00 | `check` — 배치 미완료 시 Slack |
| `JobHunt-Dashboard` | 로그인 시 | `serve` |

- 로그: `~/jobhunt-data/logs/jobhunt.log` (pythonw 는 콘솔이 없어서 run.py 가 파일로 돌림)
- 끄기: `Disable-ScheduledTask -TaskName JobHunt-Daily`

## 설정

- `.env`
  - `JOBHUNT_SLACK_WEBHOOK_URL=` — 개인 채널. 팀 `SLACK_WEBHOOK_URL` 로는 절대 보내지 않음 (fallback 없음)
  - `JOBHUNT_LLM_MODEL=haiku` — 선택. OCR·커리어 평가 모델 (공고 분석은 `extract.MODEL` = sonnet)
  - `JOBHUNT_DATA_DIR=` — 선택. 개인 데이터 폴더 (기본 `~/jobhunt-data`)
- `private/profile.md` (**gitignore**, 없으면 `profile.example.md`)
  - LLM 에 보내는 부분: 지원 시점, 연차, 경력, 보강 중, 목표, 선호/비선호
  - 코드만 쓰는 부분: `## 보유 기술` (기술 매칭), `## 연봉` (현재 연봉 — 협상 후 숫자만 바꾸면 됨)
- `jobhunt/profile.py` — 1차 필터 키워드 / 제외어 / 연차 범위

## LLM 을 구독으로 쓰는 방식

- `claude -p --output-format json --json-schema ...` → 응답의 `structured_output` 사용
- `--system-prompt` 로 Claude Code 기본 시스템 프롬프트(~10K 토큰)를 교체해 사용량 절약
- `--bare` 는 OAuth(구독)를 읽지 않으므로 사용 불가
- 빈 임시 디렉터리에서 실행해 프로젝트 CLAUDE.md 등을 읽지 않게 함
- API 로 바꾸려면 `jobhunt/llm.py` 의 `call_json()` 만 교체

## 파일

```
jobhunt/
├── sources/      원티드·잡코리아·사람인 목록·상세·회사 정보
├── collect.py    수집 + 1차 필터 + 중복 묶기 + 상세
├── ocr.py        이미지 공고 받아쓰기
├── extract.py    LLM 구조화 추출 + 업무 범위 적합도
├── fit.py        기술 표기 분류표 + 최종 적합도·부족한 점 계산
├── career.py     커리어 평가 + 회사 점수
├── profile.py    1차 필터 + 프로필 읽기 (LLM 용 / 코드 전용 섹션 분리)
├── dashboard.py  대시보드 (데이터 + HTML 템플릿, 연봉·근무지 점수)
├── server.py     로컬 서버 (/, /api/version, /api/data)
├── report.py     Slack 메시지 (신규 고적합 / 배치 완료 / 08:00 점검)
├── batch_status.py  배치 진행 상태 파일
├── llm.py        claude -p 래퍼
└── db.py         SQLite 스키마·마이그레이션 (~/jobhunt-data/jobhunt.db)
```
