"""중복 라벨 상속의 provenance 검증.

상속행에 현재 PROMPT_VERSION을 찍으면 과거 프롬프트로 채점된 라벨이 최신으로
위장한다 — 결정 #6이 막으려던 regime shift가 상속 경로로 생기는 것이고,
healthcheck는 'inherited'를 검사 대상에서 빼므로 어떤 점검에도 걸리지 않는다.
"""
import unittest

from app.scoring.scorer import inherit_duplicate_labels
from tests._support import memory_conn


def add_doc(conn, doc_id, *, code="TST", group=None, canonical=1, label=None,
            relevant=1, conf=None, pv=None):
    conn.execute(
        "INSERT INTO documents(doc_id, code, media_id, source, title, norm_title,"
        " title_hash, published_utc, published_kst_date, collected_utc,"
        " dup_group_id, is_canonical, label, is_relevant, confidence,"
        " prompt_version, label_model)"
        " VALUES (?, ?, NULL, 'gnews', 't', 't', ?,"
        " '2026-01-05T00:00:00+00:00', '2026-01-05', '2026-01-06T00:00:00+00:00',"
        " ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, code, f"h{doc_id}", group, canonical, label, relevant, conf, pv,
         "direct" if label is not None else None))


class InheritDuplicateLabelsTest(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn()
        self.conn.executemany(
            "INSERT INTO entities(code, name) VALUES (?, '테스트')",
            [("TST",), ("OTH",)])

    def row(self, doc_id):
        return dict(self.conn.execute(
            "SELECT label, is_relevant rel, confidence, prompt_version pv,"
            " label_model FROM documents WHERE doc_id = ?", (doc_id,)).fetchone())

    def test_inherits_label_and_provenance_from_canonical(self):
        # 대표글은 v2 프롬프트 시절에 직접 채점됐다.
        add_doc(self.conn, 1, group="g1", canonical=1, label=-1, relevant=0,
                conf=0.9, pv="v2")
        add_doc(self.conn, 2, group="g1", canonical=0)
        n = inherit_duplicate_labels(self.conn, "TST")
        self.assertEqual(n, 1)
        got = self.row(2)
        self.assertEqual(got["label"], -1)
        self.assertEqual(got["rel"], 0)
        self.assertAlmostEqual(got["confidence"], 0.9)   # 이전엔 NULL로 남았다
        self.assertEqual(got["pv"], "v2")                # v3로 위장하면 안 된다
        self.assertEqual(got["label_model"], "inherited")
        # 대표글 자체는 건드리지 않는다.
        self.assertEqual(self.row(1)["label_model"], "direct")

    def test_no_labeled_canonical_leaves_row_pending(self):
        add_doc(self.conn, 1, group="g1", canonical=1)          # 대표글 미채점
        add_doc(self.conn, 2, group="g1", canonical=0)
        self.assertEqual(inherit_duplicate_labels(self.conn, "TST"), 0)
        self.assertIsNone(self.row(2)["label"])                 # 다음 실행에서 재대상

    def test_same_group_other_code_is_not_matched(self):
        add_doc(self.conn, 1, code="OTH", group="g1", canonical=1, label=1,
                conf=0.8, pv="v3")
        add_doc(self.conn, 2, code="TST", group="g1", canonical=0)
        self.assertEqual(inherit_duplicate_labels(self.conn, "TST"), 0)
        self.assertIsNone(self.row(2)["label"])

    def test_already_labeled_duplicates_are_skipped(self):
        add_doc(self.conn, 1, group="g1", canonical=1, label=1, conf=0.8, pv="v3")
        add_doc(self.conn, 2, group="g1", canonical=0, label=0, pv="v1")
        self.assertEqual(inherit_duplicate_labels(self.conn, "TST"), 0)
        self.assertEqual(self.row(2)["pv"], "v1")               # 재상속으로 덮지 않는다


if __name__ == "__main__":
    unittest.main()
