"""감성 신호 백테스트 엔진.

시드머니 개념이 없다. 가중치가 곧 "1점당 매매 주식 수"이고, 매수하면 현금이
음수로 내려가며 그 최저점이 "이 전략을 굴리는 데 실제로 필요했던 자본"이 된다.

  raw(D)   = Σ_tier w_tier × (pos − neg)          # 중립은 애초에 안 들어간다
  desired  = ±raw                                  # 역배팅이면 부호 반전
  delta    = max(desired, −position)  (보유수량내) | desired  (무한공매도)
  체결가    = open(D)                              # 신호는 D-1 15:30~D 08:50에 확정
  순손익    = cash + position × close(D)           # 0원에서 시작, 항상 정의됨

수익률의 분모는 "누적 투입금"이 아니라 **최대 소요자본**이다. 누적 투입금은
매도가 누적되면 음수가 되어 수익률 부호가 뒤집히고 0 근처에서 발산한다.
"""
from dataclasses import dataclass, field

import pandas as pd

DIRECTIONS = ("forward", "contrarian")
POSITION_LIMITS = ("long_only", "unlimited")


@dataclass
class Fees:
    buy_bps: float = 1.5
    sell_bps: float = 1.5
    tax_bps: float = 18.0
    slippage_bps: float = 5.0

    @classmethod
    def load(cls, conn, enabled: bool = True):
        if not enabled:
            return cls(0.0, 0.0, 0.0, 0.0)
        row = conn.execute(
            "SELECT buy_bps, sell_bps, tax_bps, slippage_bps FROM fee_schedule "
            "ORDER BY effective_from DESC LIMIT 1"
        ).fetchone()
        return cls(*row) if row else cls()

    def charge(self, delta: float, price: float) -> float:
        notional = abs(delta) * price
        bps = self.slippage_bps + (self.buy_bps if delta > 0 else self.sell_bps)
        if delta < 0:
            bps += self.tax_bps
        return notional * bps / 10_000.0


@dataclass
class BacktestResult:
    dates: list = field(default_factory=list)
    raw_signal: list = field(default_factory=list)
    delta: list = field(default_factory=list)
    position: list = field(default_factory=list)
    cash: list = field(default_factory=list)
    equity: list = field(default_factory=list)
    net_pnl: list = field(default_factory=list)
    buy_hold: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def load_prices(conn, code: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT kst_date, open, high, low, close, volume FROM prices "
        "WHERE code = ? ORDER BY kst_date", conn, params=(code,)
    )
    if df.empty:
        return df
    df["kst_date"] = pd.to_datetime(df["kst_date"])
    # 시가가 비어 있으면(일부 종목/휴장 보정) 종가로 대체한다.
    df["open"] = df["open"].fillna(df["close"])
    return df


def load_tier_signal(conn, code: str) -> pd.DataFrame:
    """신호 귀속일(signal_date) × tier 로 접힌 pos/neg 건수."""
    df = pd.read_sql_query(
        "SELECT s.signal_date, m.tier, SUM(s.pos) pos, SUM(s.neg) neg, SUM(s.doc_cnt) doc_cnt "
        "FROM sentiment_daily s JOIN media m ON s.media_id = m.media_id "
        "WHERE s.code = ? GROUP BY s.signal_date, m.tier", conn, params=(code,)
    )
    if df.empty:
        return df
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    return df


def _assign_trading_day(signal_df: pd.DataFrame, trading_days: pd.Series) -> pd.DataFrame:
    """주말/휴장일에 귀속된 신호를 다음 거래일로 롤포워드한다."""
    td = pd.DataFrame({"kst_date": trading_days}).sort_values("kst_date")
    merged = pd.merge_asof(
        signal_df.sort_values("signal_date"), td,
        left_on="signal_date", right_on="kst_date", direction="forward",
    )
    return merged.dropna(subset=["kst_date"])


