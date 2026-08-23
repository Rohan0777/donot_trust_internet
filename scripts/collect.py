"""수집 전용 실행기 — 서빙/분석과 완전히 분리된 독립 프로세스.

장시간 백필을 백그라운드로 돌리기 위한 진입점이다. 진행상황은 stdout과
pipeline_runs 테이블 양쪽에 남으므로, 프로세스를 떼어놓고도 DB로 상태를 볼 수 있다.

  python -m scripts.collect status
  python -m scripts.collect prices 000660 --years 3
  python -m scripts.collect news   000660 --days 30
  python -m scripts.collect board  000660 --days 30 --max-pages 3000
  python -m scripts.collect board  000660 --days 180 --log logs/board_000660.log
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors import fourchan, google_news_rss, naver_board, naver_news, price
from app.db.conn import get_conn, init_db
from app.db.dao import RunLogger, refresh_sentiment_daily


class Tee:
    def __init__(self, path: Path | None):
        self.fh = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = path.open("a", encoding="utf-8")

    def __call__(self, *args, **kwargs):
        kwargs.pop("flush", None)
        msg = " ".join(str(a) for a in args)
        print(msg, flush=True)
        if self.fh:
            self.fh.write(msg + "\n")
            self.fh.flush()


def cmd_status(args, log):
    with get_conn() as conn:
        log("=== 수집 원장 (coverage) ===")
        for r in conn.execute(
            "SELECT code, source, status, COUNT(*) days, MIN(kst_date) mn, MAX(kst_date) mx, "
            "SUM(doc_count) docs FROM coverage GROUP BY code, source, status ORDER BY code, source, status"
        ):
            log(f"  {r['code']} {r['source']:<16} {r['status']:<10} {r['days']:>4}일  "
                f"{r['mn']} ~ {r['mx']}  ({r['docs']:,}건)")
        log("\n=== 문서 / 채점 ===")
        for r in conn.execute(
            "SELECT code, COUNT(*) n, SUM(label IS NOT NULL) labeled, SUM(is_canonical=0) dup "
            "FROM documents GROUP BY code"
        ):
            log(f"  {r['code']}  총 {r['n']:,}  채점 {r['labeled']:,}  중복강등 {r['dup']:,}")
        log("\n=== 최근 실행 ===")
        for r in conn.execute(
            "SELECT run_id, stage, code, status, started_utc, finished_utc, stats_json "
            "FROM pipeline_runs ORDER BY started_utc DESC LIMIT 8"
        ):
            log(f"  {r['started_utc']}  {r['stage']:<8} {r['code'] or '-':<8} {r['status']:<8} "
                f"{(r['stats_json'] or '')[:90]}")


def _run(stage: str, args, log, fn):
    with get_conn() as conn:
        with RunLogger(conn, stage, args.code) as run:
            log(f"=== [{stage}] {args.code} 시작 (run_id={run.run_id}) ===")
            stats = fn(conn)
            run.finish(stats)
            log(f"=== [{stage}] 완료: {stats} ===")
            reason = stats.get("stopped")
            if reason == "page_cap":
                log("  [경고] max_pages 상한 도달 — --max-pages를 올려 재실행하면 더 받는다.")
            elif reason == "board_end":
                log(f"  [경고] 게시판 페이지 소진({stats.get('pages')}p) — 상한을 올려도 더는 못 받는다. "
                    f"확보 구간은 {stats.get('oldest')} 이후뿐이며, coverage에 partial로 남는다.")
    return stats


def cmd_board(args, log):
    stats = _run("board", args, log, lambda conn: naver_board.crawl(
        conn, args.code, days_back=args.days, max_pages=args.max_pages,
        force=args.force, progress=log))
    with get_conn() as conn:
        refresh_sentiment_daily(conn, args.code)


def cmd_news(args, log):
    _run("news", args, log, lambda conn: naver_news.crawl(
        conn, args.code, days_back=args.days, max_pages=args.max_pages, progress=log))
    with get_conn() as conn:
        refresh_sentiment_daily(conn, args.code)


def cmd_gnews(args, log):
    """Google News RSS 백필. 네이버 검색 API가 못 하는 과거 구간을 담당한다."""
    from datetime import date, timedelta

    end = args.end or date.today().isoformat()
    start = args.start or (date.fromisoformat(end) - timedelta(days=args.days)).isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM entities WHERE code = ?", (args.code,)).fetchone()
        if not row:
            raise SystemExit(f"등록되지 않은 종목: {args.code}")
        with RunLogger(conn, "gnews", args.code) as run:
            log(f"=== [gnews] {row['name']}({args.code}) {start} ~ {end} (창 {args.step}일) ===")
            stats = google_news_rss.crawl_entity(conn, args.code, row["name"], start, end,
                                                 step_days=args.step, progress=log,
                                                 adaptive=not args.fixed_window)
            run.finish(stats)
        log(f"=== [gnews] 완료: {stats} ===")
        if stats["truncated"]:
            log(f"  [경고] {stats['truncated']}개 창이 100건 상한에 도달했다. "
                f"--step 을 줄여 해당 구간을 재수집하라 (coverage가 partial로 남아 있다).")
        refresh_sentiment_daily(conn, args.code)


def cmd_biz(args, log):
    """4chan /biz/ 수집. 과거를 못 사는 소스이므로 매일 돌려야 한다."""
    import json

    with get_conn() as conn:
        codes = ([args.code] if args.code else
                 [r["code"] for r in conn.execute(
                     "SELECT code FROM entities WHERE is_active=1 AND priority=1 AND kind='crypto'")])
        for code in codes:
            row = conn.execute("SELECT name, aliases_json FROM entities WHERE code=?", (code,)).fetchone()
            if not row:
                log(f"  [건너뜀] 등록되지 않은 엔티티: {code}")
                continue
            terms = tuple(json.loads(row["aliases_json"]) if row["aliases_json"] else [row["name"]])
            # 한글 별칭은 /biz/에서 의미 없으므로 영문/기호만 남긴다.
            terms = tuple(t for t in terms if not any("가" <= ch <= "힣" for ch in t))
            if not terms:
                log(f"  [건너뜀] {code}: 영문 별칭 없음")
                continue
            with RunLogger(conn, "biz", code) as run:
                st = fourchan.crawl(conn, code, terms, board=args.board,
                                    max_threads=args.max_threads, progress=log)
                run.finish(st)
            refresh_sentiment_daily(conn, code)


def cmd_prices(args, log):
    with get_conn() as conn:
        n = price.collect_prices(conn, args.code, years=args.years)
    log(f"[prices] {args.code}: {n:,}행")


def cmd_master(args, log):
    with get_conn() as conn:
        n = price.sync_kospi_master(conn)
    log(f"[master] KOSPI 종목 {n:,}건 동기화")


def main():
    ap = argparse.ArgumentParser(description="수집 전용 실행기")
    ap.add_argument("--log", type=Path, default=None, help="로그 파일 경로(백그라운드 실행용)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="수집 원장/실행 이력 조회")
    sub.add_parser("master", help="KOSPI 종목마스터 동기화")

    p = sub.add_parser("prices", help="가격 수집")
    p.add_argument("code")
    p.add_argument("--years", type=int, default=3)

    p_b = sub.add_parser("biz", help="4chan /biz/ 수집 (백필 불가 — 매일 실행 필요)")
    p_b.add_argument("code", nargs="?", default=None, help="생략하면 crypto 엔티티 전체")
    p_b.add_argument("--board", default="biz")
    p_b.add_argument("--max-threads", type=int, default=60)

    p_g = sub.add_parser("gnews", help="Google News RSS 백필 (과거 구간 담당)")
    p_g.add_argument("code")
    p_g.add_argument("--days", type=int, default=90, help="end 기준 소급 일수")
    p_g.add_argument("--start", default=None, help="YYYY-MM-DD")
    p_g.add_argument("--end", default=None, help="YYYY-MM-DD")
    p_g.add_argument("--step", type=int, default=32, help="시작 창 크기(일). 상한에 걸리면 자동 분할")
    p_g.add_argument("--fixed-window", action="store_true",
                     help="적응형 분할을 끄고 --step 고정 창으로만 수집")

    for name, default_pages in (("news", 5000), ("board", 3000)):
        q = sub.add_parser(name, help=f"{name} 수집")
        q.add_argument("code")
        q.add_argument("--days", type=int, default=30)
        q.add_argument("--max-pages", type=int, default=default_pages)
        q.add_argument("--force", action="store_true", help="coverage completed 날짜도 재수집")

    args = ap.parse_args()
    if not hasattr(args, "code"):
        args.code = None
    log = Tee(args.log)
    init_db()
    {"status": cmd_status, "master": cmd_master, "prices": cmd_prices,
     "news": cmd_news, "board": cmd_board, "gnews": cmd_gnews,
     "biz": cmd_biz}[args.cmd](args, log)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
