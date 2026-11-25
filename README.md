# 네이버 플레이스·검색광고 소재 오토파일럿

> 👉 **처음이면 [START_HERE.md](START_HERE.md) 만 보시면 됩니다.** 아래는 기술 설명입니다.

네이버 플레이스 순위를 매일 기록하고, **입찰가는 건드리지 않은 채** 검색광고(파워링크) 소재만 교체합니다.
새 소재는 같은 업계에서 반응이 좋은 콘텐츠(유튜브·블로그·직접 넣은 릴스 등)의 소구 패턴을 참고해
자동으로 만들고, **사람이 승인한 것만** 계정에 반영합니다.

운영 방식(기본): **GitHub Actions(무료)** 가 하루 2번 `run`, 승인은 **GitHub 이슈에 👍/👎**.
서버·상시 켜둔 PC 불필요. 실행 기록(SQLite)은 `autopilot-data` 브랜치에 자동 저장.
(직접 서버에서 돌리고 싶으면 `deploy/crontab.example`, 텔레그램 승인은 `approvals.channel: telegram`)

```
 [Actions] 하루 2회 run                                [Actions] 댓글 즉시 / 1시간마다 check-approvals
   │                                                      │
   ① track    플레이스 순위 측정 → 하락 알림                │
   ② sync     소재·키워드·통계 수집 (읽기 전용)             │
   ③ advance  검수 통과한 새 소재가 있으면 → 패자 소재 중지   │
   ④ inspire  업계 인기 콘텐츠 수집 (7일마다)               │
   ⑤ propose  패자 판정 → 인기 콘텐츠 패턴으로 새 문구 생성   │
              → GitHub 이슈 (👍 승인 / 👎 거절) ─────────────►  반응·댓글 처리
   ⑥ report   HTML 대시보드                                  └► 승인된 소재 즉시 생성 (네이버 검수 대기)
```

교체 순서: **승인 → 새 소재 생성 → 네이버 검수 통과 → 그때 패자 소재 중지.**
검수 중에는 기존 소재가 계속 돌아서 노출 공백이 생기지 않습니다.

## 1분 체험 (API 키 없이)

```bash
pip install -r requirements.txt
```

```bash
python -m naver_growth --demo demo-seed
```

```bash
python -m naver_growth --demo run
```

승인 요청 목록이 출력됩니다. 하나 승인해 보세요 (데모는 기본 `dry_run` 이라 로그만 남음):

```bash
python -m naver_growth --demo approve 1
```

`data/demo_reports/latest.html` 을 브라우저로 열면 리포트가 보입니다. 데모 숫자·콘텐츠는 전부 가상입니다.

## 실제 계정에 연결

1. **키 발급**
   - 검색광고 API: searchad.naver.com → 도구 → API 사용 관리 → API 키/비밀키, 고객 ID
   - 지역검색 API: developers.naver.com → 애플리케이션 등록 → '검색' 선택
   - (선택) Claude 문구 생성: console.anthropic.com 에서 API 키
   - (선택) 유튜브 인기 영상 수집: Google Cloud 콘솔 → YouTube Data API v3 키
   - (선택) 텔레그램 승인 버튼: @BotFather 로 봇 생성 → 토큰, 봇과 대화한 방의 chat id
2. `cp .env.example .env` 후 키 입력, `cp config.example.yaml config.yaml` 후 업체 정보 입력
3. 광고그룹 ID 확인 → `config.yaml` 의 `ads.managed_adgroups` 에 기입
   ```bash
   python -m naver_growth discover
   ```
4. 한 사이클 실행 (기본값 `dry_run: true` 라 아무것도 바뀌지 않음)
   ```bash
   python -m naver_growth run
   ```
5. `deploy/crontab.example` 또는 `.github/workflows/autopilot.yml` 로 스케줄 등록

## 승인 방법

