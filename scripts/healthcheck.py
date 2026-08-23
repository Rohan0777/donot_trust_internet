"""파이프라인 상태 점검 — 무엇이 깨져 있는지 한 번에 본다.

  python -m scripts.healthcheck

일상 운영에서 조용히 잘못되기 쉬운 것들을 검사한다. 각 항목은 "이게 깨지면
어떤 숫자가 거짓이 되는가"를 기준으로 골랐다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.conn import get_conn

FAIL, WARN, OK = "FAIL", "WARN", "OK"


def _p(level, title, detail=""):
    mark = {OK: "  OK  ", WARN: " WARN ", FAIL: " FAIL "}[level]
    print(f"[{mark}] {title}" + (f"\n         {detail}" if detail else ""))
    return level


def checks(conn):
    out = []

    # 1. 시각 정규화. 하나라도 깨지면 신호 귀속일이 통째로 밀린다.
    bad = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE published_utc IS NOT NULL "
        "AND published_utc NOT LIKE '%+00:00'").fetchone()[0]
    out.append(_p(OK if not bad else FAIL, "시각이 전부 UTC(+00:00)",
                  "" if not bad else f"{bad:,}건이 UTC가 아니다 — 신호일 계산이 어긋난다"))

    # 2. 인코딩 파손. 구 프로젝트를 무력화시켰던 결함.
    bad = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE title LIKE '%' || char(65533) || '%'").fetchone()[0]
    out.append(_p(OK if not bad else FAIL, "제목 인코딩 정상",
                  "" if not bad else f"U+FFFD 포함 {bad:,}건 — 복원 불가, 재수집 필요"))

    # 3. 프롬프트 버전 혼재. 섞이면 감성지수에 인공적 단절이 생긴다.
    vers = [r["prompt_version"] for r in conn.execute(
        "SELECT DISTINCT prompt_version FROM documents "
        "WHERE label IS NOT NULL AND label_model != 'inherited'") if r["prompt_version"]]
    out.append(_p(OK if len(vers) <= 1 else WARN, "채점 프롬프트 버전 단일",
                  "" if len(vers) <= 1 else f"혼재: {sorted(vers)} — 교체 시점에 지수가 튄다"))

    # 4. 매체 미분류 비중. 높으면 가중치 슬라이더가 무의미해진다.
    row = conn.execute(
        "SELECT COUNT(*) t, SUM(m.tier='unknown') u FROM documents d "
        "JOIN media m ON d.media_id=m.media_id WHERE m.channel='news'").fetchone()
    pct = (row["u"] or 0) / max(row["t"], 1) * 100
    out.append(_p(OK if pct < 20 else WARN, f"매체 등급 미분류 {pct:.1f}%",
                  "" if pct < 20 else "가중치를 바꿔도 차트가 잘 안 움직인다"))

    # 5. 집계 정합성. sentiment_daily가 documents와 어긋나면 화면 전체가 거짓이 된다.
    mismatch = []
    for r in conn.execute("SELECT code FROM entities WHERE is_active=1"):
        c = r["code"]
        a = conn.execute("SELECT COUNT(*) FROM documents WHERE code=? AND published_kst_date "
                         "IS NOT NULL AND published_utc IS NOT NULL", (c,)).fetchone()[0]
        b = conn.execute("SELECT COALESCE(SUM(doc_cnt),0) FROM sentiment_daily WHERE code=?",
                         (c,)).fetchone()[0]
        if a != b:
            mismatch.append(f"{c}: 문서 {a:,} vs 집계 {b:,}")
    out.append(_p(OK if not mismatch else WARN, "사전집계가 문서와 일치",
                  "" if not mismatch else "; ".join(mismatch[:4]) + "  -> refresh 필요"))

    # 6. 가격 결손. 감성만 있고 가격이 없으면 상관분석에서 조용히 빠진다.
    nopx = [r["code"] for r in conn.execute(
        "SELECT e.code FROM entities e WHERE e.is_active=1 AND e.priority=1 "
        "AND NOT EXISTS (SELECT 1 FROM prices p WHERE p.code=e.code)")]
    out.append(_p(OK if not nopx else WARN, "상시 대상 가격 확보",
                  "" if not nopx else f"가격 없음: {', '.join(nopx)} — 시차 상관에서 제외됨"))

    # 7. 최근 수집. 스케줄러가 멎으면 여기서 드러난다.
    row = conn.execute("SELECT MAX(kst_date) d FROM coverage").fetchone()
    last = row["d"] if row else None
    from datetime import date
    gap = (date.today() - date.fromisoformat(last)).days if last else 999
    out.append(_p(OK if gap <= 2 else WARN, f"최근 수집일 {last} ({gap}일 전)",
                  "" if gap <= 2 else "스케줄러가 멎었는지 확인하라"))

    # 8. 미채점 잔량.
    row = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE label IS NULL AND is_canonical=1").fetchone()[0]
    out.append(_p(OK if row < 5000 else WARN, f"미채점 대표글 {row:,}건",
                  "" if row < 5000 else "채점이 수집을 못 따라가고 있다"))

    return out


def main():
    print("=== 파이프라인 상태 점검 ===\n")
    with get_conn() as conn:
        res = checks(conn)
    n_fail = res.count(FAIL)
    n_warn = res.count(WARN)
    print(f"\n  통과 {res.count(OK)} · 경고 {n_warn} · 실패 {n_fail}")
    sys.exit(2 if n_fail else (1 if n_warn else 0))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
