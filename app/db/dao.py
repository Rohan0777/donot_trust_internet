"""문서 저장 · 중복판정 · 수집원장 갱신.

수집기(collectors)는 파싱만 하고, 정규화/해시/중복/원장은 전부 여기를 통한다.
파이프라인 순서가 중요하다 — body_hash는 본문을 파기하기 전에만 계산할 수 있다.
"""
import uuid
from datetime import datetime

from app.config import KST, NON_OPINION_CHANNELS, UTC, WINDOW_END_HHMM

CUTOFF_HH, CUTOFF_MM = WINDOW_END_HHMM
from app.text.normalize import body_hash, normalize_title, simhash64, title_hash


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def to_utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def kst_date_of(utc_iso: str | None) -> str | None:
    if not utc_iso:
        return None
    return datetime.fromisoformat(utc_iso).astimezone(KST).strftime("%Y-%m-%d")


def get_stock(conn, code: str):
    row = conn.execute("SELECT code, name, aliases_json FROM entities WHERE code = ?", (code,)).fetchone()
    return row


def stock_aliases(conn, code: str) -> tuple[str, ...]:
    row = get_stock(conn, code)
    if not row or not row["aliases_json"]:
        return ()
    import json

    return tuple(json.loads(row["aliases_json"]))


def resolve_media_id(conn, name: str, channel: str, tier: str = "unknown") -> int:
    row = conn.execute(
        "SELECT media_id FROM media WHERE name = ? AND channel = ?", (name, channel)
    ).fetchone()
    if row:
        return row["media_id"]
    domain = name if "." in name else None
    cur = conn.execute(
        "INSERT INTO media(name, domain, tier, channel) VALUES (?, ?, ?, ?)",
        (name, domain, tier, channel),
    )
    return cur.lastrowid


def existing_urls(conn, code: str, source: str) -> set[str]:
    return {
        r["url"]
        for r in conn.execute(
            "SELECT url FROM documents WHERE code = ? AND source = ?", (code, source)
        )
    }


def insert_documents(conn, code: str, source: str, items: list[dict], aliases: tuple[str, ...] = ()) -> int:
    """items: {url, title, author?, published_utc?, collected_utc, media_id, body?, ts_confidence?}

    본문(body)이 있으면 raw_documents에 임시 저장하고, 파기 전에 body_hash를 남긴다.
    """
    saved = 0
    for it in items:
        title = (it.get("title") or "").strip()
        if not title or "�" in title:
            continue
        norm = normalize_title(title, aliases)
        pub = it.get("published_utc")
        collected = it.get("collected_utc") or now_utc()
        cur = conn.execute(
            "INSERT OR IGNORE INTO documents"
            "(code, media_id, source, url, title, norm_title, title_hash, body_hash, simhash, author,"
            " engagement, endorse_up, endorse_down,"
            " published_utc, published_kst_date, collected_utc, ts_confidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                code, it["media_id"], source, it.get("url"), title, norm,
                title_hash(norm), body_hash(it.get("body")), simhash64(norm), it.get("author"),
                it.get("engagement"), it.get("endorse_up"), it.get("endorse_down"),
                pub, kst_date_of(pub or collected), collected,
                it.get("ts_confidence", "exact" if pub else "approx"),
            ),
        )
        if cur.rowcount == 0:
            continue
        saved += 1
        if it.get("body"):
            conn.execute(
                "INSERT OR REPLACE INTO raw_documents(doc_id, body, fetched_utc) VALUES (?,?,?)",
                (cur.lastrowid, it["body"], collected),
            )
    return saved


def mark_duplicates(conn, code: str, kst_dates: set[str]) -> tuple[int, int]:
    """완전일치(정규화 제목) 중복을 그룹으로 묶고 대표 외에는 is_canonical=0으로 강등한다.
    삭제하지 않는 이유: 재배포 횟수(확산도) 자체가 피처다.

    근사중복(simhash/rapidfuzz)은 별도 패스에서 처리한다 — 여기는 1단계만.
    """
    groups = demoted = 0
    for d in sorted(kst_dates):
        rows = conn.execute(
            "SELECT title_hash, COUNT(*) n, MIN(doc_id) keep FROM documents "
            "WHERE code = ? AND published_kst_date = ? GROUP BY title_hash HAVING n > 1",
            (code, d),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE documents SET dup_group_id = ?, "
                "is_canonical = CASE WHEN doc_id = ? THEN 1 ELSE 0 END "
                "WHERE code = ? AND published_kst_date = ? AND title_hash = ?",
                (r["title_hash"][:12], r["keep"], code, d, r["title_hash"]),
            )
            groups += 1
            demoted += r["n"] - 1
    return groups, demoted


