"""coverage 병합 순위(_COVERAGE_RANK) 검증.

같은 실행(run_started) 안에서는 나쁜 쪽이 이긴다:
  empty < completed < partial < failed
실행이 바뀌면 새로 시작한다 — 창을 좁혀 재수집했을 때 partial에서 벗어나야 하므로.

이 규칙이 깨지는 두 가지 방식:
  - completed 가 partial 을 덮으면 "상한에 걸린 날"이라는 사실이 사라진다 (결정 #13)
  - 실행 구분이 깨지면 재수집 성공 날도 영원히 partial 에 갇힌다
"""
import unittest

from app.db.dao import incomplete_dates, upsert_coverage
from tests._support import memory_conn

# 과거 시각: updated_utc(실제 현재) >= run_started 이므로 "같은 실행"으로 병합된다.
SAME_RUN = "2000-01-01T00:00:00+00:00"
# 미래 시각: prev.updated_utc < run_started 이므로 "새 실행"으로 덮어쓴다.
FRESH_RUN = "2099-01-01T00:00:00+00:00"


class CoverageMergeRankTest(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn()

    def upsert(self, date, status, count=10, run_started=SAME_RUN):
        upsert_coverage(self.conn, "TST", "google_rss", date, status,
                        doc_count=count, run_started=run_started)

    def row(self, date="2026-01-05"):
        return self.conn.execute(
            "SELECT status, doc_count FROM coverage WHERE code='TST' AND"
            " source='google_rss' AND kst_date=?", (date,)).fetchone()

    def test_completed_cannot_demote_partial_within_run(self):
        self.upsert("2026-01-05", "partial", count=40)
        self.upsert("2026-01-05", "completed", count=30)
        r = self.row()
        self.assertEqual(r["status"], "partial")      # 상한 정보 보존
        self.assertEqual(r["doc_count"], 70)          # 건수는 누적

    def test_failed_wins_over_partial(self):
        self.upsert("2026-01-05", "partial", count=40)
        self.upsert("2026-01-05", "failed", count=0)
        self.assertEqual(self.row()["status"], "failed")

    def test_failed_sticks_when_success_comes_later_same_run(self):
        # 문서화된 의도: 아예 못 받아온 질의가 있으면 그날의 완전성은 보장 안 된다.
        self.upsert("2026-01-05", "failed", count=0)
        self.upsert("2026-01-05", "completed", count=50)
        r = self.row()
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["doc_count"], 50)

    def test_empty_upgraded_by_completed(self):
        self.upsert("2026-01-05", "empty")
        self.upsert("2026-01-05", "completed", count=5)
        self.assertEqual(self.row()["status"], "completed")

    def test_new_run_escapes_partial(self):
        self.upsert("2026-01-05", "partial", count=40)
        self.upsert("2026-01-05", "completed", count=99, run_started=FRESH_RUN)
        r = self.row()
        self.assertEqual(r["status"], "completed")    # 좁혀서 재수집 성공
        self.assertEqual(r["doc_count"], 99)          # 누적 아님 — 새로 시작

    def test_incomplete_dates_matches_partial_and_failed(self):
        # 서빙 리본(dao.incomplete_dates)과 healthcheck 정의가 한데 모이는 지점.
        self.upsert("2026-01-05", "partial")
        self.upsert("2026-01-06", "failed")
        self.upsert("2026-01-07", "completed")
        self.upsert("2026-01-08", "empty")
        self.assertEqual(incomplete_dates(self.conn, "TST"),
                         ["2026-01-05", "2026-01-06"])


if __name__ == "__main__":
    unittest.main()
