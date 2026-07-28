# 인터넷을 믿지 마세요 (trust-no-internet)

뉴스·커뮤니티의 긍정/부정 신호를 지수화하고, 그 신호를 그대로 따라 매매했을 때
자산이 어떻게 변하는지를 보여주는 공개 웹 서비스.

> 이 프로젝트의 결론은 "감성으로 돈을 벌 수 있다"가 아니다.
> **비용을 반영하고 나면 계좌가 어떻게 녹는지**를 보여주는 것 자체가 콘텐츠다.

## 청사진 v0.3

### 데이터 플로우

```
[L0 수집]  bigkinds(1~2등급) · naver_news 스크래핑 · google_news_rss(폴백)
           naver_board(종토방) · naver_search_api(일간 증분 전용)
             ↓ body_hash 계산 후 raw_documents에 임시 저장
[L1 정규화] 제목 정규화 → title_hash(완전일치) → simhash(근사) → rapidfuzz 확정
             ↓ dup_group_id 부여, 대표만 is_canonical=1
[L2 채점]  대표글만 LLM/FinBERT → {positive, negative, neutral, irrelevant}
             ↓ 성공 시 raw_documents 행 삭제 (본문 파기)
[L3 영구]  documents — 날짜/매체/등급/라벨/제목/확산도
             ↓
[L4 집계]  sentiment_daily (code × date × media_id)
             ↓  tier 롤업은 sentiment_daily_tier 뷰로
[L5 서빙]  FastAPI — 가중치는 조회 시점 파라미터
[L6 UI]    일/주/월 가격차트 + 매체별·등급별 감성 + 자산곡선
```

### 확정된 설계 결정 9가지

| # | 결정 | 근거 |
|---|---|---|
| 1 | 시각은 **UTC로 저장**, KST 변환은 조회 계층 | 구 프로젝트는 소스별 naive/aware 혼재로 +9h 오차 |
| 2 | 본문은 `raw_documents`에 임시 저장, **파기 전 `body_hash`** | 파기 후에는 중복판정 능력을 영구히 잃음 |
| 3 | 집계는 **media 단위**, tier 롤업은 뷰 | 매체별 차트 요구 충족. tier 대비 행 수 21배(실측) |
| 4 | 중복은 삭제하지 않고 **`is_canonical=0` 강등** | 재배포 횟수(확산도)가 독립 피처 |
| 5 | 라벨에 **`irrelevant` 4번째 클래스** | "카카오톡 잡담"이 neutral로 흡수되어 지수를 0으로 끌어내림 |
| 6 | **`prompt_version`/`label_model` 기록** | 프롬프트 교체 시 과거/신규 라벨 혼재 → 인공적 regime shift |
| 7 | 신호 윈도우 `[D-1 15:30, D 08:50]` → **D일 시가 체결** | "익일 시가"로 미루면 하루를 이중으로 낭비 |
| 8 | 거래비용은 **`fee_schedule` 테이블** | 거래세율은 연도별 변경. 하드코딩 금지 |
| 9 | `coverage`에 **`partial`/`empty` 상태 분리** | 부분 수집을 완료로 찍으면 그 날짜가 영구히 반쪽으로 고정 |

### 감성지수 산식

```
극성지수 P(t) = Σ w·(pos − neg) / Σ w·(pos + neg)     # 중립 배제 (희석 방지)
관여도   E(t) = z( Σ w·doc_cnt )                       # 관심 폭증 자체가 신호
확산도   D(t) = Σ spread_sum                           # 재배포 횟수 = 파급력
합성     S(t) = P(t) · tanh(E(t))
보정     S_adj = S · n / (n + SHRINKAGE_K)             # 표본 적은 날 노이즈 억제
```

`w`는 `media.tier` → `DEFAULT_TIER_WEIGHTS` 매핑이며 **조회 시점 파라미터**다.
슬라이더를 움직여도 재집계하지 않는다.

## 현재 상태

레거시 이관 완료(`scripts/migrate_legacy.py`).