def run_backtest(conn, code: str, weights: dict[str, float], *,
                 direction: str = "forward", position_limit: str = "long_only",
                 fees_enabled: bool = True) -> BacktestResult:
    prices = load_prices(conn, code)
    if prices.empty:
        return BacktestResult(summary={"error": "가격 데이터가 없습니다."})

    signal_df = load_tier_signal(conn, code)
    if signal_df.empty:
        return BacktestResult(summary={"error": "감성 데이터가 없습니다."})

    merged = _assign_trading_day(signal_df, prices["kst_date"])
    merged["w"] = merged["tier"].map(lambda t: float(weights.get(t, 0.0)))
    merged["contrib"] = merged["w"] * (merged["pos"] - merged["neg"])
    raw = merged.groupby("kst_date")["contrib"].sum()

    df = prices.copy()
    df["raw"] = df["kst_date"].map(raw).fillna(0.0)

    # 감성 데이터가 존재하는 첫 거래일부터 시작한다(그 이전은 전부 0이라 의미 없는 평평한 구간).
    active = df.index[df["raw"] != 0]
    if len(active) == 0:
        return BacktestResult(summary={"error": "가중치를 적용한 신호가 전부 0입니다."})
    df = df.loc[max(0, active[0] - 1):].reset_index(drop=True)

    fee = Fees.load(conn, fees_enabled)
    sign = 1.0 if direction == "forward" else -1.0
    long_only = position_limit == "long_only"

    cash = position = total_cost = 0.0
    trades = 0
    out = BacktestResult()

    for row in df.itertuples(index=False):
        price_exec = float(row.open or row.close or 0.0)
        desired = sign * float(row.raw)
        delta = max(desired, -position) if long_only else desired
        if price_exec <= 0:
            delta = 0.0

        cost = fee.charge(delta, price_exec) if delta else 0.0
        cash -= delta * price_exec + cost
        position += delta
        total_cost += cost
        if abs(delta) > 1e-9:
            trades += 1

        equity = position * float(row.close or price_exec)
        out.dates.append(row.kst_date.strftime("%Y-%m-%d"))
        out.raw_signal.append(round(float(row.raw), 4))
        out.delta.append(round(delta, 4))
        out.position.append(round(position, 4))
        out.cash.append(round(cash))
        out.equity.append(round(equity))
        out.net_pnl.append(round(cash + equity))

    # 최대 소요자본: 현금이 파고든 최저점과 공매도 익스포저 중 큰 쪽.
    # (공매도 포지션도 증거금이 필요하므로 자본 소요로 본다.)
    peak_cash_need = max((-c for c in out.cash), default=0.0)
    peak_short = max((abs(e) for e, p in zip(out.equity, out.position) if p < 0), default=0.0)
    peak_capital = max(peak_cash_need, peak_short, 1.0)

    net_final = out.net_pnl[-1] if out.net_pnl else 0.0
    running_max, mdd = float("-inf"), 0.0
    for v in out.net_pnl:
        running_max = max(running_max, v)
        mdd = min(mdd, v - running_max)

    # Buy & Hold 비교선: 동일한 최대 소요자본을 첫 거래일 시가에 전량 투입해 보유.
    first_price = float(df.iloc[0]["open"] or df.iloc[0]["close"] or 0.0)
    bh_shares = peak_capital / first_price if first_price > 0 else 0.0
    bh_cost = fee.charge(bh_shares, first_price) if bh_shares else 0.0
    out.buy_hold = [
        round(bh_shares * float(c) - peak_capital - bh_cost) for c in df["close"].fillna(first_price)
    ]

    out.summary = {
        "net_pnl": round(net_final),
        "peak_capital": round(peak_capital),
        "real_return_pct": round(net_final / peak_capital * 100, 2),
        "mdd_krw": round(mdd),
        "mdd_pct": round(mdd / peak_capital * 100, 2),
        "trades": trades,
        "total_cost": -round(total_cost),
        "final_position": round(position, 2),
        "buy_hold_pnl": out.buy_hold[-1] if out.buy_hold else 0,
        "days": len(out.dates),
        "direction": direction,
        "position_limit": position_limit,
        "fees_enabled": fees_enabled,
    }
    return out
