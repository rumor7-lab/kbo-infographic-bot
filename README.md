# KBO 인포그래픽 자동 발행 봇

KBO 데이터를 수집해 인포그래픽을 만들고 인스타그램에 하루 3번 자동 발행합니다.

```
수집(KBO 공식) → 검증 → 유형 자동선택(A/B/C) → 렌더(PNG/MP4) → 공개URL → 발행
```

**현재 상태**: 코드 작성 완료, 실행 검증 미완료. `scripts/selftest.py` 를 가장 먼저 돌려주세요.

---

## 확정된 운영 설계

| 항목 | 값 |
|---|---|
| 대상 | KBO 전체 (특정 팀 편중 없음) |
| 브랜드 | 신규 구축 — `오늘의 KBO` 확정 (`config/brand.yml`) |
| 발행 | 하루 3건 · 08:00 / 12:30 / 22:30 (KST) |
| 릴스 | 포함 — 점심(12:30) 슬롯을 릴스로 발행 |
| 카드 유형 | A(표) · B(차트) · C(사진 히어로) 전부 사용, 데이터 형태로 **자동 선택** |
| 선수 사진 | 뉴스 사진 자동 크롤링 + 5단계 품질 게이트 |
| 로고/사진 | 사용 (기존 유튜브 채널 운영 방식과 동일) |

### 3슬롯 구성

| 슬롯 | 시각 | 콘텐츠 | 포맷 | 설계 이유 |
|---|---|---|---|---|
| morning | 08:00 | 어제 경기 결과 + 팀 순위 + 어제의 주인공 | 캐러셀 3장 | 전날 기록 정정까지 반영된 확정 데이터. 신뢰도 최상 |
| noon | 12:30 | 요일 로테이션 주제 | **릴스** | 점심 스크롤 타임 + 릴스로 신규 도달 확보 |
| night | 22:30 | 오늘 경기 결과 + 오늘의 기록 | 캐러셀 2장 | 경기 종료 직후. 미확정이라 `잠정` 배지 자동 부착 |

점심 슬롯 요일 로테이션: 월 가을야구 시나리오 · 화 타격순위 · 수 투수순위 · 목 득점권타율 WORST · 금 주말 매치업 · 토 기록 달성 임박 · 일 주간 결산

---

## 유형 자동 선택이 어떻게 동작하는가

아웃라인(브랜드 프레임)은 `base.html.j2` 가 **단독으로** 소유합니다. 배지 위치, 타이틀 블록, 하단 출처 바는 유형과 무관하게 고정입니다. 유형별 템플릿은 본문 슬롯만 채웁니다.

```
                    ┌─────────────────────────┐
                    │  KICKER          [배지] │  ← base.html.j2 (고정)
                    │  타이틀                 │
                    ├─────────────────────────┤
                    │                         │
                    │   ← 여기만 유형별로     │  ← table / chart / hero
                    │      바뀜               │
                    │                         │
                    ├─────────────────────────┤
                    │  출처 · 기준일 · 핸들   │  ← base.html.j2 (고정)
                    └─────────────────────────┘
```

선택 규칙 (`src/render/layout_engine.py`):

| 조건 | 선택 | 예시 카드 |
|---|---|---|
| 카드 정의에 `layout` 명시 | 그대로 사용 | `fa_list` → table |
| 주인공 1명 + 사진 확보 | **C 히어로** | 어제의 주인공 |
| 단일 숫자 지표 + 3~12행 | **B 차트** | 팀 순위(승률) |
| 그 외 (다열 데이터) | **A 표** | 경기 결과, 타격 TOP 10 |
| hero 지정인데 사진 실패 | `fallback_layout` 으로 **자동 강등** | → table 또는 chart |

마지막 줄이 중요합니다. 사진 크롤링은 실패할 수 있는 작업이라, 실패하면 발행이 멈추는 대신 조용히 다른 유형으로 내려갑니다.

행 수가 캔버스를 넘치면 자동으로 잘라내고 "상위 14건만 표시(총 40건)" 각주를 붙입니다. 셀 높이와 바 굵기도 행 수에 맞춰 자동 조절되므로 5행이든 14행이든 프레임이 깨지지 않습니다.

---

