"""매체 등급 매핑 적용 + 한글명/도메인 이중키 병합.

수집 경로에 따라 같은 언론사가 두 개의 키로 들어온다:
  naver_news(스크래핑) -> '연합뉴스'      (한글 매체명)
  naver_api_news(API)  -> 'yna.co.kr'     (등록 도메인)
등급만 맞춰서는 매체별 차트가 한 언론사를 둘로 쪼개 보여준다. 여기서 하나의
media_id로 병합하고 documents를 재연결한다.

  python -m scripts.map_media                 # 적용
  python -m scripts.map_media --dry-run       # 변경 없이 계획만 출력
  python -m scripts.map_media --report        # 현재 매핑 상태만 조회
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.conn import get_conn, init_db
from app.db.dao import refresh_sentiment_daily

CSV_PATH = Path(__file__).resolve().parent.parent / "docs" / "media_tiers.csv"


def load_rules(path: Path) -> list[tuple[str, str, list[str]]]:
    rules = []
    with path.open(encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0].lstrip().startswith("#") or row[0].strip() == "tier":
                continue
            tier, canonical, aliases = row[0].strip(), row[1].strip(), row[2].strip()
            names = [a.strip() for a in aliases.split("|") if a.strip()]
            if canonical not in names:
                names.insert(0, canonical)
            rules.append((tier, canonical, names))
    return rules


def report(conn):
    print("=== 등급별 매체/문서 분포 ===")
    for r in conn.execute(
        "SELECT m.tier, COUNT(DISTINCT m.media_id) media, COUNT(d.doc_id) docs "
        "FROM media m LEFT JOIN documents d ON d.media_id = m.media_id "
        "WHERE m.channel = 'news' GROUP BY m.tier ORDER BY docs DESC"
    ):
        print(f"  {r['tier']:<9} 매체 {r['media']:>4}곳   문서 {r['docs']:>7,}건")

    total = conn.execute(
        "SELECT COUNT(*) FROM documents d JOIN media m ON d.media_id = m.media_id "
        "WHERE m.channel = 'news'").fetchone()[0]
    unk = conn.execute(
        "SELECT COUNT(*) FROM documents d JOIN media m ON d.media_id = m.media_id "
        "WHERE m.channel = 'news' AND m.tier = 'unknown'").fetchone()[0]
    # '미상 언론사'는 레거시 데이터의 press가 NULL이라 구조적으로 매핑 불가하다.
    # 매핑 노력으로 줄일 수 있는 분모와 구분해서 보고한다.
    nameless = conn.execute(
        "SELECT COUNT(*) FROM documents d JOIN media m ON d.media_id = m.media_id "
        "WHERE m.name = '미상 언론사'").fetchone()[0]
    fixable = unk - nameless
    print(f"\n  전체 뉴스 문서      {total:>7,}건")
    print(f"  미분류              {unk:>7,}건 ({unk/max(total,1)*100:.1f}%)")
    print(f"   ├ 매체명 자체가 없음 {nameless:>6,}건  <- 레거시 결함, 재수집 전엔 매핑 불가")
    print(f"   └ 매핑 가능 잔량    {fixable:>7,}건 ({fixable/max(total,1)*100:.1f}%)")


def apply_rules(conn, rules, dry_run: bool = False) -> dict:
    stats = {"merged": 0, "retiered": 0, "created": 0, "docs_repointed": 0}
    for tier, canonical, names in rules:
        rows = conn.execute(
            f"SELECT media_id, name, tier FROM media WHERE channel = 'news' AND name IN "
            f"({','.join('?' * len(names))}) ORDER BY media_id", names
        ).fetchall()
        if not rows:
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO media(name, domain, tier, channel) VALUES (?,?,?,'news')",
                    (canonical, canonical if "." in canonical else None, tier),
                )
            stats["created"] += 1
            continue

        keep = rows[0]["media_id"]
        dupes = [r["media_id"] for r in rows[1:]]
        domain = next((r["name"] for r in rows if "." in r["name"]), None)

        if dupes and not dry_run:
            marks = ",".join("?" * len(dupes))
            n = conn.execute(
                f"UPDATE documents SET media_id = ? WHERE media_id IN ({marks})", [keep] + dupes
            ).rowcount
            conn.execute(f"DELETE FROM media WHERE media_id IN ({marks})", dupes)
            stats["docs_repointed"] += n
        if dupes:
            stats["merged"] += len(dupes)

        if not dry_run:
            conn.execute("UPDATE media SET name = ?, tier = ?, domain = COALESCE(?, domain) "
                         "WHERE media_id = ?", (canonical, tier, domain, keep))
        if any(r["tier"] != tier for r in rows):
            stats["retiered"] += 1
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=CSV_PATH)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    init_db()
    with get_conn() as conn:
        if args.report:
            report(conn)
            return
        rules = load_rules(args.csv)
        print(f"규칙 {len(rules)}건 로드 ({args.csv.name})")
        stats = apply_rules(conn, rules, args.dry_run)
        if args.dry_run:
            conn.rollback()
            print("[dry-run] 롤백됨")
        else:
            for code in [r["code"] for r in conn.execute("SELECT code FROM entities WHERE is_active = 1")]:
                refresh_sentiment_daily(conn, code)
        print(f"  병합된 중복 매체 {stats['merged']}곳 / 재연결 문서 {stats['docs_repointed']:,}건 / "
              f"신규 등록 {stats['created']}곳 / 등급 변경 {stats['retiered']}곳\n")
        report(conn)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