# 같은 날짜를 여러 질의로 훑을 때의 상태 병합 순위. 큰 쪽이 이긴다.
# empty < completed : 한 질의가 0건이어도 다른 질의가 건졌으면 그 날은 수집된 것이다.
# completed < partial : 한 질의라도 상한에 걸렸으면 그 날의 완전성은 보장할 수 없다.
# partial < failed : 아예 못 받아온 질의가 있으면 그게 가장 나쁘다.
_COVERAGE_RANK = {"empty": 0, "pending": 0, "completed": 1, "partial": 2, "failed": 3}


def upsert_coverage(conn, code: str, source: str, kst_date: str, status: str,
                    doc_count: int = 0, last_cursor: str | None = None, error: str | None = None,
                    run_started: str | None = None):
    """status 의미는 schema.sql 참조. 부분 수집을 completed로 찍으면 그 날짜가
    영구히 반쪽으로 고정되므로, 경계(더 오래된 글) 확인 전에는 partial을 유지한다.

    [run_started 를 주면 같은 실행 안에서는 나쁜 쪽이 이긴다]
    한 엔티티를 여러 질의로 돌릴 때(대형주는 질의를 늘리는 것이 응답 상한을 우회하는
    유일한 수단이다) 마지막 질의가 completed 로 덮어쓰면 앞선 질의가 상한에 걸렸다는
    사실이 사라진다. doc_count 도 마지막 질의 것만 남아 그 날의 수집량을 과소보고한다.
    같은 실행에서 이미 쓴 행이면 상태는 _COVERAGE_RANK 로 병합하고 건수는 누적한다.
    실행이 바뀌면 새로 시작한다 — 그래야 창을 좁혀 재수집했을 때 partial 에서 벗어난다.
    """
    prev = conn.execute(
        "SELECT status, doc_count, updated_utc FROM coverage "
        "WHERE code = ? AND source = ? AND kst_date = ?", (code, source, kst_date)).fetchone()
    if run_started and prev and (prev["updated_utc"] or "") >= run_started:
        doc_count += prev["doc_count"] or 0
        if _COVERAGE_RANK.get(prev["status"], 0) > _COVERAGE_RANK.get(status, 0):
            status = prev["status"]
    conn.execute(
        "INSERT INTO coverage(code, source, kst_date, status, doc_count, last_cursor, attempts, error, updated_utc) "
        "VALUES (?,?,?,?,?,?,1,?,?) "
        "ON CONFLICT(code, source, kst_date) DO UPDATE SET "
        "status = excluded.status, doc_count = excluded.doc_count, "
        "last_cursor = COALESCE(excluded.last_cursor, coverage.last_cursor), "
        "attempts = coverage.attempts + 1, error = excluded.error, updated_utc = excluded.updated_utc",
        (code, source, kst_date, status, doc_count, last_cursor, error, now_utc()),
    )


def completed_dates(conn, code: str, source: str) -> set[str]:
    return {
        r["kst_date"]
        for r in conn.execute(
            "SELECT kst_date FROM coverage WHERE code = ? AND source = ? AND status IN ('completed','empty')",
            (code, source),
        )
    }


# 개장 전 컷오프. 이 시각 이후 게시된 글은 다음 날 신호로 넘긴다.
_CUTOFF = f"{CUTOFF_HH:02d}:{CUTOFF_MM:02d}"
# published_utc(+00:00 포함)를 KST로 옮긴 뒤 컷오프로 신호 귀속일을 정한다.
_SIGNAL_DATE_SQL = (
    "CASE WHEN strftime('%H:%M', published_utc, '+9 hours') <= '" + _CUTOFF + "' "
    "THEN date(published_utc, '+9 hours') "
    "ELSE date(published_utc, '+9 hours', '+1 day') END"
)


