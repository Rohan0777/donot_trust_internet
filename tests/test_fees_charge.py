"""거래 수수료 산술(Fees.charge) 검증.

bps 방향 실수는 백테스트 수익률을 통째로 왜곡하지만 눈에 잘 띄지 않는다.
특히 세금(tax)은 매도에만 붙는다 — 매수에 붙이면 비용이 ~2배가 된다.
"""
import unittest

from app.backtest.engine import Fees


class FeeChargeTest(unittest.TestCase):
    def fee(self):
        return Fees(buy_bps=15, sell_bps=15, tax_bps=300, slippage_bps=10)

    def test_buy_charges_slippage_plus_buy_only(self):
        # 100주 @ 1,000원 = 100,000원, bps = 10+15 = 25
        self.assertAlmostEqual(self.fee().charge(+100, 1000.0), 250.0)

    def test_sell_charges_slippage_sell_and_tax(self):
        # bps = 10+15+300 = 325 -> 100,000원 × 325/10,000 = 3,250원
        self.assertAlmostEqual(self.fee().charge(-100, 1000.0), 3250.0)

    def test_tax_never_applies_to_buy(self):
        no_tax = Fees(buy_bps=15, sell_bps=0, tax_bps=300, slippage_bps=10)
        self.assertAlmostEqual(no_tax.charge(+100, 1000.0), 250.0)

    def test_zero_delta_costs_nothing(self):
        self.assertAlmostEqual(self.fee().charge(0.0, 1000.0), 0.0)

    def test_disabled_load_returns_zero_fee(self):
        from tests._support import memory_conn
        conn = memory_conn()
        conn.execute("INSERT INTO fee_schedule VALUES"
                     " ('2026-01-01', 15, 15, 300, 10, 3650, 365)")
        fee = Fees.load(conn, enabled=False)
        self.assertEqual((fee.buy_bps, fee.sell_bps, fee.tax_bps,
                          fee.slippage_bps, fee.borrow_bps_annual,
                          fee.financing_bps_annual), (0.0,) * 6)

    def test_load_reads_latest_schedule_with_carry_columns(self):
        # [확인 필요] carry 요율은 미확보 — 스키마 확장 후에도 기존 행(열 없음)이
        # 마이그레이션되면 0으로 읽혀야 한다.
        from tests._support import memory_conn
        conn = memory_conn()
        conn.execute("INSERT INTO fee_schedule(effective_from, buy_bps, sell_bps,"
                     " tax_bps, slippage_bps) VALUES ('2026-01-01', 15, 15, 300, 10)")
        fee = Fees.load(conn, enabled=True)
        self.assertAlmostEqual(fee.buy_bps, 15)
        self.assertEqual(fee.borrow_bps_annual, 0.0)
        self.assertEqual(fee.financing_bps_annual, 0.0)


if __name__ == "__main__":
    unittest.main()
