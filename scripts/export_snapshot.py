"""서빙용 스냅샷 내보내기 — 수집과 사이트 운영을 완전히 분리한다.

  python -m scripts.export_snapshot                 # data/serve.db 생성
  python -m scripts.export_snapshot --out X.db      # 경로 지정
  python -m scripts.export_snapshot --verify        # 생성물 검증만

수집 DB(tni.db)에는 문서 원본이 수십만 건 들어 있지만, 웹은 그중 아무것도 읽지
않는다. 화면에 필요한 것은 사전집계(sentiment_daily)와 가격뿐이다. 그래서 그
둘만 담은 작은 파일을 만들어 웹 호스트에 복사하면:

  - 수집이 몇 시간 돌아도 사이트 응답에 영향이 없다 (파일이 분리돼 있으므로)
  - 노트북이 꺼져 있어도 사이트는 산다
  - 웹 호스트에 원문·URL·작성자가 아예 존재하지 않는다 (유출면 축소)

documents는 통째로 제외한다. 매체별 차트는 sentiment_daily가 media_id 단위로
접혀 있어 그것만으로 그려진다. 다만 '이 지수가 몇 %의 문서로 만들어졌는가'는
documents 없이는 알 수 없으므로, 내보내기 시점에 label_coverage로 접어 넣는다.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DATA_DIR, DB_PATH
from app.db.conn import get_conn
from app.db.dao import COVERAGE_TABLE, compute_label_coverage

DEFAULT_OUT = DATA_DIR / "serve.db"

# 서빙에 필요한 테이블만. documents / raw_documents / coverage / pipeline_runs 제외.
TABLES = ("entities", "media", "prices", "sentiment_daily", "fee_schedule")

VIEW_HEAD_RE = re.compile(
    r'^(CREATE\s+(?:UNIQUE\s+)?(?:INDEX|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?)', re.I)


def export(src: Path, out: Path, progress=print) -> dict:
    if out.exists():
        out.unlink()
    tmp = out.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()

    stats = {}
    with get_conn(src) as conn:
        conn.execute("ATTACH DATABASE ? AS snap", (str(tmp),))
        for t in TABLES:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if not ddl or not ddl["sql"]:
                progress(f"  [건너뜀] {t}: 원본에 없음")
                continue
            # ALTER TABLE ... RENAME을 거친 테이블은 sqlite_master에 이름이
            # 따옴표로 감싸져 저장된다(`CREATE TABLE "entities" (...)`). 단순
            # 문자열 치환으로는 안 잡히므로 정규식으로 스키마 접두어를 붙인다.
            create = re.sub(
                r'^(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(["\[`]?)' + re.escape(t) + r'\2',
                lambda m: f"{m.group(1)}snap.{t}", ddl["sql"], count=1, flags=re.I)
            if "snap." not in create:
                progress(f"  [건너뜀] {t}: DDL 파싱 실패")
                continue
            conn.execute(create)
            conn.execute(f"INSERT INTO snap.{t} SELECT * FROM main.{t}")
            stats[t] = conn.execute(f"SELECT COUNT(*) FROM snap.{t}").fetchone()[0]

        # 채점 커버리지는 documents 를 접어서 만든다. 스냅샷에는 documents 가 없으므로
        # 여기서 미리 계산해 넣지 않으면 사이트가 "이 지수가 몇 %의 문서로 만들어졌는지"를
        # 영영 알 수 없다 — 라벨률 2%인 시장과 99%인 시장이 같은 표에 나란히 선다.
        cov = compute_label_coverage(conn)
        conn.execute(f"CREATE TABLE snap.{COVERAGE_TABLE} ("
                     "code TEXT PRIMARY KEY, canonical INTEGER, labeled INTEGER)")
        conn.executemany(
            f"INSERT INTO snap.{COVERAGE_TABLE}(code, canonical, labeled) VALUES (?,?,?)",
            [(r["code"], r["canonical"], r["labeled"]) for r in cov])
        stats[COVERAGE_TABLE] = len(cov)

        # 조회 인덱스와 tier 롤업 뷰는 서빙에 직접 쓰이므로 함께 옮긴다.
        for row in conn.execute(
            "SELECT sql, name, type FROM sqlite_master "
            "WHERE type IN ('index','view') AND sql IS NOT NULL"
        ):
            name, sql = row["name"], row["sql"]
            if not any(t in sql for t in TABLES):
                continue
            # CREATE VIEW/INDEX 키워드 바로 뒤에 스키마 접두어만 끼워 넣는다.
            # 이름이 따옴표로 감싸져 있든 아니든(ALTER RENAME을 거치면 그렇다)
            # 뒤쪽은 손대지 않으므로 인용 형태를 따질 필요가 없다.
            try:
                conn.execute(VIEW_HEAD_RE.sub(r'\g<1>snap.', sql, count=1))
            except Exception as exc:  # noqa: BLE001 - 의존 누락은 조용히 넘기지 않는다
                progress(f"  [건너뜀] {row['type']} {name}: {type(exc).__name__}")
                continue

        # DETACH는 열린 트랜잭션이 남아 있으면 "database is locked"로 실패한다.
        conn.commit()
        conn.execute("DETACH DATABASE snap")

    # 원자적 교체: 웹이 반쯤 쓰인 파일을 읽는 상황을 막는다.
    shutil.move(str(tmp), str(out))
    stats["_bytes"] = out.stat().st_size
    return stats


def verify(out: Path, progress=print) -> bool:
    if not out.exists():
        progress(f"  스냅샷이 없습니다: {out}")
        return False
    ok = True
    with get_conn(out) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in TABLES:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in names else 0
            progress(f"  {t:<18}{n:>9,}행" + ("" if t in names else "   [없음]"))
            if t in ("entities", "sentiment_daily", "prices") and not n:
                ok = False
        # 원문이 새어 나가지 않았는지 확인한다 — 이 스냅샷의 존재 이유 중 하나다.
        leaked = names & {"documents", "raw_documents"}
        if leaked:
            progress(f"  [경고] 원문 테이블이 포함됨: {leaked}")
            ok = False
        else:
            progress("  원문 테이블 미포함 확인 (documents / raw_documents)")
        if COVERAGE_TABLE in names:
            n = conn.execute(f"SELECT COUNT(*) FROM {COVERAGE_TABLE}").fetchone()[0]
            progress(f"  {COVERAGE_TABLE:<18}{n:>9,}행")
        else:
            progress(f"  [경고] {COVERAGE_TABLE} 누락 — 화면에 채점률이 표시되지 않는다")
            ok = False
        try:
            conn.execute("SELECT * FROM sentiment_daily_tier LIMIT 1").fetchall()
            progress("  tier 롤업 뷰 동작 확인")
        except Exception as exc:  # noqa: BLE001
            progress(f"  [경고] tier 뷰 실패: {type(exc).__name__}")
            ok = False
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DB_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if args.verify:
        sys.exit(0 if verify(args.out) else 1)

    src_mb = args.src.stat().st_size / 1e6
    st = export(args.src, args.out)
    print(f"=== 스냅샷 생성: {args.out} ===")
    for k, v in st.items():
        if k != "_bytes":
            print(f"  {k:<18}{v:>9,}행")
    print(f"\n  수집 DB {src_mb:>8.1f} MB -> 서빙 DB {st['_bytes']/1e6:>6.1f} MB "
          f"({st['_bytes']/1e6/src_mb*100:.1f}%)")
    print()
    verify(args.out)
    print(f"\n  웹에서 사용:  set TNI_DB={args.out}  후  python -m scripts.serve")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
