"""파이프라인 통합 상태 대시보드.

모든 자산(신규 자산 포함)의:
1. 가격 데이터 (10년치 일자 범위, 일수)
2. 문서 수집 (총 문서 수, 일자 범위)
3. 감성 채점 완료율 (%) 및 영상 생성 준비도
를 한눈에 표 형식으로 출력한다.
"""
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

db_path = Path("D:/dev/trust-no-internet/data/tni.db")
if not db_path.exists():
    print(f"DB 파일을 찾을 수 없습니다: {db_path}")
    sys.exit(1)

conn = sqlite3.connect(db_path)

print("=" * 96)
print(f"{'코드':<8} {'자산명':<8} {'구분':<6} | {'가격일수':>6} | {'문서수집':>8} | {'채점완료':>8} ({'완료율':>6}) | {'영상 제작 준비도':<16}")
print("=" * 96)

entities = conn.execute("SELECT code, name, kind, calendar FROM entities ORDER BY priority, kind, code").fetchall()

for code, name, kind, cal in entities:
    # 1. 가격 확인
    p_row = conn.execute("SELECT count(*), min(kst_date), max(kst_date) FROM prices WHERE code=?", (code,)).fetchone()
    p_cnt = p_row[0] if p_row else 0

    # 2. 문서 확인
    d_row = conn.execute("SELECT count(*), sum(label IS NOT NULL), min(published_kst_date), max(published_kst_date) FROM documents WHERE code=?", (code,)).fetchone()
    d_cnt = d_row[0] if d_row else 0
    s_cnt = d_row[1] if (d_row and d_row[1]) else 0

    pct = (s_cnt / d_cnt * 100) if d_cnt else 0.0

    # 준비도 판정
    if p_cnt >= 2000 and pct >= 70.0:
        readiness = "🟢 10년 영상 즉시 제작 가능"
    elif p_cnt >= 2000 and d_cnt > 0:
        readiness = f"🟡 채점 진행 중 ({pct:4.1f}%)"
    elif p_cnt >= 2000:
        readiness = "🔵 가격완료 / 뉴스수집 중"
    else:
        readiness = "⚪ 준비 중"

    print(f"{code:<8} {name:<8} {kind:<6} | {p_cnt:>6,}일 | {d_cnt:>8,}건 | {s_cnt:>8,}건 ({pct:5.1f}%) | {readiness:<16}")

print("=" * 96)

# 최근 실행 중/완료된 파이프라인 작업
print("\n[최근 파이프라인 실행 이력 (최근 5건)]")
for r in conn.execute("SELECT run_id, stage, code, status, started_utc, finished_utc FROM pipeline_runs ORDER BY started_utc DESC LIMIT 5").fetchall():
    status_str = "진행중" if not r[5] else "완료"
    print(f"  [{status_str:4s}] {r[1]:<10} | 종목: {r[2] or '-':<8} | 시작: {r[4][:19]}")

conn.close()