## 선수 사진 품질 게이트

뉴스 사진 자동 크롤링은 최신성이 최고인 대신 무인 운영에서 사고가 잦습니다. 그래서 수집보다 **거르기**에 코드를 더 썼습니다 (`src/collect/player_photo.py`).

| 단계 | 기준 | 걸러내는 것 |
|---|---|---|
| 1. 해상도 | 900×600 이상, 40KB 이상 | 썸네일 |
| 2. 얼굴 검출 | 정확히 1명, 면적비 4.5% 이상 | 단체샷, 그래픽, 로고, 경기장 전경 |
| 3. 인물 위치 | 얼굴 중심이 상단 55% 안 | 히어로 레이아웃에서 얼굴이 가려지는 사진 |
| 4. 화질 | 라플라시안 분산 60 이상 | 블러, 저화질 |
| 5. 캐시 | 통과 사진은 21일 재사용 | 매번 새로 뽑을 때의 오인 누적 |

추가로 얼굴 중심 좌표에서 `background-position` 을 계산해 **얼굴이 잘리지 않게** 자동 정렬합니다. 히어로 카드의 가장 흔한 실패 모드가 이겁니다.

새로 채택된 사진은 `data/photo_review.json` 에 쌓입니다. 무인 운영이라도 주 1회는 눈으로 넘겨보세요. 오인 사진을 찾으면:

```bash
python -c "from src.collect.player_photo import blocklist_add; blocklist_add('선수명')"
```

---

## 데이터 정확성 방어

발행 전 자동 검증 (`src/validate/rules.py`). 실패 시 **발행 스킵 + 로그**. 절대 대충 발행하지 않습니다.

| 룰 | 막는 사고 |
|---|---|
| 동일 라벨 3회 이상 반복 | 업로드해주신 '최고 구속 TOP 10'에서 전준표가 9번 나온 케이스 — 원본은 투구 단위 집계라 정상이지만 인포그래픽으로는 고장처럼 보임 |
| 승 + 무 + 패 ≠ 경기수 | 파싱 열 밀림 |
| 승률 0~1 범위 밖 | 셀 오독 |
| 누적 스탯 역행 (전일 대비 홈런 감소) | 잘못된 페이지/시즌 파싱 |
| 팀 수 ≠ 10 | 표 일부 누락 |
| 빈 셀 25% 초과 | 경고만 |

추가 방어:

- **기록 정정 감지** — KBO '기록 정정 현황' 페이지를 확인해 최근 정정이 있으면 실행 로그에 경고를 남깁니다
- **아침 슬롯을 08:00 으로 둔 이유** — 경기 직후가 아니라 정정 반영 후 데이터를 씁니다
- **밤 슬롯 `잠정` 배지** — 미확정 데이터임을 카드에 명시
- **고위험 카드 승인제** — FA 명단, 연봉처럼 계약 정보가 섞인 카드(`risk: high`)는 자동 발행하지 않고 `data/approval/` 로 보냅니다. 옵트아웃·연장옵션은 구단이 공개하지 않는 정보라 자동화 대상이 아닙니다

---

## 설치

### 1. 의존성

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

**ffmpeg** (릴스 인코딩용):
- Windows: `winget install Gyan.FFmpeg` 또는 `choco install ffmpeg`
- macOS: `brew install ffmpeg`

