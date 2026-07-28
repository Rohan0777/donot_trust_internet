"""구 프로젝트(sentiment_predictor/data/stock_sentiment.db) -> 신규 스키마 이관.

이관 정책:
  - naver_discussion / dcinside_stock : 전량 제외. 제목이 U+FFFD로 파손되어(99.4%)
    복원이 불가능하다. coverage에 'failed'로 기록해 재수집 대상으로 남긴다.
  - naver_news / naver_api_news / naver_api_cafe : 이관. 제목 정상 확인됨.
  - 시각: naive는 KST로 간주해 UTC 변환, tz-aware는 그대로 UTC 변환.
  - 라벨: 3-class(sentiment 컬럼)만 이관. 레거시 연속점수(sentiment_score)는
    스케일이 달라 섞을 수 없으므로 label=NULL로 두고 재채점 대상으로 남긴다.

실행: python -m scripts.migrate_legacy [--legacy <path>] [--dry-run]
"""
import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DB_PATH, KST, UTC
from app.db.conn import get_conn, init_db
from app.db.dao import refresh_sentiment_daily
from app.text.normalize import blocking_key, normalize_title, simhash64, title_hash

LEGACY_DEFAULT = Path(
    r"C:\Users\kh991\OneDrive\문서\python\주식예측\sentiment_predictor\data\stock_sentiment.db"
)

CORRUPTED_SOURCES = ("naver_discussion", "dcinside_stock")
PORTABLE_SOURCES = ("naver_news", "naver_api_news", "naver_api_cafe")

TIER_RENAME = {"main": "major", "sub": "daily", "mini": "online"}
ALIASES = {
    "000660": ("SK하이닉스", "sk하이닉스", "하이닉스"),
    "005930": ("삼성전자",),
    "035720": ("카카오",),
}
# "카카오" 오탐 제외어 — 수집 쿼리와 LLM 판정 양쪽에서 쓴다.
EXCLUDES = {
    "035720": '["카카오뱅크","카카오페이","카카오모빌리티","카카오게임즈","카카오톡 오류"]',
}


