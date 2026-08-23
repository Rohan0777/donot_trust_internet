"""감성 채점 실행기 (수집기와 마찬가지로 독립 프로세스).

  python -m scripts.score 035720 --limit 200
  python -m scripts.score 035720 --channel community
  python -m scripts.score 035720 --dry-run          # 비용 추정만
  python -m scripts.score 035720 --log logs/score_035720.log
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.conn import get_conn, init_db
from app.db.dao import RunLogger, refresh_sentiment_daily
from app.scoring.scorer import BATCH_SIZE, score_pending
from scripts.collect import Tee


def estimate(conn, code: str, channel: str | None):
    sql = ("SELECT m.channel, COUNT(*) n FROM documents d JOIN media m ON d.media_id = m.media_id "
           "WHERE d.code = ? AND d.label IS NULL AND d.is_canonical = 1 ")
    params = [code]
    if channel:
        sql += "AND m.channel = ? "
        params.append(channel)
    sql += "GROUP BY m.channel"
    rows = conn.execute(sql, params).fetchall()
    total = sum(r["n"] for r in rows)
    dup = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE code = ? AND label IS NULL AND is_canonical = 0", (code,)
    ).fetchone()[0]
    return rows, total, dup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--since-days", type=int, default=None)
    ap.add_argument("--channel", choices=["news", "community", "cafe", "blog"], default=None)
    ap.add_argument("--keep-body", action="store_true", help="채점 후 원문을 지우지 않는다(디버깅용)")
    ap.add_argument("--daily-cap", type=int, default=200,
                    help="(종목,일)당 채점 상한. 0이면 전수. 기본 200 — 실측 오차 0.037")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", type=Path, default=None)
    args = ap.parse_args()

    log = Tee(args.log)
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM stocks WHERE code = ?", (args.code,)).fetchone()
        if not row:
            raise SystemExit(f"등록되지 않은 종목: {args.code}")
        name = row["name"]

        rows, total, dup = estimate(conn, args.code, args.channel)
        log(f"[추정] {name}({args.code}) 미채점 대표글 {total:,}건")
        for r in rows:
            log(f"    {r['channel']:<10} {r['n']:>6,}건")
        log(f"    중복 상속 대기 {dup:,}건 (채점 불필요 — 대표 라벨을 물려받음)")
        log(f"    예상 LLM 호출 {(total + BATCH_SIZE - 1)//BATCH_SIZE:,}회 (배치 {BATCH_SIZE}건)")
        if args.dry_run:
            log("[dry-run] 실제 채점은 하지 않았습니다.")
            return
        if not total:
            return

        with RunLogger(conn, "score", args.code) as run:
            st = score_pending(conn, args.code, name, limit=args.limit,
                               since_days=args.since_days, channel=args.channel,
                               purge_body=not args.keep_body,
                               daily_cap=args.daily_cap or None, progress=log)
            payload = {"scored": st.scored, "irrelevant": st.relevant_false,
                       "missing": st.missing, "failed": st.failed, "batches": st.batches,
                       "retries": st.retries, "inherited": st.inherited, "labels": st.by_label}
            run.finish(payload)
        refresh_sentiment_daily(conn, args.code)
        log(f"[완료] {payload}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
