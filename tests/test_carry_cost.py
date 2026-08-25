"""보유기간 비용(carry) 검증 — 차입(borrow)과 현금 금융(financing).

이 백테스트는 시드머니 0에서 시작한다. 매수 순간 현금은 음수가 되므로
"돈을 빌려 사는 것"이고, 공매도는 주식을 빌리는 것이다. 둘 다 요율 0으로
쳐지던 상태에서 나온 수익률은 비용 누락이다. 특히 논지의 유일한 흑자였던
역배팅/무한공매도 칸이 이 비용으로 뒤집힐 수 있다.

요율 단위는 연율 bps, 일할 부과(365일). 실제 요율은 [확인 필요]이며
테스트는 산술 규칙만 고정한다 — 임의의 시장 요율을 사실로 굳히지 않기 위해.
"""
import unittest

from app.backtest.engine import Fees, carry_charge, run_backtest
from tests._support import memory_conn


def make_fee(borrow=0.0, financing=0.0):
    return Fees(buy_bps=15, sell_bps=15, tax_bps=300, slippage_bps=10,
                borrow_bps_annual=borrow, financing_bps_annual=financing)


class CarryChargeUnitTest(unittest.TestCase):
    def test_borrow_accrues_on_short_position(self):
        # 50주 @ 100원 = 5,000원 익스포저 × 3,650bps/10,000 ÷ 365일 = 5원/일
        self.assertAlmostEqual(
            carry_charge(make_fee(borrow=3650), position=-50, close=100.0,
                         cash=+4981.0), 5.0)

    def test_financing_accrues_on_negative_cash(self):
        # 10,000원 부채 × 730bps/10,000 ÷ 365일 = 2원/일
        self.assertAlmostEqual(
            carry_charge(make_fee(financing=730), position=+10, close=100.0,
                         cash=-10_000.0), 2.0)

    def test_positive_cash_earns_nothing(self):
        # 보수적 관례: 현금 여유에는 이자를 주지 않는다. 주면 논지에 유리하게
        # 숫자를 만지는 꼴이 된다.
        self.assertAlmostEqual(
            carry_charge(make_fee(borrow=3650, financing=730),
                         position=+10, close=100.0, cash=+5_000.0), 0.0)

    def test_zero_rates_charge_nothing(self):
        self.assertAlmostEqual(
            carry_charge(make_fee(), position=-50, close=100.0, cash=-5_000.0),
            0.0)


def seeded_conn(borrow: float, financing: float):
    """4거래일 픽스처. 가격 불변(100원)이라 손익은 오직 비용에서 나온다.

    D1에 major 감성 pos=10 -> raw=5×10=50주 매수 신호. D2~D4 신호 없음.
    """
    conn = memory_conn()
    conn.execute("INSERT INTO entities(code, name) VALUES ('TST', '테스트')")
    conn.execute("INSERT INTO media(media_id, name, tier, channel)"
                 " VALUES (1, '매체', 'major', 'news')")
    for i, day in enumerate(("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08")):
        conn.execute("INSERT INTO prices VALUES ('TST', ?, 100, 100, 100, 100, 0,"
                     " 0, 'test')", (day,))
        if i == 0:
            conn.execute("INSERT INTO sentiment_daily VALUES"
                         " ('TST', '2026-01-05', '2026-01-05', 1, 10, 0, 0, 0, 10, 1, 0)")
    conn.execute("INSERT INTO fee_schedule VALUES"
                 " ('2026-01-01', 15, 15, 300, 10, ?, ?)", (borrow, financing))
    conn.commit()
    return conn


WEIGHTS = {"major": 5.0}


class EngineCarryIntegrationTest(unittest.TestCase):
    def run_bt(self, borrow, financing, **kw):
        conn = seeded_conn(borrow, financing)
        return run_backtest(conn, "TST", WEIGHTS,
                            fees_enabled=True, **kw)

    def test_financing_reduces_long_pnl_by_exactly_total_carry(self):
        base = self.run_bt(0.0, 0.0)
        rated = self.run_bt(0.0, 730.0)
        # equity는 가격에서만 나오므로 변하지 않는다 -> 순손익 차이 == 총 carry.
        # 다만 두 순손익은 각각 반올림된 정수라 끝자리 ±1까지 허용한다.
        diff = base.summary["net_pnl"] - rated.summary["net_pnl"]
        self.assertAlmostEqual(diff, -rated.summary["carry_total"], delta=1)
        # 손계산: |cash|≈5,012원 × 730bps(=7.3%)/365 ≈ 1.0원/일 × 4일 ≈ 4원
        self.assertTrue(3.5 <= abs(rated.summary["carry_total"]) <= 4.5,
                        rated.summary["carry_total"])

    def test_borrow_reduces_short_pnl_by_exactly_total_carry(self):
        kw = dict(direction="contrarian", position_limit="unlimited")
        base = self.run_bt(0.0, 0.0, **kw)
        rated = self.run_bt(3650.0, 730.0, **kw)
        self.assertLess(rated.summary["final_position"], 0)
        diff = base.summary["net_pnl"] - rated.summary["net_pnl"]
        self.assertAlmostEqual(diff, -rated.summary["carry_total"], delta=1)
        # 손계산: 공매도 익스포저 ≈5,000원 × 3,650bps(=36.5%)/365 = 5원/일 × 4일
        self.assertTrue(18 <= abs(rated.summary["carry_total"]) <= 22,
                        rated.summary["carry_total"])

    def test_fees_toggle_kills_carry_too(self):
        conn = seeded_conn(3650.0, 730.0)
        off = run_backtest(conn, "TST", WEIGHTS,
                           direction="contrarian", position_limit="unlimited",
                           fees_enabled=False)
        self.assertEqual(off.summary["carry_total"], 0)
        self.assertEqual(off.summary["total_cost"], 0)


if __name__ == "__main__":
    unittest.main()