| 방법 | 설정 | 사용 |
|---|---|---|
| 텔레그램 버튼 (권장) | `.env` 에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, config 의 `approvals.telegram_user_ids` 에 승인권자 숫자 ID | 제안마다 [✅ 승인] [❌ 거절] 버튼. 5분 cron 이 응답을 처리 |
| 명령줄 | 없음 | `python -m naver_growth proposals` 로 확인 → `approve 3 5` / `reject 4` |
| Slack | `SLACK_WEBHOOK_URL` | 알림만. 버튼 응답은 공개 서버가 필요해서 지원 안 함 |

- 승인권자 ID 를 지정하면 그 사람 클릭만 인정, 다른 사람이 누르면 "권한 없음"
- 버튼을 두 번 눌러도, 두 사람이 동시에 눌러도 한 번만 처리
- 응답 없는 제안은 7일 후 만료, 광고그룹당 동시에 2건까지만 올라옴

## 권장 도입 순서

| 기간 | 설정 | 하는 일 |
|---|---|---|
| 1~2주 | `dry_run: true` (기본) | 제안 품질 확인. 승인해도 계정은 안 바뀜. `inspiration.queries` 와 `brand_notes` 다듬기 |
| 이후 | `dry_run: false` | 승인한 소재만 실제 생성·교체 |

## 업계 인기 콘텐츠 → 소재 생성

| 소스 | 인기 근거 | 필요한 것 |
|---|---|---|
| 유튜브 | 실제 조회수·하루 평균 조회수·반응률(좋아요+댓글/조회) | `YOUTUBE_API_KEY` (무료 할당량) |
| 네이버 블로그 | 여러 업계 검색어에서 상위 노출된 정도 + 최신성 | 순위 추적과 같은 네이버 키 |
| 직접 입력 | 인스타 릴스·틱톡 등 공개 API 없는 곳 | `inspiration/*.csv` (예시: `example_reels.csv.example`) |

- 네이버 블로그는 **조회수 API가 없어서** '많이 본 글'이 아니라 '검색 상위 노출 글'입니다. 리포트에 근거를 그대로 표시합니다.
- Claude 가 참고 콘텐츠들이 **왜** 반응을 얻었는지 패턴(예: 구체적 고민 해결, 상담 과정 공개, 시간 효율)을
  뽑고, 그 패턴을 `brand_notes` 에 적은 우리 업체 사실에만 적용해 새 문구를 씁니다. 제안마다 참고한 콘텐츠와 지표가 붙습니다.
- **베끼기 방지**: 참고 원문과 공백 제외 10자 이상 연속 같으면 탈락. 경쟁사명(설정값 + 순위 추적 상위 업체 자동)은
  띄어쓰기를 바꿔도 탈락. 수집한 글 안의 지시문은 따르지 않도록 '데이터'로만 넘깁니다.
- 저작권: 원문을 광고에 쓰지 않고 소구 방식만 참고하는 구조지만, 최종 문구는 승인 단계에서 사람이 확인하세요.

## 입찰은 바꾸지 않음

`ads.bidding.enabled: false` (기본) 상태에서는 세 군데에서 막힙니다:
계획 생성 단계(입찰 계획 없음) → 실행기(입찰 변경 거부) → API 클라이언트(입찰 API 호출 자체를 예외 처리).
키워드는 리포트용으로 **읽기만** 합니다.

## 안전장치

| 장치 | 내용 |
|---|---|
| 승인 필수 | 소재 생성·중지는 승인된 제안을 통해서만. 자동 실행 경로 없음 |
| `dry_run` (기본 ON) | 승인해도 로그만 남기고 계정은 안 바꿈 |
| 킬스위치 | 프로젝트 폴더에 `STOP` 파일이 있으면 승인된 것도 생성 보류 |
| 최소 활성 소재 | 패자를 꺼도 검수 통과한 소재가 2개 밑으로 안 내려감 |
| 변경 한도 | 1회 10건, 하루 30건 |
| 실행 직전 재검증 | 승인 후에도 글자 수·금지어·경쟁사명·베끼기를 한 번 더 검사 |
| 중복 방지 | 같은 제안이 두 번 생성되지 않음 |

