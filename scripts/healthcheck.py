"""파이프라인 상태 점검 — 무엇이 깨져 있는지 한 번에 본다.

  python -m scripts.healthcheck

일상 운영에서 조용히 잘못되기 쉬운 것들을 검사한다. 각 항목은 "이게 깨지면
어떤 숫자가 거짓이 되는가"를 기준으로 골랐다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.conn import get_conn
from app.config import NON_OPINION_CHANNELS
from app.db.dao import label_coverage
from app.backtest.cross_market import MIN_LABELED_RATIO

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

    # 8. 미채점 잔량. 가중치 0 채널은 채점하지 않는 것이 정상이므로 분모에서 뺀다 —
    #    넣어두면 영원히 0에 닿지 않아 경고가 상시로 켜지고, 아무도 보지 않게 된다.
    marks = ",".join("?" * len(NON_OPINION_CHANNELS))
    pending = conn.execute(
        "SELECT COUNT(*) FROM documents d JOIN media m ON d.media_id = m.media_id "
        f"WHERE d.label IS NULL AND d.is_canonical=1 AND m.channel NOT IN ({marks})",
        NON_OPINION_CHANNELS).fetchone()[0]
    out.append(_p(OK if pending < 5000 else WARN, f"미채점 대표글 {pending:,}건",
                  "" if pending < 5000 else "채점이 수집을 못 따라가고 있다"))

    # 9. 가격 계열의 단위 혼재. source가 여러 개인 것 자체는 문제가 아니다 —
    #    같은 단위의 소스를 이어붙였을 수 있다(실측 레거시->pykrx 경계 -3.6%/+1.4%로
    #    정상). 진짜 실패는 경계에서 값이 튀는 것이다. UST가 그랬다: 금리(%, 약 4.7)
    #    411행 위에 ETF 가격(달러, 약 93)을 얹으면 하루에 +1,900%가 찍히고 그 하루가
    #    상관계수 전체를 지배한다. 그래서 개수가 아니라 경계 수익률을 잰다.
    JUMP_FAIL, JUMP_WARN = 0.30, 0.15
    jumps = []
    for (code,) in conn.execute(
            "SELECT code FROM prices GROUP BY code HAVING COUNT(DISTINCT source) > 1"):
        rows = conn.execute(
            "SELECT kst_date, close, source FROM prices WHERE code = ? ORDER BY kst_date",
            (code,)).fetchall()
        for a, b in zip(rows, rows[1:]):
            if a["source"] == b["source"] or not a["close"]:
                continue
            r = b["close"] / a["close"] - 1
            if abs(r) >= JUMP_WARN:
                jumps.append((code, a["source"], b["source"], b["kst_date"], r))
    worst = max((abs(j[4]) for j in jumps), default=0.0)
    detail = "; ".join(f"{c} {s1}->{s2} {d} {r*100:+.0f}%" for c, s1, s2, d, r in jumps[:3])
    out.append(_p(FAIL if worst >= JUMP_FAIL else (WARN if jumps else OK),
                  "가격 계열 이어붙인 경계 연속성",
                  detail or ""))

    # 채점 커버리지. 잔량 총량이 작아도 특정 시장만 비어 있으면 그 시장의
    #    감성지수는 "시장"이 아니라 "먼저 채점된 일부"를 재는 것이 된다.
    low = [(c, v) for c, v in label_coverage(conn).items()
           if v["ratio"] is not None and v["ratio"] < MIN_LABELED_RATIO and v["canonical"] >= 500]
    detail = ", ".join(f"{c} {v['ratio']*100:.0f}%" for c, v in
                       sorted(low, key=lambda kv: kv[1]["ratio"]))
    out.append(_p(OK if not low else WARN,
                  f"시장별 채점률 (하한 {MIN_LABELED_RATIO*100:.0f}%)",
                  "" if not low else f"{detail} — 화면에서 '채점 미완'으로 표시됨"))

    # 11. 수집 완전성. 미완결은 partial("끝까지 못 훑음", 대개 응답 상한)과
    #     failed("아예 못 받아옴") 둘 다다. 한 질의가 남긴 partial 을 같은 실행 안의
    #     다른 질의 실패로 failed 가 덮는 일이 있으므로(coverage 병합 순위),
    #     partial 만 세면 그 날짜가 경고에서 통째로 사라진다. 서빙 리본
    #     (dao.incomplete_dates)은 partial+failed 를 다므로 정의를 여기서 어긋나게
    #     두면 화면은 물고 있고 원장 점검만 조용히 통과한다. pending 은 분모에서
    #     제외한다 — 아직 차례가 안 온 날짜까지 세면 초기 백필 중 경고가 항상 켜져
    #     아무도 보지 않게 된다. 비중이 높으면 그 종목의 일별 건수는 여론의 크기가
    #     아니라 상한에 걸린 횟수를 센 것이 된다 — 관여도·확산도가 통째로 거짓이다.
    INCOMPLETE_WARN = 0.20
    rows = conn.execute(
        "SELECT code, source, "
        " SUM(status IN ('partial','failed')) inc, "
        " SUM(status IN ('partial','failed','completed','empty')) tot "
        "FROM coverage GROUP BY code, source HAVING tot >= 30").fetchall()
    bad = [(r["code"], r["source"], r["inc"] / r["tot"], r["tot"])
           for r in rows if r["tot"] and r["inc"] / r["tot"] >= INCOMPLETE_WARN]
    detail = ", ".join(f"{c}/{src} {ratio*100:.0f}%({tot}일)"
                       for c, src, ratio, tot in sorted(bad, key=lambda x: -x[2]))
    out.append(_p(OK if not bad else WARN,
                  f"수집 완전성 (미완결 비중 하한 {INCOMPLETE_WARN*100:.0f}%)",
                  detail or ""))

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
