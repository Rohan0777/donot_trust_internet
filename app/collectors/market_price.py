"""엔티티별 가격/금리 수집.

자산군마다 소스와 의미가 다르다:
  index     지수 포인트 (KS11/KQ11/IXIC/US500)
  crypto    원화 환산 시세. 주말·공휴일에도 값이 있다 — 거래일 개념이 없다.
  commodity 선물 종가 (GC=F)
  bond      **가격이 아니라 금리(%)다.** 금리가 오르면 채권 가격은 내린다.
            수익률 부호가 주식과 반대로 해석돼야 하므로 is_yield로 표시한다.

[국내 채권은 무료 소스를 확보하지 못했다]
KR10YT=RR / KR3YT=RR 모두 404다. 한국은행 ECOS API가 대안이나 키 발급이 필요하다.
KTB는 가격 없이 감성만 수집되며, 시차 상관 분석에서는 제외된다. [확인 필요]
"""
import warnings

warnings.filterwarnings("ignore")

# code -> (심볼, 수익률(금리) 여부)
SYMBOLS = {
    "KOSPI":  ("KS11", False),
    "KOSDAQ": ("KQ11", False),
    "NASDAQ": ("IXIC", False),
    "SPX":    ("US500", False),
    "BTC":    ("BTC/KRW", False),
    "ETH":    ("ETH/KRW", False),
    "GOLD":   ("GC=F", False),
    # 금리 시계열. 값 자체가 %이며 가격이 아니다.
    "UST":    ("^TNX", True),
}

# 가격 소스를 확보하지 못한 엔티티. 감성만 쌓이고 상관분석에서 빠진다.
NO_PRICE = {"KTB", "ALTCOIN"}


def collect(conn, code: str, start: str = "2025-01-01") -> int:
    if code in NO_PRICE or code not in SYMBOLS:
        return 0
    import FinanceDataReader as fdr

    symbol, is_yield = SYMBOLS[code]
    df = fdr.DataReader(symbol, start)
    if df is None or df.empty:
        return 0

    def col(name):
        return df[name] if name in df.columns else df["Close"]

    rows = []
    for idx, r in df.iterrows():
        close = r.get("Close")
        if close is None or close != close:      # NaN
            continue
        rows.append((
            code, idx.strftime("%Y-%m-%d"),
            float(r.get("Open") or close), float(r.get("High") or close),
            float(r.get("Low") or close), float(close),
            int(r.get("Volume") or 0), 1,
        ))
    if not rows:
        return 0

    conn.executemany(
        "INSERT INTO prices(code, kst_date, open, high, low, close, volume, is_adjusted) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(code, kst_date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
        "volume=excluded.volume", rows)
    conn.commit()
    return len(rows)


def collect_all(conn, start: str = "2025-01-01", progress=print) -> dict:
    out = {}
    codes = [r["code"] for r in conn.execute(
        "SELECT code FROM entities WHERE is_active = 1 ORDER BY priority, code")]
    for code in codes:
        if code in NO_PRICE:
            progress(f"  {code:<9} 가격 소스 없음 (감성만 수집)")
            continue
        if code not in SYMBOLS:
            continue
        try:
            n = collect(conn, code, start)
            out[code] = n
            sym, is_yield = SYMBOLS[code]
            progress(f"  {code:<9} {sym:<10} {n:>5}행" + ("  (금리 %)" if is_yield else ""))
        except Exception as exc:  # noqa: BLE001
            progress(f"  {code:<9} 실패: {type(exc).__name__}: {str(exc)[:50]}")
    return out
