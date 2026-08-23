"""일일 수집 파이프라인 — 스케줄러가 이것 하나만 호출하면 된다.

  python -m scripts.daily                 # 어제~오늘 수집 + 채점 + 집계
  python -m scripts.daily --catchup 14    # 놓친 날짜까지 소급 (기본 7일)
  python -m scripts.daily --no-score      # 수집만

[놓친 날 처리]
노트북이 꺼져 있으면 그날 수집을 건너뛴다. coverage 원장에서 빈 날짜를 찾아
--catchup 범위 안에서 자동 보충하므로 며칠 꺼져 있어도 복구된다.
단 4chan은 예외다 — 살아있는 카탈로그만 존재해 소급이 불가능하므로 항상
"지금 보이는 것"만 가져온다. 이것이 매일 실행해야 하는 유일한 이유다.
"""
import argparse
import json
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors import fourchan, google_news_rss, price
from app.db.conn import get_conn, init_db
from app.db.dao import RunLogger, refresh_sentiment_daily
from app.scoring.scorer import score_pending
from scripts.collect import Tee


def active_entities(conn, kinds=None, priority=1):
    sql = "SELECT code, name, kind, calendar, aliases_json FROM entities WHERE is_active=1 AND priority<=?"
    params = [priority]
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += list(kinds)
    return conn.execute(sql + " ORDER BY priority, code", params).fetchall()


def missing_dates(conn, code: str, source: str, lo: date, hi: date) -> list[date]:
    """coverage에 completed/empty로 기록되지 않은 날짜. 이것이 곧 작업 큐다."""
    done = {r["kst_date"] for r in conn.execute(
        "SELECT kst_date FROM coverage WHERE code=? AND source=? AND status IN ('completed','empty')",
        (code, source))}
    out, cur = [], lo
    while cur <= hi:
        if cur.isoformat() not in done:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def run(args, log):
    today = date.today()
    lo = today - timedelta(days=args.catchup)
    init_db()
    summary = {"gnews": 0, "biz": 0, "prices": 0, "scored": 0, "errors": 0}

    with get_conn() as conn:
        ents = active_entities(conn, priority=args.priority)
        log(f"=== 일일 파이프라인 {today} (소급 {args.catchup}일, 대상 {len(ents)}개) ===")

        # --- 1. 뉴스 ---
        for e in ents:
            gaps = missing_dates(conn, e["code"], "google_rss", lo, today)
            if not gaps:
                continue
            start, end = gaps[0].isoformat(), gaps[-1].isoformat()
            try:
                with RunLogger(conn, "daily_gnews", e["code"]) as run_:
                    st = google_news_rss.crawl_entity(
                        conn, e["code"], e["name"], start, end, progress=log)
                    run_.finish(st)
                summary["gnews"] += st["saved"]
                log(f"  [뉴스] {e['code']:<9} {start}~{end} 결손 {len(gaps)}일 -> {st['saved']:,}건")
            except Exception:
                summary["errors"] += 1
                log(f"  [실패] 뉴스 {e['code']}\n{traceback.format_exc(limit=2)}")

        # --- 2. 4chan (소급 불가 — 항상 현재 카탈로그) ---
        for e in ents:
            if e["kind"] != "crypto":
                continue
            terms = tuple(t for t in (json.loads(e["aliases_json"]) if e["aliases_json"] else [e["name"]])
                          if not any("가" <= ch <= "힣" for ch in t))
            if not terms:
                continue
            try:
                with RunLogger(conn, "daily_biz", e["code"]) as run_:
                    st = fourchan.crawl(conn, e["code"], terms, progress=log)
                    run_.finish(st)
                summary["biz"] += st["saved"]
            except Exception:
                summary["errors"] += 1
                log(f"  [실패] biz {e['code']}\n{traceback.format_exc(limit=2)}")

        # --- 3. 가격 (가격 소스가 있는 종목만) ---
        for e in ents:
            if e["kind"] not in ("equity",):
                continue   # 지수·코인·채권 가격 소스는 미구현 [확인 필요]
            try:
                summary["prices"] += price.collect_prices(conn, e["code"], years=1)
            except Exception:
                summary["errors"] += 1
                log(f"  [실패] 가격 {e['code']}: {sys.exc_info()[1]}")

        # --- 4. 채점 + 집계 ---
        if not args.no_score:
            for e in ents:
                try:
                    st = score_pending(conn, e["code"], e["name"],
                                       daily_cap=args.daily_cap or None, progress=log)
                    summary["scored"] += st.scored
                    if st.scored:
                        log(f"  [채점] {e['code']:<9} {st.scored:,}건 "
                            f"(무관 {st.relevant_false:,} 상속 {st.inherited:,})")
                except Exception:
                    summary["errors"] += 1
                    log(f"  [실패] 채점 {e['code']}: {sys.exc_info()[1]}")
                refresh_sentiment_daily(conn, e["code"])

    log(f"=== 완료: {summary} ===")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catchup", type=int, default=7, help="놓친 날짜 소급 범위(일)")
    ap.add_argument("--priority", type=int, default=1, help="이 값 이하 우선순위만 처리")
    ap.add_argument("--daily-cap", type=int, default=200)
    ap.add_argument("--no-score", action="store_true")
    ap.add_argument("--log", type=Path, default=Path("logs/daily.log"))
    args = ap.parse_args()
    log = Tee(args.log)
    st = run(args, log)
    sys.exit(1 if st["errors"] else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
