-- trust-no-internet / 청사진 v0.3 스키마
--
-- 설계 원칙 4가지:
--   1. 시각은 전부 UTC ISO8601(+00:00)로 저장한다. KST 변환은 조회 계층에서만 한다.
--      (구 프로젝트는 소스별로 naive KST / tz-aware가 섞여 9시간 오차가 났다.)
--   2. 원문(body)은 raw_documents에만 두고 채점 직후 삭제한다. 단 삭제 전에
--      body_hash를 documents에 남겨 중복판정 능력을 잃지 않는다.
--   3. 집계는 tier가 아니라 media_id 단위로 한다. tier 롤업은 조회 시점 GROUP BY.
--      (매체별 차트 / 등급별 차트 / 가중치 조절이 전부 같은 테이블에서 나온다.)
--   4. 중복은 삭제하지 않고 is_canonical=0으로 강등한다. 재배포 횟수 자체가 피처다.

PRAGMA foreign_keys = ON;

-- ============================================================
-- 마스터
-- ============================================================
-- 관측 대상. 개별 종목뿐 아니라 지수·코인·채권까지 같은 테이블에 담는다.
-- code는 자산군마다 형식이 다르다: 005930 / KOSPI / BTC / KTB10Y
CREATE TABLE IF NOT EXISTS entities (
    code              TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    -- index(지수) / equity(개별주) / crypto / bond / commodity / fx
    kind              TEXT NOT NULL DEFAULT 'equity',
    market            TEXT,
    -- 1=상시수집(매일) 2=온디맨드(사용자가 열람하면) 3=보관만
    priority          INTEGER NOT NULL DEFAULT 2,
    -- 거래 캘린더. 코인은 휴장일이 없어 롤포워드 규칙이 다르다.
    --   krx / us / crypto(24x7) / none(가격 없음)
    calendar          TEXT NOT NULL DEFAULT 'krx',
    -- 수집 로케일. JSON 배열. 예: [["ko","KR","KR:ko"],["en-US","US","US:en"]]
    locales_json      TEXT,
    -- 검색 별칭 / 오탐 제외어. JSON 배열 문자열.
    aliases_json      TEXT,
    exclude_json      TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_entities_active ON entities(is_active, priority, kind);

-- 매체 레지스트리. 뉴스뿐 아니라 종토방/카페/블로그도 각각 한 행으로 등록한다
-- (media_id NULL인 행이 생기면 집계 키가 무너지기 때문).
CREATE TABLE IF NOT EXISTS media (
    media_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    domain       TEXT,
    naver_oid    TEXT,
    -- major(주요언론) / daily(일간지) / online(인터넷언론) / community / blog / unknown
    tier         TEXT NOT NULL,
    -- news / community / cafe / blog
    channel      TEXT NOT NULL,
    UNIQUE(name, channel)
);
CREATE INDEX IF NOT EXISTS idx_media_domain ON media(domain);
CREATE INDEX IF NOT EXISTS idx_media_oid    ON media(naver_oid);

-- 거래비용. 세율은 연도별로 바뀌므로 하드코딩하지 않고 발효일 기준으로 조인한다.
CREATE TABLE IF NOT EXISTS fee_schedule (
    effective_from TEXT PRIMARY KEY,
    buy_bps        REAL NOT NULL,
    sell_bps       REAL NOT NULL,
    tax_bps        REAL NOT NULL,
    slippage_bps   REAL NOT NULL
);

-- ============================================================
-- 가격
-- ============================================================
CREATE TABLE IF NOT EXISTS prices (
    code      TEXT NOT NULL,
    kst_date  TEXT NOT NULL,
    open      REAL, high REAL, low REAL, close REAL,
    volume    INTEGER,
    is_adjusted INTEGER NOT NULL DEFAULT 0,
    -- 이 계열이 어느 심볼에서 왔는가. 심볼을 바꾸면 단위가 달라지는데(UST: 금리 %
    -- -> ETF 가격 $), 섞이면 전환일에 말도 안 되는 수익률이 생기고 아무도 눈치채지
    -- 못한다. 기록해 두면 수집기가 자동으로 감지해 과거 행을 지운다.
    source    TEXT,
    PRIMARY KEY (code, kst_date)
);

-- ============================================================
-- 원문: 임시 저장소 (TTL). 채점 성공 시 행 자체를 삭제한다.
-- ============================================================
CREATE TABLE IF NOT EXISTS raw_documents (
    doc_id     INTEGER PRIMARY KEY,
    body       TEXT NOT NULL,
    fetched_utc TEXT NOT NULL
);

-- ============================================================
-- 영구 메타 (요구사항 R10: 날짜/매체/매체구분/긍부중 + 제목)
-- ============================================================
CREATE TABLE IF NOT EXISTS documents (
    doc_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT NOT NULL,
    media_id          INTEGER,
    source            TEXT NOT NULL,
    url               TEXT,

    title             TEXT NOT NULL,
    norm_title        TEXT NOT NULL,
    title_hash        TEXT NOT NULL,
    body_hash         TEXT,
    simhash           INTEGER,
    author            TEXT,

    -- 반응 지표. 소스마다 의미가 다르므로 수집기가 무엇을 넣었는지 명확히 한다:
    --   naver_board : engagement=조회수, up=추천, down=비추천
    --   4chan       : engagement=댓글수, up/down=NULL (추천 개념 없음)
    --   뉴스        : 전부 NULL
    -- 커뮤니티 표본을 "많이 읽힌/공감받은 글" 기준으로 자르는 데 쓴다.
    engagement        INTEGER,
    endorse_up        INTEGER,
    endorse_down      INTEGER,

    published_utc     TEXT,
    -- 조회/집계 최적화용 파생 컬럼. published_utc를 KST로 변환한 날짜.
    published_kst_date TEXT,
    collected_utc     TEXT NOT NULL,
    -- exact: 원문에 게시시각이 명시됨 / approx: 수집시각으로 대체(네이버 카페 API 등)
    -- approx 데이터를 08:50 컷오프 실험에 섞으면 결과가 오염되므로 반드시 분리한다.
    ts_confidence     TEXT NOT NULL DEFAULT 'exact',

    dup_group_id      TEXT,
    is_canonical      INTEGER NOT NULL DEFAULT 1,

    -- -1 부정 / 0 중립 / +1 긍정. is_relevant=0이면 label은 집계에서 제외한다.
    label             INTEGER,
    is_relevant       INTEGER,
    confidence        REAL,
    label_model       TEXT,
    prompt_version    TEXT,
    ai_reasoning      TEXT,
    labeled_at        TEXT,

    UNIQUE(code, source, url),
    FOREIGN KEY (code)     REFERENCES entities(code),
    FOREIGN KEY (media_id) REFERENCES media(media_id)
);

CREATE INDEX IF NOT EXISTS idx_doc_code_date  ON documents(code, published_kst_date);
CREATE INDEX IF NOT EXISTS idx_doc_dedup      ON documents(code, published_kst_date, title_hash);
CREATE INDEX IF NOT EXISTS idx_doc_simhash    ON documents(code, published_kst_date, simhash);
CREATE INDEX IF NOT EXISTS idx_doc_pending    ON documents(code, label) WHERE label IS NULL;
CREATE INDEX IF NOT EXISTS idx_doc_author     ON documents(code, published_kst_date, author);
-- 커뮤니티 표본 추출용: 날짜별로 반응 큰 순 정렬
CREATE INDEX IF NOT EXISTS idx_doc_engagement ON documents(code, published_kst_date, engagement DESC);

-- ============================================================
-- 사전집계 (media 단위). 가중치 변경은 이 테이블의 선형결합으로 끝난다.
-- ============================================================
-- kst_date   : 게시일(KST). Zone 1 감성 오버레이는 이걸 쓴다 — 사용자는 "이 날 나온
--              뉴스의 감성"을 보고 싶어하기 때문.
-- signal_date: 신호 귀속일. 개장 전 컷오프(08:50) 이후 글은 다음 날로 넘긴다.
--              백테스트는 반드시 이걸 쓴다 — 같은 날 장중 뉴스로 그날 시가에
--              체결하면 look-ahead가 된다.
-- 한 kst_date는 08:50을 경계로 최대 2개의 signal_date로 쪼개지므로 PK에 둘 다 넣는다.
CREATE TABLE IF NOT EXISTS sentiment_daily (
    code          TEXT NOT NULL,
    kst_date      TEXT NOT NULL,
    signal_date   TEXT NOT NULL,
    media_id      INTEGER NOT NULL,
    pos           INTEGER NOT NULL DEFAULT 0,
    neu           INTEGER NOT NULL DEFAULT 0,
    neg           INTEGER NOT NULL DEFAULT 0,
    irrelevant    INTEGER NOT NULL DEFAULT 0,
    doc_cnt       INTEGER NOT NULL DEFAULT 0,
    canonical_cnt INTEGER NOT NULL DEFAULT 0,
    spread_sum    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, kst_date, signal_date, media_id)
);
CREATE INDEX IF NOT EXISTS idx_sd_code_date   ON sentiment_daily(code, kst_date);
CREATE INDEX IF NOT EXISTS idx_sd_code_signal ON sentiment_daily(code, signal_date);

