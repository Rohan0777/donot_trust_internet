"""관측 대상(entities) 등록/갱신.

  python -m scripts.seed_entities            # docs/entities.csv 적용
  python -m scripts.seed_entities --list     # 현재 등록 상태 조회
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.conn import get_conn, init_db

CSV_PATH = Path(__file__).resolve().parent.parent / "docs" / "entities.csv"

# 수집 로케일 정의. Google News RSS의 hl/gl/ceid 조합.
LOCALES = {
    "ko": [["ko", "KR", "KR:ko"]],
    "en": [["en-US", "US", "US:en"]],
    "ko+en": [["ko", "KR", "KR:ko"], ["en-US", "US", "US:en"]],
}


def load(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0].lstrip().startswith("#") or row[0].strip() == "code":
                continue
            code, kind, name, calendar, priority, locales, aliases, exclude = (row + [""] * 8)[:8]
            out.append({
                "code": code.strip(), "kind": kind.strip(), "name": name.strip(),
                "calendar": calendar.strip(), "priority": int(priority or 2),
                "locales_json": json.dumps(LOCALES.get(locales.strip(), LOCALES["ko"])),
                "aliases_json": json.dumps([a.strip() for a in aliases.split("|") if a.strip()],
                                           ensure_ascii=False),
                "exclude_json": json.dumps([e.strip() for e in exclude.split("|") if e.strip()],
                                           ensure_ascii=False) if exclude.strip() else None,
            })
    return out


def show(conn):
    print(f"  {'code':<10}{'kind':<11}{'name':<14}{'달력':<8}{'우선':>5}{'로케일':>10}{'문서':>9}")
    for r in conn.execute(
        "SELECT e.code, e.kind, e.name, e.calendar, e.priority, e.locales_json, e.is_active,"
        " (SELECT COUNT(*) FROM documents d WHERE d.code = e.code) docs "
        "FROM entities e ORDER BY e.priority, e.kind, e.code"
    ):
        loc = "?" if not r["locales_json"] else "+".join(
            l[0][:2] for l in json.loads(r["locales_json"]))
        flag = "" if r["is_active"] else "  (비활성)"
        print(f"  {r['code']:<10}{r['kind']:<11}{r['name']:<14}{r['calendar']:<8}"
              f"{r['priority']:>5}{loc:>10}{r['docs']:>9,}{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=CSV_PATH)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    init_db()
    with get_conn() as conn:
        if args.list:
            show(conn)
            return
        rows = load(args.csv)
        for r in rows:
            conn.execute(
                "INSERT INTO entities(code, kind, name, calendar, priority, locales_json,"
                " aliases_json, exclude_json, is_active) VALUES (?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(code) DO UPDATE SET kind=excluded.kind, name=excluded.name,"
                " calendar=excluded.calendar, priority=excluded.priority,"
                " locales_json=excluded.locales_json, aliases_json=excluded.aliases_json,"
                " exclude_json=excluded.exclude_json, is_active=1",
                (r["code"], r["kind"], r["name"], r["calendar"], r["priority"],
                 r["locales_json"], r["aliases_json"], r["exclude_json"]),
            )
        print(f"{len(rows)}개 등록/갱신 ({args.csv.name})\n")
        show(conn)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
