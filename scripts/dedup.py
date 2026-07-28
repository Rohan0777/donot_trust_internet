"""근사중복 판정 + 도배 억제 일괄 적용.

  python -m scripts.dedup 035720
  python -m scripts.dedup --all --dry-run
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import MAX_DOCS_PER_AUTHOR_PER_DAY
from app.db.conn import get_conn, init_db
from app.db.dao import refresh_sentiment_daily
from app.text.dedup import group_day, spam_demotions


def run(conn, code: str, dry_run: bool = False, progress=print) -> dict:
    dates = [r["d"] for r in conn.execute(
        "SELECT DISTINCT published_kst_date d FROM documents "
        "WHERE code = ? AND published_kst_date IS NOT NULL ORDER BY d", (code,))]

    stats = {"dates": len(dates), "docs": 0, "groups": 0, "demoted": 0, "spam_demoted": 0}
    for d in dates:
        rows = conn.execute(
            "SELECT doc_id, norm_title, title_hash, simhash, author FROM documents "
            "WHERE code = ? AND published_kst_date = ? ORDER BY doc_id", (code, d)
        ).fetchall()
        docs = [dict(r) for r in rows]
        stats["docs"] += len(docs)
        if not docs:
            continue

        mapping = group_day(docs)
        members = defaultdict(list)
        for doc_id, rep in mapping.items():
            members[rep].append(doc_id)

        spam = spam_demotions(docs)
        stats["spam_demoted"] += len(spam)

        for rep, ids in members.items():
            if len(ids) > 1:
                stats["groups"] += 1
                stats["demoted"] += len(ids) - 1
            gid = f"g{rep}" if len(ids) > 1 else None
            for doc_id in ids:
                # 대표 조건: 그룹 대표이면서 도배 초과분이 아님
                canonical = 1 if (doc_id == rep and doc_id not in spam) else 0
                if not dry_run:
                    conn.execute(
                        "UPDATE documents SET dup_group_id = ?, is_canonical = ? WHERE doc_id = ?",
                        (gid, canonical, doc_id),
                    )
        if not dry_run:
            conn.commit()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    init_db()
    with get_conn() as conn:
        codes = ([r["code"] for r in conn.execute("SELECT code FROM stocks WHERE is_kospi200 = 1")]
                 if args.all else [args.code])
        if not codes or codes == [None]:
            raise SystemExit("종목코드 또는 --all 이 필요합니다.")

        for code in codes:
            before = conn.execute(
                "SELECT SUM(is_canonical = 0) demoted, COUNT(*) n FROM documents WHERE code = ?", (code,)
            ).fetchone()
            st = run(conn, code, args.dry_run)
            after = conn.execute(
                "SELECT SUM(is_canonical = 0) demoted FROM documents WHERE code = ?", (code,)
            ).fetchone()
            print(f"[{code}] {st['dates']}일 · 문서 {st['docs']:,}건")
            print(f"   중복 그룹 {st['groups']:,}개 · 강등 {st['demoted']:,}건 "
                  f"(도배 초과 {st['spam_demoted']:,}건 포함, 상한 {MAX_DOCS_PER_AUTHOR_PER_DAY}건/인/일)")
            print(f"   강등 총계 {before['demoted']:,} -> {after['demoted']:,} "
                  f"({after['demoted']/max(before['n'],1)*100:.1f}%)")
            if not args.dry_run:
                refresh_sentiment_daily(conn, code)
        if args.dry_run:
            conn.rollback()
            print("[dry-run] 롤백됨")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