-- tier 롤업은 별도 테이블이 아니라 뷰로 둔다(이중 저장하면 정합성이 깨진다).
CREATE VIEW IF NOT EXISTS sentiment_daily_tier AS
SELECT s.code, s.kst_date, s.signal_date, m.tier, m.channel,
       SUM(s.pos) pos, SUM(s.neu) neu, SUM(s.neg) neg,
       SUM(s.irrelevant) irrelevant, SUM(s.doc_cnt) doc_cnt,
       SUM(s.canonical_cnt) canonical_cnt, SUM(s.spread_sum) spread_sum
FROM sentiment_daily s JOIN media m ON s.media_id = m.media_id
GROUP BY s.code, s.kst_date, s.signal_date, m.tier, m.channel;

-- ============================================================
-- 수집 원장 (요구사항 R12) — "어느 종목 며칠~며칠이 수집됐는가"
-- ============================================================
CREATE TABLE IF NOT EXISTS coverage (
    code        TEXT NOT NULL,
    source      TEXT NOT NULL,
    kst_date    TEXT NOT NULL,
    -- pending  : 수집 대상으로 등록만 됨
    -- partial  : 페이지 상한/오류로 중단. 그 날짜의 경계(더 오래된 글)를 확인하지 못함
    -- completed: 해당 날짜보다 오래된 글을 1건 이상 확인 = 그 날짜를 끝까지 훑었음
    -- empty    : 끝까지 훑었으나 글이 0건 (수집 안 한 날과 반드시 구분)
    -- failed   : 재시도 한도 초과
    status      TEXT NOT NULL DEFAULT 'pending',
    doc_count   INTEGER NOT NULL DEFAULT 0,
    last_cursor TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (code, source, kst_date)
);
CREATE INDEX IF NOT EXISTS idx_cov_status ON coverage(code, status, kst_date);

-- ============================================================
-- 파이프라인 실행 로그 (관측성)
-- ============================================================
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id       TEXT PRIMARY KEY,
    stage        TEXT NOT NULL,
    code         TEXT,
    started_utc  TEXT NOT NULL,
    finished_utc TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    stats_json   TEXT,
    error        TEXT
);