def refresh_sentiment_daily(conn, code: str, kst_dates: set[str] | None = None):
    where, params = "code = ?", [code]
    if kst_dates:
        where += f" AND published_kst_date IN ({','.join('?' * len(kst_dates))})"
        params += sorted(kst_dates)

    if kst_dates:
        conn.execute(
            "DELETE FROM sentiment_daily WHERE code = ? AND kst_date IN "
            f"({','.join('?' * len(kst_dates))})", [code] + sorted(kst_dates)
        )
    else:
        conn.execute("DELETE FROM sentiment_daily WHERE code = ?", (code,))

    conn.execute(
        "INSERT INTO sentiment_daily(code, kst_date, signal_date, media_id, pos, neu, neg,"
        " irrelevant, doc_cnt, canonical_cnt, spread_sum) "
        f"SELECT code, published_kst_date, {_SIGNAL_DATE_SQL}, media_id,"
        " COALESCE(SUM(label = 1),0), COALESCE(SUM(label = 0),0), COALESCE(SUM(label = -1),0),"
        " COALESCE(SUM(is_relevant = 0),0), COUNT(*), COALESCE(SUM(is_canonical),0),"
        " COALESCE(SUM(CASE WHEN is_canonical = 0 THEN 1 ELSE 0 END),0) "
        f"FROM documents WHERE published_kst_date IS NOT NULL AND published_utc IS NOT NULL AND {where} "
        f"GROUP BY code, published_kst_date, {_SIGNAL_DATE_SQL}, media_id",
        params,
    )


class RunLogger:
    """파이프라인 실행 기록. 백그라운드로 돌릴 때 진행상황을 DB에서 확인하기 위한 것."""

    def __init__(self, conn, stage: str, code: str | None = None):
        self.conn, self.stage, self.code = conn, stage, code
        self.run_id = uuid.uuid4().hex[:12]

    def __enter__(self):
        self.conn.execute(
            "INSERT INTO pipeline_runs(run_id, stage, code, started_utc, status) VALUES (?,?,?,?, 'running')",
            (self.run_id, self.stage, self.code, now_utc()),
        )
        self.conn.commit()
        return self

    def finish(self, stats: dict):
        import json

        self.conn.execute(
            "UPDATE pipeline_runs SET finished_utc = ?, status = 'done', stats_json = ? WHERE run_id = ?",
            (now_utc(), json.dumps(stats, ensure_ascii=False), self.run_id),
        )

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.conn.execute(
                "UPDATE pipeline_runs SET finished_utc = ?, status = 'failed', error = ? WHERE run_id = ?",
                (now_utc(), f"{exc_type.__name__}: {exc}", self.run_id),
            )
            self.conn.commit()
        return False


# --- 채점 커버리지 -----------------------------------------------------------
# 감성지수는 "채점된 문서"만으로 만들어지는데, 화면에는 그 사실이 어디에도 없다.
# BTC 라벨률 6%와 KOSPI 99%를 같은 표에 나란히 놓으면 "시장마다 관계가 다르다"는
# 결론이 나오지만, 실제로 다른 것은 시장이 아니라 채점 진척도일 수 있다.
#
# 분모에서 가중치 0 채널(NON_OPINION_CHANNELS)을 뺀다. 그것들은 채점하지 않는 것이
# 정상이므로 분모에 남기면 영원히 100%에 닿지 못하고, 커버리지가 낮다는 경고가
# 상시로 켜져 아무도 보지 않게 된다.
COVERAGE_TABLE = "label_coverage"


def compute_label_coverage(conn) -> list[dict]:
    """code별 (여론채널 대표글, 라벨 보유) 집계. documents가 있는 수집 DB 전용."""
    marks = ",".join("?" * len(NON_OPINION_CHANNELS))
    rows = conn.execute(
        "SELECT d.code, COUNT(*) canonical, "
        "       SUM(d.label IS NOT NULL) labeled "
        "FROM documents d JOIN media m ON d.media_id = m.media_id "
        f"WHERE d.is_canonical = 1 AND m.channel NOT IN ({marks}) "
        "GROUP BY d.code", NON_OPINION_CHANNELS).fetchall()
    return [{"code": r["code"], "canonical": r["canonical"],
             "labeled": r["labeled"] or 0} for r in rows]


