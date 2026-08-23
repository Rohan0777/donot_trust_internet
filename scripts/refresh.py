"""사전집계(sentiment_daily) 재구축 — documents로부터 결정적으로 다시 만든다.

  python -m scripts.refresh                # 전 활성 엔티티 재집계
  python -m scripts.refresh BTC 035720     # 지정 종목만
  python -m scripts.refresh --verify       # 재집계 없이 어긋난 종목만 보고

healthcheck가 "집계가 문서와 어긋난다 -> refresh 필요"라고 말해도 사용자가 칠
명령이 없었다. refresh_sentiment_daily는 collect/dedup/map_media/daily 안에서만
호출되므로, 백필이 그 경로 밖에서 끝나면(예: 중단 후 재개, 스크립트 직접 호출)
집계가 조용히 뒤처진다. 실측: BTC 문서 15,871건 대 집계 1,041건.

집계는 파생 테이블이라 언제 다시 만들어도 같은 값이 나온다. 잃을 것이 없으므로
의심스러우면 그냥 돌리면 된다.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.conn import get_conn
from app.db.dao import refresh_sentiment_daily


def _counts(conn, code: str) -> tuple[int, int]:
    """(문서, 집계). healthcheck 5번과 같은 기준이어야 판정이 엇갈리지 않는다."""
    docs = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE code = ? AND published_kst_date IS NOT NULL "
        "AND published_utc IS NOT NULL", (code,)).fetchone()[0]
    agg = conn.execute(
        "SELECT COALESCE(SUM(doc_cnt), 0) FROM sentiment_daily WHERE code = ?", (code,)).fetchone()[0]
    return docs, agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*", help="생략하면 활성 엔티티 전체")
    ap.add_argument("--verify", action="store_true", help="재집계 없이 차이만 보고")
    args = ap.parse_args()

    with get_conn() as conn:
        codes = args.codes or [r["code"] for r in conn.execute(
            "SELECT code FROM entities WHERE is_active = 1 ORDER BY code")]
        if not codes:
            raise SystemExit("활성 엔티티가 없다. python -m scripts.seed_entities 를 먼저 실행하라.")

        stale, moved = [], 0
        for code in codes:
            before_docs, before_agg = _counts(conn, code)
            if args.verify:
                if before_docs != before_agg:
                    stale.append((code, before_docs, before_agg))
                continue

            refresh_sentiment_daily(conn, code)
            after_docs, after_agg = _counts(conn, code)
            delta = after_agg - before_agg
            if delta:
                moved += 1
                print(f"  {code:<10} 집계 {before_agg:>7,} -> {after_agg:>7,}  ({delta:+,})")
            # 재집계 직후에도 어긋나면 refresh로 못 고치는 결함이다(무시하면 안 된다).
            if after_docs != after_agg:
                stale.append((code, after_docs, after_agg))

        if args.verify:
            print(f"=== 검증 {len(codes)}개 엔티티 ===")
        else:
            print(f"=== 재집계 {len(codes)}개 엔티티 · 변화 {moved}개 ===")

        if stale:
            for code, d, a in stale:
                print(f"  [불일치] {code}: 문서 {d:,} vs 집계 {a:,}")
            if args.verify:
                print("  -> python -m scripts.refresh 로 재집계하라")
            else:
                # 재집계했는데도 남았다면 집계 SQL의 WHERE와 검증 기준이 어긋난 것이다.
                print("  -> 재집계 후에도 남았다. dao.refresh_sentiment_daily의 필터를 의심하라")
            raise SystemExit(1)
        print("  모두 일치")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
