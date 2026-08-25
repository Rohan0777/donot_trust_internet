"""신호 귀속일 컷오프 경계 검증.

규칙(app/db/dao.py _SIGNAL_DATE_SQL): 게시 시각을 KST로 옮겨 08:50 이전이면 당일,
초과면 다음 날. 이 경계가 하루라도 밀리면 백테스트 전체가 어긋난다 —
장중 뉴스가 같은 날 시가에 체결되는 look-ahead가 생기거나, 개장 전 신호가
하루 늦게 체결되어 유의 신호가 씻겨 나간다.
"""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.db.dao import refresh_sentiment_daily
from tests._support import memory_conn

KST = ZoneInfo("Asia/Seoul")


def kst_to_utc_iso(y: int, mo: int, d: int, h: int, mi: int) -> str:
    dt = datetime(y, mo, d, h, mi, tzinfo=KST)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class SignalDateCutoffTest(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn()
        self.conn.execute(
            "INSERT INTO entities(code, name) VALUES ('TST', '테스트종목')")
        # 실제 파이프라인과 동일하게 매체를 배정한다 (sentiment_daily PK 구성요소).
        self.conn.execute(
            "INSERT INTO media(media_id, name, tier, channel)"
            " VALUES (1, '테스트매체', 'major', 'news')")
        # (게시 KST, 게시일, 예상 signal_date)
        cases = [
            ((7, 49),  "2026-08-25", "2026-08-25"),  # 개장 전 -> 당일
            ((8, 50),  "2026-08-25", "2026-08-25"),  # 경계 포함(<=) -> 당일
            ((8, 51),  "2026-08-25", "2026-08-26"),  # 경계 초과 -> 다음날
            ((9, 0),   "2026-08-24", "2026-08-25"),  # 장중 뉴스 -> 다음날 (look-ahead 차단)
            ((15, 30), "2026-08-24", "2026-08-25"),  # 장 마감 직후 -> 다음날
            ((23, 59), "2026-08-24", "2026-08-25"),  # 자정 직전 -> 다음날
        ]
        for i, ((h, m), kd, _) in enumerate(cases):
            y, mo, d = map(int, kd.split("-"))
            pub = kst_to_utc_iso(y, mo, d, h, m)
            self.conn.execute(
                "INSERT INTO documents(code, media_id, source, title, norm_title,"
                " title_hash, published_utc, published_kst_date, collected_utc)"
                " VALUES ('TST', 1, 'gnews', ?, ?, ?, ?, ?, ?)",
                (f"제목{i}", f"제목{i}", f"h{i}", pub, kd,
                 "2026-08-25T00:00:00+00:00"))
        self.conn.commit()

    def test_cutoff_boundary_assigns_signal_date(self):
        refresh_sentiment_daily(self.conn, "TST")
        # sentiment_daily는 사전집계 테이블 — 문서 수는 COUNT(*)가 아니라 doc_cnt다.
        got = {(r["kst_date"], r["signal_date"]): r["doc_cnt"]
               for r in self.conn.execute(
                   "SELECT kst_date, signal_date, SUM(doc_cnt) doc_cnt"
                   " FROM sentiment_daily GROUP BY 1, 2")}
        expected = {
            ("2026-08-25", "2026-08-25"): 2,  # 07:49, 08:50
            ("2026-08-25", "2026-08-26"): 1,  # 08:51
            ("2026-08-24", "2026-08-25"): 3,  # 09:00, 15:30, 23:59
        }
        self.assertEqual(got, expected)

    def test_total_doc_cnt_preserved(self):
        refresh_sentiment_daily(self.conn, "TST")
        total = self.conn.execute(
            "SELECT SUM(doc_cnt) FROM sentiment_daily").fetchone()[0]
        self.assertEqual(total, 6)


if __name__ == "__main__":
    unittest.main()
