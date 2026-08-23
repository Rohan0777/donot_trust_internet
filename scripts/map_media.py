"""매체 등급 매핑 적용 + 한글명/도메인 이중키 병합.

수집 경로에 따라 같은 언론사가 두 개의 키로 들어온다:
  naver_news(스크래핑) -> '연합뉴스'      (한글 매체명)
  naver_api_news(API)  -> 'yna.co.kr'     (등록 도메인)
등급만 맞춰서는 매체별 차트가 한 언론사를 둘로 쪼개 보여준다. 여기서 하나의
media_id로 병합하고 documents를 재연결한다.

매칭은 매체명 완전일치 + **등록도메인 일치** 두 갈래다. 서브도메인 변형을 별칭에
일일이 적는 것은 지는 싸움이다 — 같은 언론사가 stock.mk.co.kr / m.newsprime.co.kr /
fr.tradingview.com 처럼 끝없이 갈라진다. 등록도메인(mk.co.kr)까지 접어서 매칭하면
새 서브도메인이 나타나도 CSV를 고칠 일이 없다.

CSV 4번째 컬럼은 channel이다(생략 시 news). 브로커·거래소·자동생성 리서치는
언론사가 아니므로 news 분모에서 아예 빼야 한다. 등급만 낮추는 것으로는 부족하다 —
"뉴스 문서 중 미분류 비중" 같은 지표가 계속 거짓이 되기 때문이다.

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

# co.kr / or.kr / com.au 처럼 2단계짜리 국가 SLD는 한 칸 더 봐야 등록도메인이 나온다.
_SLD = {"co", "or", "ne", "go", "ac", "com", "net", "org", "gov", "edu"}


def regdom(s: str | None) -> str | None:
    """등록도메인만 남긴다. 'stock.mk.co.kr' -> 'mk.co.kr'. 도메인이 아니면 None."""
    s = (s or "").strip().lower().rstrip(".")
    if not s or " " in s or "/" in s or "." not in s:
        return None
    p = s.split(".")
    if len(p) < 2:
        return None
    if len(p) >= 3 and p[-2] in _SLD:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def load_rules(path: Path) -> list[tuple[str, str, list[str], str]]:
    """(tier, canonical, names, channel). 4번째 컬럼은 선택이며 기본은 news."""
    rules = []
    with path.open(encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0].lstrip().startswith("#") or row[0].strip() == "tier":
                continue
            tier, canonical, aliases = row[0].strip(), row[1].strip(), row[2].strip()
            channel = row[3].strip() if len(row) > 3 and row[3].strip() else "news"
            names = [a.strip() for a in aliases.split("|") if a.strip()]
            if canonical not in names:
                names.insert(0, canonical)
            rules.append((tier, canonical, names, channel))
    return rules


def report(conn):
    print("=== 등급별 매체/문서 분포 ===")
    for r in conn.execute(
        "SELECT m.tier, COUNT(DISTINCT m.media_id) media, COUNT(d.doc_id) docs "
        "FROM media m LEFT JOIN documents d ON d.media_id = m.media_id "
        "WHERE m.channel = 'news' GROUP BY m.tier ORDER BY docs DESC"
    ):
        print(f"  {r['tier']:<9} 매체 {r['media']:>4}곳   문서 {r['docs']:>7,}건")

    nonnews = conn.execute(
        "SELECT m.channel, COUNT(DISTINCT m.media_id) media, COUNT(d.doc_id) docs "
        "FROM media m LEFT JOIN documents d ON d.media_id = m.media_id "
        "WHERE m.channel <> 'news' GROUP BY m.channel ORDER BY docs DESC").fetchall()
    if nonnews:
        print()
        print("=== news 분모 밖 (별도 채널) ===")
        for r in nonnews:
            print(f"  {r['channel']:<9} 매체 {r['media']:>4}곳   문서 {r['docs']:>7,}건")

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
    stats = {"merged": 0, "retiered": 0, "created": 0, "docs_repointed": 0, "rechanneled": 0}

    # 매체는 수천 행뿐이라 통째로 올려놓고 파이썬에서 접는다. 등록도메인 매칭을
    # SQL로 표현하려면 도메인 파싱을 SQL에 심어야 하는데, 그쪽이 훨씬 깨지기 쉽다.
    rows = conn.execute("SELECT media_id, name, domain, tier, channel FROM media").fetchall()
    by_name: dict[str, list] = {}
    by_dom: dict[str, list] = {}
    for r in rows:
        by_name.setdefault((r["name"] or "").strip(), []).append(r)
        for key in {regdom(r["domain"]), regdom(r["name"])} - {None}:
            by_dom.setdefault(key, []).append(r)

    # 한 매체를 두 규칙이 가져가면 뒤 규칙이 앞 규칙의 병합을 되돌린다. 선착순으로 막는다.
    claimed: set[int] = set()

    for tier, canonical, names, channel in rules:
        # news에 잘못 앉아 있는 브로커/자동생성 매체를 끌어와 제 채널로 옮겨야 하므로
        # 대상 채널뿐 아니라 news도 후보에 넣는다.
        allowed = {"news", channel}
        doms = {d for d in (regdom(n) for n in names) if d}

        cand: dict[int, object] = {}
        for n in names:
            for r in by_name.get(n, []):
                cand[r["media_id"]] = r
        for d in doms:
            for r in by_dom.get(d, []):
                cand[r["media_id"]] = r

        # media_id 오름차순 = 가장 먼저 등장한 행을 대표로 남긴다.
        hits = [r for mid, r in sorted(cand.items())
                if r["channel"] in allowed and mid not in claimed]

        if not hits:
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO media(name, domain, tier, channel) VALUES (?,?,?,?)",
                    (canonical, canonical if "." in canonical else None, tier, channel),
                )
            stats["created"] += 1
            continue

        claimed.update(r["media_id"] for r in hits)
        keep = hits[0]["media_id"]
        dupes = [r["media_id"] for r in hits[1:]]
        # 도메인은 CSV 별칭에서 먼저 고른다. 큐레이션된 값이 수집물보다 믿을 만하다.
        domain = (next((n for n in names if "." in n), None)
                  or next((r["domain"] for r in hits if r["domain"]), None))

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
            # 개명 전에 (canonical, channel)을 이미 점유한 행을 흡수한다. 남겨두면
            # UNIQUE(name, channel)에 걸려 규칙 하나가 전체 적용을 중단시킨다.
            squat = [r[0] for r in conn.execute(
                "SELECT media_id FROM media WHERE name = ? AND channel = ? AND media_id <> ?",
                (canonical, channel, keep))]
            if squat:
                marks = ",".join("?" * len(squat))
                stats["docs_repointed"] += conn.execute(
                    f"UPDATE documents SET media_id = ? WHERE media_id IN ({marks})",
                    [keep] + squat).rowcount
                conn.execute(f"DELETE FROM media WHERE media_id IN ({marks})", squat)
                stats["merged"] += len(squat)
            conn.execute(
                "UPDATE media SET name = ?, tier = ?, channel = ?, domain = COALESCE(?, domain) "
                "WHERE media_id = ?", (canonical, tier, channel, domain, keep))
        if any(r["tier"] != tier for r in hits):
            stats["retiered"] += 1
        if any(r["channel"] != channel for r in hits):
            stats["rechanneled"] += 1
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
        # dry-run도 실제로 쓴 뒤 리포트하고 마지막에 되돌린다. 롤백 후에 리포트하면
        # 적용 전 상태가 출력돼 "무엇이 달라지는가"를 볼 수 없다.
        stats = apply_rules(conn, rules, dry_run=False)
        if not args.dry_run:
            for code in [r["code"] for r in conn.execute("SELECT code FROM entities WHERE is_active = 1")]:
                refresh_sentiment_daily(conn, code)
        print(f"  병합된 중복 매체 {stats['merged']}곳 / 재연결 문서 {stats['docs_repointed']:,}건 / "
              f"신규 등록 {stats['created']}곳 / 등급 변경 {stats['retiered']}곳 / "
              f"채널 이동 {stats['rechanneled']}곳\n")
        report(conn)
        if args.dry_run:
            conn.rollback()
            print()
            print("[dry-run] 위 상태는 적용 후 예상치다. DB는 롤백되어 그대로다.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