def label_coverage(conn) -> dict[str, dict]:
    """{code: {canonical, labeled, ratio}}.

    서빙 스냅샷에는 documents가 없으므로 내보내기 시점에 미리 접어둔
    label_coverage 테이블을 읽는다. 수집 DB에서는 그때그때 계산한다.
    둘 다 없으면 빈 dict — 호출부는 '모른다'와 '0%'를 구분해야 한다.
    """
    has_docs = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'").fetchone()
    if has_docs and conn.execute("SELECT 1 FROM documents LIMIT 1").fetchone():
        rows = compute_label_coverage(conn)
    elif conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (COVERAGE_TABLE,)).fetchone():
        rows = [dict(r) for r in conn.execute(
            f"SELECT code, canonical, labeled FROM {COVERAGE_TABLE}")]
    else:
        return {}
    return {r["code"]: {**r, "ratio": (r["labeled"] / r["canonical"]) if r["canonical"] else None}
            for r in rows}


# --- 수집이 미완결로 남은 날짜 -----------------------------------------------------
# partial = "그 날짜를 끝까지 훑지 못했다"(대개 응답 상한 100건), failed = 아예 못
# 받아왔다. 원인은 다르지만 결과는 같다 — 그 날의 건수가 실제보다 적다.
#
# [왜 화면까지 올려야 하는가]
# 지수의 재료는 극성만이 아니다. 백테스트 신호는 순건수 Σ w·(pos−neg)이고 관여도는
# E = z(log(1+doc_cnt))다. 상한에 걸린 날은 doc_cnt 가 100에서 잘린 **검열된 관측치**라,
# "여론이 폭발한 날"과 "상한에 걸린 날"이 같은 값으로 찍힌다. 게다가 이 편향은
# 무작위가 아니라 기사가 가장 많은 날, 즉 신호가 가장 강한 날에 집중된다.
#
# 보정하지 않고 표시만 한다. 잘린 건수의 참값을 알 수 없으므로 추정해서 채우면
# 없는 숫자를 만들어내는 것이 된다.
INCOMPLETE_TABLE = "date_coverage"
_INCOMPLETE_STATUS = ("partial", "failed")


def compute_incomplete_dates(conn) -> list[tuple[str, str]]:
    """(code, kst_date) 목록. coverage 가 있는 수집 DB 전용.

    소스 단위로 기록된 것을 날짜 단위로 접는다 — sentiment_daily 는 소스를 구분하지
    않으므로, 한 소스라도 잘렸으면 그 날의 건수는 검열된 것이다.
    """
    marks = ",".join("?" * len(_INCOMPLETE_STATUS))
    return [(r["code"], r["kst_date"]) for r in conn.execute(
        f"SELECT DISTINCT code, kst_date FROM coverage WHERE status IN ({marks})",
        _INCOMPLETE_STATUS)]


def incomplete_dates(conn, code: str) -> list[str]:
    # 테이블 존재가 아니라 **행이 있는지**로 판정한다. 빈 테이블이 있을 수 있고
    # (배포된 스냅샷에 서빙 프로세스가 스키마를 만들어 둔 경우), 그때 존재만 보고
    # 분기하면 조용히 빈 결과를 돌려준다.
    def has_rows(t):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (t,)).fetchone():
            return False
        return bool(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone())

    if has_rows("coverage"):
        marks = ",".join("?" * len(_INCOMPLETE_STATUS))
        rows = conn.execute(
            f"SELECT DISTINCT kst_date FROM coverage WHERE code = ? AND status IN ({marks}) "
            "ORDER BY kst_date", (code, *_INCOMPLETE_STATUS)).fetchall()
    elif has_rows(INCOMPLETE_TABLE):
        rows = conn.execute(
            f"SELECT kst_date FROM {INCOMPLETE_TABLE} WHERE code = ? ORDER BY kst_date",
            (code,)).fetchall()
    else:
        return []
    return [r["kst_date"] for r in rows]
