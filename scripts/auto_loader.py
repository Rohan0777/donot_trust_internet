"""삼성전자(005930) 및 SK하이닉스(000660) 백그라운드 순차 채점 및 데이터 적재 데몬."""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from app.db.conn import get_conn, init_db
from app.db.dao import RunLogger, refresh_sentiment_daily
from app.scoring.scorer import score_pending
from scripts.collect import Tee
from scripts.export_snapshot import export, DEFAULT_OUT

LOG_FILE = BASE_DIR / "logs" / "auto_loader.log"
LOG_FILE.parent.mkdir(exist_ok=True)
log = Tee(LOG_FILE)

TARGETS = [
    {"code": "005930", "name": "삼성전자"},
    {"code": "000660", "name": "SK하이닉스"}
]

def run_pipeline():
    init_db()
    log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [시작] 백그라운드 데이터 적재 시작: 삼성전자 & SK하이닉스")

    for item in TARGETS:
        code = item["code"]
        name = item["name"]
        log(f"\n========================================================")
        log(f">> [{code}] {name} 채점 및 데이터 적재 시작")
        log(f"========================================================")

        # 1. 뉴스 채널 전수 채점 (공식 기사 여론)
        log(f"\n[1단계: {name} 뉴스 채널 채점]")
        try:
            with get_conn() as conn:
                st_news = score_pending(
                    conn, code, name,
                    channel="news",
                    daily_cap=None,  # 뉴스는 전수 채점
                    progress=log
                )
                conn.commit()
                refresh_sentiment_daily(conn, code)
                conn.commit()
            log(f"[1단계 완료] {name} 뉴스 채점 결과: {st_news.scored}건 완료, {st_news.inherited}건 상속")
        except Exception as e:
            log(f"[1단계 오류] {name} 뉴스 채점 중 예외: {e}")

        # 2. 커뮤니티(종토방) 채널 채점 (일일 상한 200건 적용 - 실측 오차 0.037 최적화)
        log(f"\n[2단계: {name} 커뮤니티/종토방 채널 채점]")
        try:
            with get_conn() as conn:
                st_comm = score_pending(
                    conn, code, name,
                    channel="community",
                    daily_cap=200,   # 일별 상한 200건으로 대표 여론 집중 채점
                    progress=log
                )
                conn.commit()
                refresh_sentiment_daily(conn, code)
                conn.commit()
            log(f"[2단계 완료] {name} 커뮤니티 채점 결과: {st_comm.scored}건 완료, {st_comm.inherited}건 상속")
        except Exception as e:
            log(f"[2단계 오류] {name} 커뮤니티 채점 중 예외: {e}")

        # 3. 카페/블로그 잔여 채점
        log(f"\n[3단계: {name} 카페 채널 채점]")
        try:
            with get_conn() as conn:
                st_cafe = score_pending(
                    conn, code, name,
                    channel="cafe",
                    daily_cap=100,
                    progress=log
                )
                conn.commit()
                refresh_sentiment_daily(conn, code)
                conn.commit()
            log(f"[3단계 완료] {name} 카페 채점 결과: {st_cafe.scored}건 완료, {st_cafe.inherited}건 상속")
        except Exception as e:
            log(f"[3단계 오류] {name} 카페 채점 중 예외: {e}")

        # 해당 종목 집계 갱신
        with get_conn() as conn:
            refresh_sentiment_daily(conn, code)
            conn.commit()
        log(f"[완료] [{code}] {name} 사전집계(sentiment_daily) 갱신 완료!")

    # 4. 서빙용 스냅샷 (serve.db) 자동 내보내기
    log(f"\n========================================================")
    log(f">> 서빙용 스냅샷(serve.db) 동기화 시작")
    log(f"========================================================")
    try:
        from app.config import DB_PATH
        stats = export(DB_PATH, DEFAULT_OUT, progress=log)
        log(f"[성공] serve.db 스냅샷 동기화 완료: {stats}")
    except Exception as e:
        log(f"[오류] serve.db 스냅샷 내보내기 실패: {e}")

    log(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [종료] 전체 백그라운드 적재 파이프라인 정상 종료!")

if __name__ == "__main__":
    run_pipeline()