**폰트** — Pretendard 를 시스템에 설치하세요. 템플릿이 CDN 도 함께 참조하지만, 로컬 설치본이 있으면 렌더가 빠르고 안정적입니다. (https://github.com/orioncactus/pretendard/releases)

### 2. 셀프테스트 — 반드시 이 순서로

```bash
# 1단계: 설정 · 레이아웃 엔진 · 검증 룰 · 템플릿 (네트워크 불필요)
python scripts/selftest.py

# 2단계: 실제 PNG 3장 생성 → out/selftest/ 를 눈으로 확인
python scripts/selftest.py --render

# 3단계: KBO 사이트 실제 파싱 확인 (네트워크 필요)
python scripts/selftest.py --live
```

셀렉터는 2026-07-30 실측으로 검증됐습니다(아래 '셀렉터 검증 결과' 참조). 이후 사이트가 개편되면 `src/collect/kbo_official.py` 의 `SELECTORS` **딕셔너리 한 곳만** 고치면 됩니다.

3단계가 통과하면 콘솔에 실제 데이터가 찍힙니다. 예:

```
  [OK] 팀 순위 파싱
       1위 삼성 0.617 (58승 2무 36패)
  [OK] 타격 순위 파싱
       1위 최원준 타율 0.354
  [OK] 어제 경기 파싱
       키움 vs LG  18 : 11  → 키움
```

### 3. 구단 로고

`assets/logos/` 에 팀 키와 같은 이름으로 PNG 를 넣으세요 (`삼성.png`, `LG.png` …). 없으면 팀 컬러 스와치로 대체되므로 발행은 멈추지 않습니다.

### 4. 인스타그램 연결

계정은 이미 비즈니스 계정이니, 필요한 건 ID 와 토큰뿐입니다.

인증 방식은 **Instagram API with Instagram Login** (FB 페이지 연결 불필요, 인스타
프로페셔널 계정에 직접 로그인). 예전 "Facebook 로그인이 포함된 API"(graph.facebook.com)
방식과 토큰이 호환되지 않으니 혼동하지 말 것.

1. Meta 개발자 콘솔 → 앱 생성 → 제품에 Instagram 추가
2. 앱 설정 → Instagram → "Instagram 로그인이 포함된 API 설정" → 계정 추가
   → 여기서 연결된 계정 옆에 IG User ID 가 바로 보임, "토큰 생성" 버튼으로 토큰 발급
3. `.env.example` 을 `.env` 로 복사해 값 채우기 (로컬용)
4. GitHub Actions 용: 리포 Settings → Secrets → Actions 에 등록

| 시크릿 | 용도 |
|---|---|
| `IG_USER_ID` | 인스타 프로페셔널 계정의 Instagram-scoped ID |
| `IG_ACCESS_TOKEN` | 장기 액세스 토큰(60일, 자동 갱신 필요) |
| `IG_APP_SECRET` | 토큰 자동 갱신용 — 앱 설정 → Instagram → API 설정 페이지의 "Instagram 앱 시크릿 코드" |

`GITHUB_TOKEN` 은 Actions 가 자동 제공하므로 등록 불필요합니다.

### 5. 로컬 실행

```bash
python -m src.pipeline morning --dry-run    # 렌더까지만, 발행 안 함
python -m src.pipeline noon --dry-run
python -m src.pipeline night                # 실제 발행
```

`--dry-run` 산출물은 `out/{타임스탬프}/{슬롯}/` 에, 실행 로그는 `data/runs/` 에 남습니다.

---

## 미디어 호스팅

Instagram Graph API 는 **공개 접근 가능한 URL** 만 받습니다. 로컬 PNG 를 직접 못 올립니다.

기본값은 **GitHub Release** 를 공개 CDN 처럼 쓰는 방식입니다. 추가 서비스도 비용도 없고, 일자별 태그(`media-20260730`)로 정리됩니다. 트래픽이 커지면 `MEDIA_BACKEND=r2` 로 전환하세요 (`src/publish/hosting.py`).

---

## 프로젝트 구조

```
kbo-infographic-bot/
├─ config/
│  ├─ brand.yml            디자인 시스템 — 컬러·폰트·고정 프레임·10구단 컬러
│  └─ cards.yml            카드 카탈로그 + 3슬롯 스케줄 + 검증 룰
├─ src/
│  ├─ collect/
│  │  ├─ kbo_official.py   KBO 공식 파서 (셀렉터 한 곳에 집중)
│  │  └─ player_photo.py   뉴스 사진 크롤러 + 5단계 품질 게이트
│  ├─ validate/rules.py    발행 전 검증
│  ├─ render/
│  │  ├─ layout_engine.py  유형 A/B/C 자동 선택
│  │  ├─ renderer.py       Jinja + Playwright (PNG / 릴스 MP4)
│  │  └─ templates/
│  │     ├─ base.html.j2   ★ 고정 아웃라인 — 브랜드 일관성의 단일 소유자
│  │     ├─ table.html.j2  유형 A
│  │     ├─ chart.html.j2  유형 B
│  │     └─ hero.html.j2   유형 C
│  ├─ publish/
│  │  ├─ instagram.py      Graph API (컨테이너 → 폴링 → 발행)
│  │  ├─ hosting.py        로컬 파일 → 공개 URL
│  │  └─ captions.py       캡션 + 해시태그
│  └─ pipeline.py          오케스트레이터
├─ scripts/selftest.py     3단계 셀프테스트
├─ data/
│  ├─ snapshots/           일자별 원본 JSON (git 커밋 = 무료 시계열 DB)
│  ├─ manual/fa_list.yml   수동 관리 데이터
│  ├─ approval/            risk:high 카드 승인 큐
│  ├─ photo_review.json    새로 채택된 사진 리뷰 큐
│  └─ runs/                실행 로그
├─ assets/{logos,photos}/
└─ .github/workflows/
   ├─ publish.yml          3슬롯 cron
   └─ token-refresh.yml    주 1회 토큰 갱신 + 만료 임박 시 이슈 생성
```

**데이터 스냅샷을 git 에 커밋**하는 구조입니다. 별도 DB 없이 시계열 축적, 전일 대비 검증, 오류 추적, 롤백이 전부 해결됩니다.

---

## 릴스 생성 방식

Playwright 비디오 녹화를 쓰지 않습니다. Web Animations API 로 CSS 애니메이션의 `currentTime` 을 프레임 단위로 직접 세팅하고 스크린샷을 찍어 PNG 시퀀스를 만든 뒤 ffmpeg 로 인코딩합니다. 녹화 방식보다 프레임 타이밍이 정확하고 화질 손실이 없습니다.

인포그래픽과 **같은 템플릿을 재사용**합니다 (`motion=true` 플래그로 애니메이션 CSS만 추가 주입). 릴스용 자산을 따로 만들 필요가 없습니다.

기본 8초 / 30fps / 1080×1920. `config/brand.yml` 의 `canvas.reels` 에서 조정합니다.

---

## 알려진 미완성 항목

정직하게 남겨둡니다.

| 항목 | 상태 | 비고 |
|---|---|---|
| `SELECTORS` | **검증 완료** (2026-07-30) | 실제 페이지 DOM 대조 — 아래 표 참조 |
| 파이썬 실행 | 미완료 | 작성 환경에 샌드박스가 없었음. `selftest.py` 로 확인 필요 |
| `compute.playoff_odds` | **구현 완료** | 잔여 경기 몬테카를로 시뮬레이션 (`src/compute/playoff_odds.py`). 새 크롤링 없이 `standings()` 결과만 사용 |
| `frustration_index` 카드 | **RISP로 교체 완료** | 원래 '팀별 잔루 WORST'로 기획했으나 KBO 공식에 팀 시즌 누적 잔루 통계가 없음(타자/투수/주루 페이지 실측 확인). 잔루 랭킹은 STATIZ 전용 데이터라 크롤링 금지 정책상 대신 **득점권 타율(RISP)** 로 대체(`kbo.team_risp_worst()`) |
| `top_performer` | 근사 구현 | 현재는 시즌 타격 1위 기준. 게임센터 박스스코어 파싱으로 교체하면 정확해짐 |
| 쇼츠 · 스레드 배포 | 미구현 | 발행 어댑터가 추상화돼 있어 `src/publish/` 에 파일 추가로 확장 가능 |

### 셀렉터 검증 결과 (2026-07-30 실측)

| 페이지 | 셀렉터 | 확인된 구조 |
|---|---|---|
| 팀 순위 | `#…udpRecord table.tData` | 10행 · `순위 팀명 경기 승 패 무 승률 게임차 최근10경기 연속 홈 방문` — **승-패-무 순서** |
| 타자 기록 | `#…udpContent table.tData01` | 30행 · `순위 선수명 팀명 AVG G PA AB R H 2B 3B HR TB RBI SAC SF` |
| 투수 기록 | `#…udpContent table.tData01` | 20행 · `순위 선수명 팀명 ERA G W L SV HLD WPCT IP H HR BB HBP SO R ER WHIP` |
| 팀 타자 기록(2p) | `#…udpContent table` (`Record/Team/Hitter/Basic2.aspx`) | 10행 · `순위 팀명 AVG BB IBB HBP SO GDP SLG OBP OPS MH RISP PH-BA` |
| 스코어보드 | `.smsScore` + `table.tScore` | 경기당 1블록 · 라인스코어 `TEAM 1~12 R H E B`, 1행=원정/2행=홈 |
| 날짜 이동 | `input[id$='hfSearchDate']` + `#…btnPreDate` | hidden 값이 `YYYYMMDD`. '전일' 버튼 클릭 시 postback 으로 하루씩 이동 |

### 가을야구 시나리오 계산 방식

`src/compute/playoff_odds.py` — 각 팀의 잔여 경기(144경기 기준)를 '현재 승률과 동일한 확률의 독립 시행'으로 가정해 10만 회(선택 가능) 몬테카를로 시뮬레이션하고, 매 시뮬레이션마다 승수 상위 5개 팀(포스트시즌 진출권)에 든 비율을 팀별 확률로 집계합니다. 실제 잔여 상대 전력·홈어웨이는 반영하지 않는 단순화 모델이라 카드 각주에 "자체 산출"을 명시합니다. 새로운 크롤링 없이 이미 수집한 순위표 데이터만 사용합니다.

주의 지점 두 가지를 실측에서 발견해 코드에 반영했습니다.

1. **팀 순위 페이지에 표가 2개** 있습니다(순위표 + 상대전적표). `udpRecord` 로 한정하지 않으면 엉뚱한 표를 잡습니다.
2. **선수 기록 컬럼이 영문 약어**입니다(`AVG`, `HR`, `RBI`…). 카드에 그대로 쓸 수 없어 `HITTER_LABELS` / `PITCHER_LABELS` 로 한글 매핑합니다.

또 `Record/Expectation/WeekList.aspx`('예상 달성 기록')는 데이터 표가 아니라 **게시판 목록**이었습니다. 그래서 이 페이지 의존을 버리고, 선수 기록에서 라운드 넘버(홈런 30, 타점 100, 탈삼진 200 …)까지 남은 개수를 **직접 계산**하도록 바꿨습니다. 의존성이 줄고 검증도 쉬워집니다.

---

## 다음 단계 권장 순서

1. `selftest.py` 3단계 전부 통과시키기 — 여기가 전부입니다
2. `out/selftest/` PNG 를 보고 디자인 조정 (`config/brand.yml` + 템플릿 CSS)
3. ~~브랜드명 확정~~ — "오늘의 KBO"로 확정, `brand.yml` 반영 완료. 실제 인스타 `@handle`만 가입 가능 여부 확인 후 맞춰서 수정
4. **2주간 수동 업로드 운영** — 어떤 카드가 반응이 오는지 측정
5. 반응 나온 카드만 남기고 `cards.yml` 정리 → Actions 활성화
6. 무인 7일 연속 성공 확인 후 쇼츠·스레드 배포 추가

4번을 건너뛰지 마세요. 자동화의 가장 큰 낭비는 아무도 안 보는 카드를 완벽하게 자동 발행하는 것입니다. 지금은 7월 말, 후반기 순위 싸움이 가장 뜨거운 시점이라 수동 운영만으로도 트래픽을 잡을 수 있습니다.

---

## 운영 주의사항

- **크롤링 예의** — 요청 간 최소 2초 간격, 하루 3회 슬롯에서 필요한 페이지만. 병렬 크롤링 금지
- **STATIZ 는 크롤링하지 않습니다** — 공지에서 IP 차단을 명시. 코드에도 포함하지 않았습니다
- **수치만 가져와 자체 디자인으로 재구성** — KBO 사이트의 표·이미지·기사 원본은 쓰지 않습니다
- **토큰 만료** — 파이프라인이 조용히 죽는 1순위 원인. `token-refresh.yml` 이 주 1회 갱신하고 만료 15일 전에 이슈를 자동 생성합니다
- **API 버전** — Meta 는 분기마다 버전업하고 각 버전을 약 2년 지원합니다. 연 1회 `IG_API_VERSION` 갱신
- **로고·선수 사진** — 현재 방식대로 진행합니다. 광고·협찬·굿즈로 수익 구조가 생기는 시점에는 한 번 재검토해두면 좋습니다