## 플레이스 순위 측정 방식 선택

| | `official` (기본) | `browser` |
|---|---|---|
| 방법 | 네이버 오픈API 지역검색 | Playwright로 플레이스 목록 화면 읽기 |
| 범위 | **상위 5개만** (5위 밖이면 "순위권 밖") | 설정한 개수까지 (기본 50) |
| 화면 순위와 일치 | 다를 수 있음 | 가까움 |
| 안정성 | 높음 | 화면 구조 바뀌면 깨짐 → `url_template`, `ad_marker` 수정 |
| 정책 | 공식 API | 네이버 약관상 자동 수집에 해당할 수 있음. 하루 1~2회, 본인 업체 확인 용도로만 |

이 시스템은 **측정만** 합니다. 트래픽 작업·리워드 클릭 같은 순위 조작 기능은 넣지 않았습니다
(네이버 정책 위반이며 플레이스 노출 제재 사유).

## 실제 계정에서 처음 확인해야 할 것 (검증 못 한 부분)

개발 환경에서 네이버 API에 접속할 수 없어 **실제 계정으로는 테스트하지 못했습니다.**
서명 방식·엔드포인트·통계 필드는 네이버 공식 샘플(github.com/naver/searchad-apidoc)과 맞췄지만,
아래는 첫 실행 때 꼭 확인하세요.

- [ ] `discover` 가 캠페인/광고그룹을 정상 출력하는지 (서명·인증 확인)
- [ ] 소재 생성 요청 형식(`TEXT_45`, `ad.headline/description/pc.final/mobile.final`)이 현재 API와 맞는지 —
      `dry_run: false` 로 바꾼 뒤 제안 1건만 `approve` 해서 먼저 시험
- [ ] 텔레그램 승인: 봇에게 먼저 말을 걸어야 메시지를 받을 수 있음. 승인권자 숫자 ID 는 봇 대화에서 확인
- [ ] 소재 검수 상태값(`inspectStatus`)이 코드의 `PENDING / APPROVED / REJECTED` 와 같은지 (`naver_ads.ad_status`)
- [ ] 텍스트 소재 글자 수 제한(제목 15 / 설명 45로 설정)과 광고그룹당 소재 개수 제한
- [ ] 플레이스 광고(PLACE 캠페인)는 소재 구조가 달라 소재 교체 대상이 아님 — 파워링크 텍스트 소재 광고그룹만 넣으세요
- [ ] 전환(`ccnt`)은 네이버 전환추적이 설치돼 있어야 의미가 있음. 없으면 CPA 규칙이 동작하지 않고 CTR 기준으로만 판정

## 파일 구조

```
naver_growth/
  naver_ads.py    검색광고 API 클라이언트 (HMAC 서명, 재시도, 입찰 API 잠금)
  place_rank.py   플레이스 순위 측정 (official / browser) + 하락 알림
  sync.py         소재·키워드·통계 수집 (읽기 전용)
  inspiration.py  업계 인기 콘텐츠 수집 (유튜브 / 네이버 블로그 / CSV)
  copygen.py      Claude 문구 생성 + 독창성·금지어·글자 수 검증
  creative.py     패자 판정(베이지안) + 교체 제안 생성
  approvals.py    승인 큐, 생성 → 검수 → 교체 완료 (+텔레그램 옵션)
  github_ops.py   GitHub 이슈 승인(👍/👎/댓글) + 대시보드 이슈
  executor.py     실제 변경 (안전장치)
  optimizer.py    입찰 조정 (기본 꺼짐)
  report.py       HTML 대시보드
  demo.py         API 없이 돌리는 시뮬레이터
tests/            21개 테스트 (python -m pytest)
```
