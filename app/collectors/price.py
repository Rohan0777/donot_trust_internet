"""가격 수집 (pykrx).

주의: 수정주가 여부는 확인이 필요하다. 액면분할/유상증자가 미반영이면 백테스트에
-50% 같은 가짜 수익률이 찍힌다. is_adjusted 컬럼에 확인 결과를 기록할 것.
"""
from datetime import datetime, timedelta

from pykrx import stock as krx

from app.db.dao import now_utc


def collect_prices(conn, code: str, years: int = 3, adjusted: bool = True) -> int:
    fromdate = (datetime.now() - timedelta(days=365 * years)).strftime("%Y%m%d")
    todate = datetime.now().strftime("%Y%m%d")
    df = krx.get_market_ohlcv_by_date(fromdate, todate, code, adjusted=adjusted)
    if df is None or df.empty:
        return 0
    rows = [
        (code, idx.strftime("%Y-%m-%d"), float(r["시가"]), float(r["고가"]),
         float(r["저가"]), float(r["종가"]), int(r["거래량"]), 1 if adjusted else 0)
        for idx, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT INTO prices(code, kst_date, open, high, low, close, volume, is_adjusted) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(code, kst_date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
        "volume=excluded.volume, is_adjusted=excluded.is_adjusted",
        rows,
    )
    conn.commit()
    return len(rows)


def sync_kospi_master(conn) -> int:
    import FinanceDataReader as fdr

    df = fdr.StockListing("KOSPI")
    col = "Code" if "Code" in df.columns else "Symbol"
    rows = [(r[col], r["Name"], "KOSPI") for _, r in df.iterrows() if isinstance(r.get("Name"), str)]
    conn.executemany(
        "INSERT INTO stocks(code, name, market) VALUES (?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market",
        rows,
    )
    conn.commit()
    return len(rows)
