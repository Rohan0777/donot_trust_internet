"""'미상 언론사' 문서의 매체 정보를 URL에서 복원한다 (네트워크 요청 없음).

레거시 005930 데이터는 press가 NULL인 채로 넘어와 4,975건이 '미상 언론사'에 묶였다.
그런데 URL 자체가 매체 식별자를 담고 있다:

  naver_news     https://n.news.naver.com/mnews/article/001/0016179110
                                                        ^^^ 네이버 언론사 코드(oid)
  naver_api_news http://amenews.kr/news/view.php?idx=67464
                        ^^^^^^^^^^ 등록 도메인

oid -> 언론사명 사전은 press가 정상인 다른 종목 데이터에서 역으로 만든다.
따라서 외부 호출이 전혀 필요 없고, 재수집 없이 복구된다.

  python -m scripts.recover_press [--dry-run]
"""
import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors.naver_api_search import registrable_domain
from app.db.conn import get_conn, init_db
from app.db.dao import refresh_sentiment_daily, resolve_media_id

OID_RE = re.compile(r"/article/(\d+)/")
PLACEHOLDER = "미상 언론사"


def build_oid_index(conn) -> dict[str, tuple[str, str]]:
    """press가 정상인 문서에서 oid -> (매체명, 등급) 사전을 만든다."""
    index: dict[str, tuple[str, str]] = {}
    for r in conn.execute(
        "SELECT m.name, m.tier, d.url FROM documents d JOIN media m ON d.media_id = m.media_id "
        "WHERE m.name != ? AND m.channel = 'news' AND d.url LIKE '%/article/%'", (PLACEHOLDER,)
    ):
        o = OID_RE.search(r["url"] or "")
        if o:
            index.setdefault(o.group(1), (r["name"], r["tier"]))
    return index


def recover(conn, dry_run: bool = False) -> dict:
    oid_index = build_oid_index(conn)
    rows = conn.execute(
        "SELECT d.doc_id, d.url, d.source FROM documents d JOIN media m ON d.media_id = m.media_id "
        "WHERE m.name = ?", (PLACEHOLDER,)
    ).fetchall()

    stats = {"total": len(rows), "by_oid": 0, "by_domain": 0, "unresolved": 0,
             "oid_dict_size": len(oid_index)}
    cache: dict[tuple[str, str], int] = {}

    for r in rows:
        url = r["url"] or ""
        name = tier = None

        o = OID_RE.search(url)
        if o and o.group(1) in oid_index:
            name, tier = oid_index[o.group(1)]
            stats["by_oid"] += 1
        else:
            host = urlparse(url).netloc
            if host:
                name, tier = registrable_domain(host), "unknown"
                stats["by_domain"] += 1
            else:
                stats["unresolved"] += 1
                continue

        if dry_run:
            continue
        key = (name, tier)
        if key not in cache:
            cache[key] = resolve_media_id(conn, name, "news", tier)
        conn.execute("UPDATE documents SET media_id = ? WHERE doc_id = ?", (cache[key], r["doc_id"]))

    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    init_db()
    with get_conn() as conn:
        st = recover(conn, args.dry_run)
        if args.dry_run:
            conn.rollback()
        else:
            for code in [x["code"] for x in conn.execute("SELECT code FROM entities WHERE is_active = 1")]:
                refresh_sentiment_daily(conn, code)

        print(f"oid 사전 {st['oid_dict_size']}개로 복원 시도")
        print(f"  대상            {st['total']:>6,}건")
        print(f"  oid로 복원      {st['by_oid']:>6,}건")
        print(f"  도메인으로 복원 {st['by_domain']:>6,}건")
        print(f"  복원 실패       {st['unresolved']:>6,}건")
        if args.dry_run:
            print("[dry-run] 롤백됨")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
