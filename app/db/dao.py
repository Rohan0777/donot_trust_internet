"""문서 저장 · 중복판정 · 수집원장 갱신.

수집기(collectors)는 파싱만 하고, 정규화/해시/중복/원장은 전부 여기를 통한다.
파이프라인 순서가 중요하다 — body_hash는 본문을 파기하기 전에만 계산할 수 있다.
"""
import uuid
from datetime import datetime

from app.config import KST, UTC, WINDOW_END_HHMM

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


def upsert_coverage(conn, code: str, source: str, kst_date: str, status: str,
                    doc_count: int = 0, last_cursor: str | None = None, error: str | None = None):
    """status 의미는 schema.sql 참조. 부분 수집을 completed로 찍으면 그 날짜가
    영구히 반쪽으로 고정되므로, 경계(더 오래된 글) 확인 전에는 partial을 유지한다."""
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