def to_utc(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def kst_date_of(utc_iso: str | None) -> str | None:
    if not utc_iso:
        return None
    return datetime.fromisoformat(utc_iso).astimezone(KST).strftime("%Y-%m-%d")


def migrate(legacy_path: Path, dry_run: bool = False):
    if not legacy_path.exists():
        raise SystemExit(f"레거시 DB를 찾을 수 없습니다: {legacy_path}")

    init_db()
    src = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    stats = defaultdict(int)

    with get_conn() as dst:
        # --- 1. 종목 ---
        for row in src.execute("SELECT code, name FROM stock_master"):
            if row["code"] not in ALIASES:
                continue
            dst.execute(
                "INSERT OR REPLACE INTO stocks(code, name, market, is_kospi200, aliases_json, exclude_json) "
                "VALUES (?, ?, 'KOSPI', 1, ?, ?)",
                (row["code"], row["name"],
                 '["' + '","'.join(ALIASES[row["code"]]) + '"]',
                 EXCLUDES.get(row["code"])),
            )
            stats["stocks"] += 1

        # --- 2. 매체 레지스트리 ---
        legacy_tiers = {r["press"]: r["tier"] for r in src.execute("SELECT press, tier FROM media_tier_map")}
        press_rows = src.execute(
            "SELECT DISTINCT press FROM articles WHERE press IS NOT NULL AND source IN "
            f"({','.join('?' * len(PORTABLE_SOURCES))})", PORTABLE_SOURCES
        ).fetchall()
        for r in press_rows:
            press = r["press"]
            tier = TIER_RENAME.get(legacy_tiers.get(press), "unknown")
            domain = press if "." in press else None
            dst.execute(
                "INSERT OR IGNORE INTO media(name, domain, tier, channel) VALUES (?, ?, ?, 'news')",
                (press, domain, tier),
            )
            stats["media_news"] += 1

        # 채널 자체가 하나의 매체인 것들 (media_id NULL 방지)
        for name, tier, channel in [
            ("네이버 카페", "community", "cafe"),
            ("네이버 종목토론방", "community", "community"),
            ("네이버 블로그", "blog", "blog"),
            ("미상 언론사", "unknown", "news"),
        ]:
            dst.execute(
                "INSERT OR IGNORE INTO media(name, tier, channel) VALUES (?, ?, ?)", (name, tier, channel)
            )

        media_id = {(r["name"], r["channel"]): r["media_id"]
                    for r in dst.execute("SELECT media_id, name, channel FROM media")}
        cafe_mid = media_id[("네이버 카페", "cafe")]
        unknown_mid = media_id[("미상 언론사", "news")]

        # --- 3. 가격 ---
        for r in src.execute("SELECT code, date, open, high, low, close, volume FROM prices"):
            if r["code"] not in ALIASES:
                continue
            dst.execute(
                "INSERT OR REPLACE INTO prices(code, kst_date, open, high, low, close, volume, is_adjusted) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (r["code"], r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]),
            )
            stats["prices"] += 1

        # --- 4. 문서 ---
        rows = src.execute(
            "SELECT id, code, source, press, url, title, collected_at, published_at, sentiment, sentiment_score "
            f"FROM articles WHERE source IN ({','.join('?' * len(PORTABLE_SOURCES))})", PORTABLE_SOURCES
        ).fetchall()

        for r in rows:
            if r["code"] not in ALIASES:
                stats["skip_unknown_code"] += 1
                continue
            title = r["title"] or ""
            if not title.strip():
                stats["skip_empty_title"] += 1
                continue
            if "\ufffd" in title:
                stats["skip_corrupted"] += 1
                continue

            collected_utc = to_utc(r["collected_at"])
            published_utc = to_utc(r["published_at"])
            ts_conf = "exact"
            if not published_utc:
                published_utc = collected_utc
                ts_conf = "approx"

            if r["source"] == "naver_api_cafe":
                mid = cafe_mid
            else:
                mid = media_id.get((r["press"], "news"), unknown_mid) if r["press"] else unknown_mid

            norm = normalize_title(title, ALIASES[r["code"]])
            label = r["sentiment"]
            if label is None and r["sentiment_score"] is not None:
                stats["legacy_score_needs_rescore"] += 1

            dst.execute(
                "INSERT OR IGNORE INTO documents"
                "(code, media_id, source, url, title, norm_title, title_hash, simhash,"
                " published_utc, published_kst_date, collected_utc, ts_confidence,"
                " label, is_relevant, label_model, prompt_version, labeled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["code"], mid, r["source"], r["url"], title, norm, title_hash(norm), simhash64(norm),
                 published_utc, kst_date_of(published_utc), collected_utc, ts_conf,
                 label, 1 if label is not None else None,
                 "gpt-4o-mini" if label is not None else None,
                 "legacy" if label is not None else None,
                 collected_utc if label is not None else None),
            )
            stats["documents"] += 1
            if label is not None:
                stats["labeled"] += 1

        # --- 5. 중복 판정 (완전일치: 같은 종목/같은 날/같은 정규화제목) ---
        dup_rows = dst.execute(
            "SELECT code, published_kst_date d, title_hash, COUNT(*) n, MIN(doc_id) keep "
            "FROM documents WHERE published_kst_date IS NOT NULL "
            "GROUP BY code, d, title_hash HAVING n > 1"
        ).fetchall()
        for r in dup_rows:
            dst.execute(
                "UPDATE documents SET dup_group_id = ?, is_canonical = CASE WHEN doc_id = ? THEN 1 ELSE 0 END "
                "WHERE code = ? AND published_kst_date = ? AND title_hash = ?",
                (r["title_hash"][:12], r["keep"], r["code"], r["d"], r["title_hash"]),
            )
            stats["dup_groups"] += 1
            stats["dup_demoted"] += r["n"] - 1

        # --- 6. 사전집계 (신호 귀속일 계산 포함) ---
        for c in ALIASES:
            refresh_sentiment_daily(dst, c)
        stats["sentiment_daily"] = dst.execute("SELECT COUNT(*) FROM sentiment_daily").fetchone()[0]

        # --- 7. 수집 원장 ---
        now = datetime.now(UTC).isoformat(timespec="seconds")
        # 이관된 소스: 실제 문서가 있는 날짜만 partial로 기록한다.
        # 구 크롤러가 페이지 상한에 걸렸는지 알 수 없으므로 completed로 단정하지 않는다.
        dst.execute(
            "INSERT OR REPLACE INTO coverage(code, source, kst_date, status, doc_count, attempts, updated_utc) "
            "SELECT code, source, published_kst_date, 'partial', COUNT(*), 1, ? "
            "FROM documents WHERE published_kst_date IS NOT NULL GROUP BY code, source, published_kst_date",
            (now,),
        )
        # 파손된 커뮤니티 소스: 재수집 대상으로 명시 기록
        for r in src.execute(
            "SELECT code, source, substr(COALESCE(published_at, collected_at),1,10) d, COUNT(*) n "
            f"FROM articles WHERE source IN ({','.join('?' * len(CORRUPTED_SOURCES))}) "
            "GROUP BY code, source, d", CORRUPTED_SOURCES
        ):
            if r["code"] not in ALIASES or not r["d"] or len(r["d"]) != 10:
                continue
            dst.execute(
                "INSERT OR REPLACE INTO coverage(code, source, kst_date, status, doc_count, attempts, error, updated_utc) "
                "VALUES (?,?,?,'failed',0,0,'legacy encoding corruption (U+FFFD) - full re-collect required',?)",
                (r["code"], r["source"], r["d"], now),
            )
            stats["coverage_failed"] += 1

        stats["coverage_rows"] = dst.execute("SELECT COUNT(*) FROM coverage").fetchone()[0]

        # --- 8. 거래비용 (기본값 — 실제 요율은 사용자 확인 후 갱신) ---
        dst.execute(
            "INSERT OR IGNORE INTO fee_schedule(effective_from, buy_bps, sell_bps, tax_bps, slippage_bps) "
            "VALUES ('2000-01-01', 1.5, 1.5, 18.0, 5.0)"
        )

        if dry_run:
            raise RuntimeError("dry-run: 롤백")

    src.close()
    return stats


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy", default=str(LEGACY_DEFAULT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    st = migrate(Path(args.legacy), args.dry_run)
    print(f"=== 이관 완료 -> {DB_PATH} ===")
    for k, v in sorted(st.items()):
        print(f"  {k:32} {v:>8,}")