| 항목 | 값 |
|---|---|
| documents | 12,986 |
| 라벨 보유 | 4,864 |
| 중복 강등 | 1,296 (1,020 그룹) |
| 매체 등록 | 489 |
| 가격 | 2,188행 / 3종목 |
| coverage | 102일 (partial 66 / failed 36) |

### 알려진 부채

- **`tier='unknown'` 6,867건 (57%)** — 매체 등급 매핑 미완. 상위 20개 매체만
  매핑해도 커버리지 80%가 해결된다.
- **재채점 대기 8,122건** — 005930 전량(레거시 연속점수, 스케일 상이) + 035720 미채점분.
- **종토방 28,286건 재수집 필요** — 아래 사고 기록 참조. `coverage`에 `failed`로 등록됨.

## 사고 기록: 인코딩 파손 (재발 방지)

`finance.naver.com`은 페이지마다 인코딩이 다르다.

```
/item/news_news.naver  → charset=EUC-KR
/item/board.naver      → charset=UTF-8    ← 종목토론방
```

구 코드가 두 페이지 모두에 `encoding="euc-kr"`를 하드코딩해서 종토방 제목
**28,114/28,286건(99.4%)** 이 U+FFFD로 파손됐다. 복원 불가. 그 상태로 LLM 채점까지
나가 **99.1%가 neutral**로 찍혔고(대조군 naver_news 36.9%), 그 결과 "커뮤니티
인간지표" 차트 전체가 노이즈였다.

방어 장치 2개를 넣었다 — `http_utils._resolve_encoding()`(헤더 charset 우선)과
`assert_decoded()`(U+FFFD 비율 초과 시 예외). **인코딩을 호출부에서 지정하지 말 것.**

## 웹 서비스 실행

```powershell
python -m scripts.serve            # http://127.0.0.1:8000
python -m scripts.serve --reload   # 개발 모드
```

기본 바인딩은 `127.0.0.1`이다. 외부 공개는 `--host 0.0.0.0`을 명시할 때만 일어난다.

### 화면 구성

| Zone | 내용 |
|---|---|
| Header | 종목 · 일별/주간/월간 · 기간(데이터 구간/3M/6M/1Y/전체) · 데이터 보유 구간 |
| Zone 1 | 주가 + 감성지수 오버레이. 종합 / 등급별 / 매체별(칩 선택 최대 6개). 데이터 없는 구간은 회색 음영 |
| Zone 2 | ① 정방향·역배팅 ② 보유수량내·무한공매도 ③ **비용 반영(기본 ON)** ④ 등급별 가중치 6개 |
| Zone 3 | **3선 분해** — 순손익 / 주식 평가액 / 현금 잔고 + Buy&Hold. 차트 위에 실질 수익률 대표 숫자 |

### 백테스트 계산

```
raw(D)   = Σ_tier w_tier × (pos − neg)        # 중립은 애초에 들어가지 않는다
desired  = ±raw                                # 역배팅이면 부호 반전
delta    = max(desired, −position) | desired   # 보유수량내 | 무한공매도
체결      = open(D)                            # 신호는 D−1 15:30 ~ D 08:50 확정
순손익    = cash + position × close(D)         # 0원 시작, 모든 모드에서 정의됨
```

**시드머니가 없으므로 수익률의 분모는 `최대 소요자본`이다.** 누적 투입금을 분모로 쓰면
매도가 쌓여 음수가 될 때 손실이 +수익률로 표시되고, 0을 지날 때 발산한다.
최대 소요자본 = max(현금 잔고 최저점의 절댓값, 공매도 익스포저 최대값).

### 검증된 동작 (035720 · 22거래일)

| 방향 | 포지션 | 비용 | 수익률 | 순손익 |
|---|---|:---:|---:|---:|
| 정방향 | 보유수량내 | ON | −0.83% | −17,496원 |
| 정방향 | 무한공매도 | OFF | **+0.62%** | +16,550원 |
| 정방향 | 무한공매도 | **ON** | **−0.04%** | −1,150원 |
| 역배팅 | 보유수량내 | ON | +0.77% | +34,483원 |

3행·4행이 이 사이트의 논지다 — **비용 토글 하나로 이익이 손실로 뒤집힌다.**

## 수집기 실행 (분석/서빙과 분리된 독립 프로세스)

