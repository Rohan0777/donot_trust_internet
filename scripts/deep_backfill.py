"""아카이브 고갈 백필 — 소스가 가진 과거를 한 번은 끝까지 훑는다.

  python -m scripts.deep_backfill                     # 모든 엔티티 · 기본 시작점
  python -m scripts.deep_backfill --start 2020-01-01  # 시작점 지정
  python -m scripts.deep_backfill --codes BTC ETH     # 일부만

Google News RSS 아카이브는 얕다 — 한국어는 약 1년, 영어는 2022년쯤부터가 실용
한계고 그 이전은 창당 수 건이다. 그래서 이 스크립트의 가치는 "10년을 채우는 것"이
아니라 **소스가 주는 만큼을 한 번은 받아서 원장에 새겨 두는 것**이다.

완료 판정은 coverage 원장(completed/empty)으로 한다. 이미 끝까지 훑은 날짜는
건너뛰므로, 몇 번을 다시 돌려도 요청은 남은 구간에만 들어간다 — partial/failed/
pending만 재시도 대상이 된다. 즉 스케줄러의 전진 수집과 함께 돌리면 "무한 백필"의
실체가 된다: 앞으로는 매일 쌓이고, 과거는 소스가 허용하는 만큼 최초 1회 고갈.

[동시 실행 금지] data/daily.lock 을 daily 와 공유해 상호 배타한다 — 두 writer가
media INSERT 에 경쟁하면 UNIQUE 충돌로 라운드가 죽는다(2026-08-25 실측).
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors import google_news_rss
from app.config import DATA_DIR
from app.db.conn import get_conn, init_db
from app.db.dao import RunLogger, completed_dates

LOCK_FILE = DATA_DIR / "daily.lock"          # scripts.daily 와 공유하는 상호 배타
LOCK_STALE_SEC = 24 * 3600                   # 딥백필 자체가 길 수 있으므로 넉넉히


def _acquire_lock() -> bool:
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() - LOCK_FILE.stat().st_mtime > LOCK_STALE_SEC:
                LOCK_FILE.unlink()
                continue
            return False
    return False


def contiguous_days(missing: list[date]) -> list[tuple[date, date]]:
    """비연속 결손일을 연속 구간으로 접는다. 크롤러는 구간 단위라 호출 수가 줄어든다."""
    runs: list[tuple[date, date]] = []
    start = prev = None
    for d in missing:
        if prev is None or (d - prev).days > 1:
            if start is not None:
                runs.append((start, prev))
            start = d
        prev = d
    if start is not None:
        runs.append((start, prev))
    return runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01", help="소급 시작점 (기본 2016-01-01)")
    ap.add_argument("--codes", nargs="*", help="이 코드만 (기본: 활성 전체)")
    args = ap.parse_args()

    lo = date.fromisoformat(args.start)
    hi = date.today()
    init_db()

    if not _acquire_lock():
        print("다른 수집 프로세스(daily 등)가 실행 중이다 — 나중에 다시 시도하라.")
        sys.exit(3)
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT code, name FROM entities WHERE is_active=1 ORDER BY priority, code"
            ).fetchall()
            targets = [(r["code"], r["name"]) for r in rows
                       if not args.codes or r["code"] in set(args.codes)]
            print(f"=== 아카이브 고갈 백필 {lo} ~ {hi} · 대상 {len(targets)}개 ===")
            for code, name in targets:
                done = completed_dates(conn, code, "google_rss")
                missing = []
                cur = lo
                while cur <= hi:
                    if cur.isoformat() not in done:
                        missing.append(cur)
                    cur += timedelta(days=1)
                runs = contiguous_days(missing)
                span = sum((b - a).days + 1 for a, b in runs)
                if not runs:
                    print(f"[{code}] 결손 없음 — 건너뜀")
                    continue
                print(f"[{code}] 결손 {len(missing)}일({len(runs)}구간) 백필 시작")
                for a, b in runs:
                    try:
                        with RunLogger(conn, "deep_gnews", code) as run_:
                            st = google_news_rss.crawl_entity(
                                conn, code, name, a.isoformat(), b.isoformat())
                            run_.finish(st)
                        print(f"  {a}~{b}: 신규 {st.get('saved', 0):,}건 "
                              f"(offtopic {st.get('offtopic', 0):,})")
                    except Exception as exc:  # noqa: BLE001 - 한 구간 실패에 전체가 멈추지 않는다
                        print(f"  [실패] {a}~{b}: {type(exc).__name__}: {exc}")
    finally:
        LOCK_FILE.unlink(missing_ok=True)
    print("DEEP_DONE")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
