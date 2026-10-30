# step4 — 개인 이직용 공고 수집·분석 (jobhunt)

step3 크롤러(`crawlers/`)를 바탕으로, **내가 노리는 포지션만** 모아서
적합도를 매기고 "무엇을 준비해야 하는지"를 집계하는 개인 파이프라인.
팀 프로젝트 DB(`data/crawler.db`)와 분리된 `data/jobhunt.db` 를 쓴다.

## 흐름

```
collect   원티드 개발직군 리스트 전체 (≈2,700건, 100건/페이지)
            └ 1차 필터 (profile.py: ML 카테고리 or 제목 키워드, 연차 범위)  → ≈740건
            └ 매칭 공고만 상세 본문 (주요업무/자격요건/우대사항이 섹션별로 옴)
            └ 리스트에서 사라진 공고 = 마감 처리
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
```

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