```powershell
python -m scripts.collect status                        # 원장/실행이력 조회
python -m scripts.collect master                        # KOSPI 종목마스터
python -m scripts.collect prices 000660 --years 3
python -m scripts.collect news   000660 --days 30
python -m scripts.collect board  000660 --days 30 --max-pages 3000
```

장시간 백필은 백그라운드로 떼어놓는다. 진행상황은 로그 파일과 `pipeline_runs`
테이블 양쪽에 남으므로, 콘솔을 닫아도 `status`로 확인할 수 있다.

```powershell
# 백그라운드 시작
Start-Process python -ArgumentList "-m","scripts.collect","board","000660","--days","180","--max-pages","20000","--log","logs\board_000660.log" -WorkingDirectory "D:\dev\trust-no-internet" -WindowStyle Hidden

# 진행 확인
Get-Content logs\board_000660.log -Tail 20 -Wait
python -m scripts.collect status
```

중단해도 안전하다. 페이지 단위로 커밋하고 `coverage`에 `partial`+`last_cursor`를
남기므로, 같은 명령을 다시 실행하면 이미 받은 URL은 건너뛴다.

## 데이터 소스별 제약 (구 프로젝트에서 실제로 겪은 것)

- **네이버 검색 오픈API는 백필에 쓸 수 없다.** 쿼리당 1000건 상한(`start<=1000`),
  날짜 범위 지정 불가(`sort=date`만). `days_back`을 키워도 같은 최신 1000건이
  돌아온다. 구 코드가 이걸로 1년치 루프를 돌려 신규 0건을 얻었다. **일간 증분 전용.**
- **`cafearticle` 검색엔 게시일 필드가 없다.** `ts_confidence='approx'`로 격리한다.
- **디시인사이드 주식갤러리(`stock_new1`)는 2017-01 이후 사실상 죽은 갤러리다.**
  커뮤니티 소스로 기대하지 말 것.
- **대형주는 스크래핑 페이지네이션이 실질 상한이 된다.** 종토방 하루 게시량이
  1만 건을 넘어(실측 005930 16,909건/일) `max_pages`가 곧 커버리지 한계다.
  이 경우 `coverage`가 `partial`로 남는 것이 정상이며, 상한을 올려 재실행해야 한다.
- **가격은 pykrx**(비공식 KRX 스크래핑). 키움 OpenAPI+는 32비트 파이썬 + 실계좌
  로그인이 매번 필요해 자동화에 부적합.

## 로드맵

- [x] 스키마 v0.3 + 레거시 이관
- [x] 인코딩 버그 수정 + 회귀 방어
- [x] 수집기 이식 (종토방 `author` 파싱, 검색API 백필 금지 명시)
- [x] 신호 귀속일(`signal_date`) 분리 — 개장 전 컷오프
- [x] 백테스트 엔진 (4모드 × 비용 토글 + `fee_schedule` + Buy&Hold)
- [x] FastAPI 서빙 + 일/주/월 차트 + 3선 분해
- [ ] **매체 등급 매핑 상위 40개** ← 현재 최대 부채(unknown 57%)
- [ ] 채점기: `irrelevant` 라벨 + 반어법 few-shot + 배치 실패 반분할 재시도
- [ ] 종토방 재수집 (coverage `failed` 36일)
- [ ] 근사중복 판정 (blocking → simhash → rapidfuzz)
- [ ] 백필: 빅카인즈 API 신청 / Google News RSS 폴백 병행

### 코스피200 확장 시점으로 미룬 것

MinHash/TF-IDF 근사중복, 은어 사전, 로컬 모델 파인튜닝, PostgreSQL 전환
(트리거: 종목 30개 초과 **또는** `sentiment_daily` 300만 행 초과).

### 확인 필요 항목

- 빅카인즈 Open API 신청 절차·호출 상한·수록 매체 범위·라이선스
- `pykrx` 반환 가격의 수정주가 여부 (액면분할 미반영 시 백테스트 왜곡)
- 코스피200 과거 편입 종목 이력 (생존 편향)
- 실제 거래 수수료율 (현재 `fee_schedule`은 임시 기본값)
