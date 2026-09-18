"""신규 자산 데이터 수집 및 미채점 자산 감성 채점 통합 오케스트레이터.

주요 역할:
  1. 신규 자산 (NVDA, TSLA, 005380, 086520, 035420, DOGE) 뉴스/감성 수집
  2. 기존 미채점 자산 (035720 카카오, SPX, NASDAQ 등) 감성 채점 순차 진행
  3. 일별 감성 지수 집계(sentiment_daily) 갱신 및 서빙 DB(serve.db) 자동 스냅샷 동기화
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 루트 디렉토리 추가
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.db.conn import get_conn, init_db
from app.db.dao import refresh_sentiment_daily
from app.collectors import google_news_rss, naver_news, market_price, price
from app.scoring.scorer import score_pending
import scripts.export_snapshot as export_snapshot


def log(msg: str, **kwargs):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def collect_new_assets(days: int = 365):
    """신규 KOSPI 자산들의 뉴스 및 가격 데이터를 수집한다."""
    log("=== [1단계] KOSPI 대표 자산 데이터 수집 시작 ===")
    
    new_targets = [
        # (code, name, kind, has_naver_news)
        ("005490", "POSCO홀딩스", "krx", True),
        ("068270", "셀트리온", "krx", True),
        ("000270", "기아", "krx", True),
        ("105560", "KB금융", "krx", True),
        ("012450", "한화에어로스페이스", "krx", True),
    ]
    
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    with get_conn() as conn:
        for code, name, kind, has_naver in new_targets:
            log(f"\n>> [{code}] {name} 뉴스 수집 중 ({start_date} ~ {end_date})")
            
            # 1) Google RSS 수집
            try:
                st_g = google_news_rss.crawl_entity(
                    conn, code, name, start_date, end_date, step_days=32, progress=log
                )
                log(f"   [Google RSS] 저장: {st_g.get('saved', 0):,}건, 기간: {st_g.get('dates', 0)}일")
            except Exception as e:
                log(f"   [Google RSS 오류] {e}")
                
            # 2) 네이버 종목뉴스 수집 (국내 주식)
            if has_naver:
                try:
                    st_n = naver_news.crawl(
                        conn, code, days_back=min(days, 180), max_pages=30, progress=log
                    )
                    log(f"   [Naver News] 저장: {st_n.get('saved', 0):,}건, 페이지: {st_n.get('pages', 0)}p")
                except Exception as e:
                    log(f"   [Naver News 오류] {e}")
                    
            # 3) 감성 집계 갱신
            try:
                refresh_sentiment_daily(conn, code)
                log(f"   [집계 완료] {code} sentiment_daily 갱신 완료")
            except Exception as e:
                log(f"   [집계 오류] {e}")


def score_undercovered_assets(kakao_limit: int = 500, other_limit: int = 300):
    """카카오 및 모자란 자산의 미채점 기사를 채점한다."""
    log("\n=== [2단계] 모자란 자산 감성 채점 시작 ===")
    
    score_targets = [
        ("005490", "POSCO홀딩스", "news", other_limit, 50),
        ("068270", "셀트리온", "news", other_limit, 50),
        ("000270", "기아", "news", other_limit, 50),
        ("105560", "KB금융", "news", other_limit, 50),
        ("012450", "한화에어로스페이스", "news", other_limit, 50),
    ]
    
    with get_conn() as conn:
        for code, name, channel, limit, cap in score_targets:
            log(f"\n>> [{code}] {name} 채점 시작 (상한: {limit}건, 일별 cap: {cap})")
            try:
                stats = score_pending(
                    conn, code, name, limit=limit, channel=channel,
                    daily_cap=cap, progress=log
                )
                log(f"   [채점 완료] 채점: {stats.scored}건, 상속: {stats.inherited}건, 실패: {stats.failed}건")
                refresh_sentiment_daily(conn, code)
            except Exception as e:
                log(f"   [채점 오류] {e}")


def sync_and_report():
    """서빙 DB 스냅샷을 생성하고 최종 상태를 출력한다."""
    log("\n=== [3단계] 서빙 DB 스냅샷 및 현황 보고 ===")
    try:
        export_snapshot.main()
        log("serve.db 스냅샷 내보내기 성공")
    except Exception as e:
        log(f"스냅샷 내보내기 실패: {e}")

    with get_conn() as conn:
        log("\n--- 현재 종목별 문서 및 채점 현황 ---")
        rows = conn.execute("""
            SELECT d.code, e.name, count(d.doc_id) total_docs, 
                   sum(d.label IS NOT NULL) scored_docs,
                   sum(d.is_canonical=0) dup_docs
            FROM documents d
            LEFT JOIN entities e ON d.code = e.code
            GROUP BY d.code
            ORDER BY total_docs DESC
        """).fetchall()
        for r in rows:
            code, name, total, scored, dup = r[0], r[1] or "-", r[2], r[3] or 0, r[4] or 0
            pct = (scored / total * 100) if total else 0
            log(f"  {code:<8} ({name:<6}) | 총: {total:>7,}건 | 채점: {scored:>7,}건 ({pct:5.1f}%) | 중복상속: {dup:>7,}건")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-collect", action="store_true", help="수집 단계 건너뛰기")
    parser.add_argument("--skip-score", action="store_true", help="채점 단계 건너뛰기")
    parser.add_argument("--kakao-limit", type=int, default=500, help="카카오 1회 채점 상한")
    parser.add_argument("--other-limit", type=int, default=300, help="기타 자산 1회 채점 상한")
    parser.add_argument("--days", type=int, default=365, help="수집 소급 일수")
    args = parser.parse_args()

    init_db()
    
    if not args.skip_collect:
        collect_new_assets(days=args.days)
        
    if not args.skip_score:
        score_undercovered_assets(kakao_limit=args.kakao_limit, other_limit=args.other_limit)
        
    sync_and_report()
    log("\n[전체 파이프라인 1회 라운드 완료]")


if __name__ == "__main__":
    main()
