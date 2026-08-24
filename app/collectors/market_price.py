"""엔티티별 가격 수집.

자산군마다 소스와 의미가 다르다:
  index     지수 포인트 (KS11/KQ11/IXIC/US500)
  crypto    원화 환산 시세. 주말·공휴일에도 값이 있다 — 거래일 개념이 없다.
  commodity 선물 종가 (GC=F)
  bond      **채권 ETF 가격**. 아래 참조.

[채권을 금리가 아니라 ETF 가격으로 담는 이유]
국내 국고채 금리는 무료 소스를 확보하지 못했다 — KR10YT=RR / KR3YT=RR / KR1YT=RR
모두 404이고, 한국은행 ECOS API는 키 발급이 필요하다. 그래서 KTB는 금리로 담을
방법이 없다.

그러면 UST만 금리로 남는데, 그렇게 두면 두 채권이 서로 다른 단위가 된다. 시차 상관
표는 모든 엔티티를 close.pct_change()로 수익률화해 나란히 놓으므로, 금리 계열이
섞이면 그 행만 부호가 반대인 채로(금리가 오르면 채권 가격은 내린다) 아무 표시 없이
실린다. 그래서 둘 다 ETF 가격으로 통일했다.

  KTB -> 148070 (KOSEF 국고채10년)
  UST -> IEF    (iShares 7-10Y Treasury)

**ETF는 순수 금리가 아니다.** 듀레이션·운용보수·추적오차가 섞여 있고, 만기가 고정된
지수가 아니라 편입 채권을 굴리는 실물이다. "국고채 금리" 질의로 모은 감성과 자산이
정확히 같은 것을 가리키지는 않는다. 다만 부호 의미가 주식·코인과 같아지고 실제 체결
가능한 대상이 된다는 이점이 그 오차보다 크다고 봤다. [확인 필요]

[ALTCOIN은 합성 지수다]
"알트코인"은 단일 자산이 아니다. 무료 알트코인 지수 소스가 없어 4종
(XRP·SOL·ADA·DOGE) 동일가중 지수를 직접 만든다. **생존편향이 있다** — 오늘 거래대금
상위인 코인으로 과거를 돌리는 것이므로 그 사이 사라진 코인은 들어가지 않는다.
1년 창에서는 완만하지만 창을 늘릴수록 낙관 쪽으로 기운다.
"""
import warnings

warnings.filterwarnings("ignore")

# code -> 심볼
SYMBOLS = {
    "KOSPI":  "KS11",
    "KOSDAQ": "KQ11",
    "NASDAQ": "IXIC",
    "SPX":    "US500",
    "BTC":    "BTC/KRW",
    "ETH":    "ETH/KRW",
    "GOLD":   "GC=F",
    # 채권 ETF (금리 아님 — 위 주석 참조)
    "KTB":    "148070",
    "UST":    "IEF",
}

# 합성 지수. 심볼 하나로 받아올 수 없어 별도 경로를 탄다.
ALTCOIN_BASKET = ("XRP/KRW", "SOL/KRW", "ADA/KRW", "DOGE/KRW")
ALTCOIN_SOURCE = "basket:" + "+".join(s.split("/")[0] for s in ALTCOIN_BASKET)
ALTCOIN_BASE = 100.0

# 단위가 바뀐 코드. 과거 행은 새 계열과 섞을 수 없으므로 통째로 지운다.
# UST는 금리(%, 약 4.7)에서 ETF 가격(달러, 약 93)으로 바뀌었다 — 섞으면 전환일
# 하루에 +1,900% 수익률이 생기고, 그 하루가 상관계수 전체를 지배한다.
UNIT_CHANGED = {"UST"}


def _clear_stale(conn, code: str, symbol: str, progress) -> int:
    """심볼이 바뀐 과거 행을 지운다.

    source가 NULL인 행은 이 컬럼이 생기기 전에 쌓인 것이라 출처를 알 수 없다.
    단위가 바뀐 코드는 지우고, 그렇지 않으면 현재 심볼로 소급 기록한다.
    """
    if code in UNIT_CHANGED:
        n = conn.execute(
            "DELETE FROM prices WHERE code = ? AND (source IS NULL OR source <> ?)",
            (code, symbol)).rowcount
    else:
        n = conn.execute(
            "DELETE FROM prices WHERE code = ? AND source IS NOT NULL AND source <> ?",
            (code, symbol)).rowcount
        conn.execute("UPDATE prices SET source = ? WHERE code = ? AND source IS NULL",
                     (symbol, code))
    if n:
        progress(f"  {code:<9} 이전 심볼 {n:,}행 삭제 (단위 불일치 — 새 심볼 {symbol})")
    return n


def _write(conn, rows: list) -> int:
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO prices(code, kst_date, open, high, low, close, volume, is_adjusted, source) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(code, kst_date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
        "volume=excluded.volume, source=excluded.source", rows)
    conn.commit()
    return len(rows)


def _altcoin_rows(start: str) -> list:
    """4종 동일가중 지수. 일간 수익률을 평균해 100에서 체인으로 쌓는다.

    지수 레벨을 코인 가격의 평균으로 만들면 안 된다 — SOL(13만원)과 DOGE(127원)를
    평균하면 사실상 SOL 단독 지수가 된다. 수익률을 평균해야 동일가중이다.

    고가·저가는 합성 바스켓에 정의되지 않는다(각 코인의 장중 고점 시각이 다르다).
    시가와 종가의 범위로만 채우고 봉차트에는 꼬리가 그려지지 않는다 — 없는 값을
    지어내는 것보다 낫다.
    """
    import pandas as pd
    import FinanceDataReader as fdr

    frames = {}
    for sym in ALTCOIN_BASKET:
        df = fdr.DataReader(sym, start)
        if df is None or df.empty:
            raise RuntimeError(f"{sym} 응답 없음 — 구성이 바뀌면 계열이 단절된다")
        frames[sym] = df

    closes = pd.DataFrame({s: f["Close"] for s, f in frames.items()}).dropna()
    if len(closes) < 2:
        return []
    opens = pd.DataFrame({s: f["Open"] for s, f in frames.items()}).reindex(closes.index)

    ret_c = closes.pct_change().mean(axis=1)
    level = ALTCOIN_BASE * (1 + ret_c.fillna(0.0)).cumprod()
    # 시가는 "전일 종가 대비 시가"의 동일가중 평균을 전일 지수에 적용한다.
    ret_o = (opens / closes.shift(1) - 1).mean(axis=1)
    level_open = (level.shift(1) * (1 + ret_o)).fillna(level)

    rows = []
    for d in closes.index:
        c, o = float(level.loc[d]), float(level_open.loc[d])
        if c != c or o != o:            # NaN
            continue
        rows.append((
            "ALTCOIN", d.strftime("%Y-%m-%d"),
            o, max(o, c), min(o, c), c,
            0,                          # 코인마다 단위가 달라 거래량은 합산할 수 없다
            1, ALTCOIN_SOURCE,
        ))
    return rows


def collect(conn, code: str, start: str = "2025-01-01", progress=print) -> int:
    if code == "ALTCOIN":
        _clear_stale(conn, code, ALTCOIN_SOURCE, progress)
        return _write(conn, _altcoin_rows(start))
    if code not in SYMBOLS:
        return 0
    import FinanceDataReader as fdr

    symbol = SYMBOLS[code]
    df = fdr.DataReader(symbol, start)
    if df is None or df.empty:
        return 0
    _clear_stale(conn, code, symbol, progress)

    rows = []
    for idx, r in df.iterrows():
        close = r.get("Close")
        if close is None or close != close:      # NaN
            continue
        rows.append((
            code, idx.strftime("%Y-%m-%d"),
            float(r.get("Open") or close), float(r.get("High") or close),
            float(r.get("Low") or close), float(close),
            int(r.get("Volume") or 0), 1, symbol,
        ))
    return _write(conn, rows)


def collect_all(conn, start: str = "2025-01-01", progress=print) -> dict:
    out = {}
    codes = [r["code"] for r in conn.execute(
        "SELECT code FROM entities WHERE is_active = 1 ORDER BY priority, code")]
    for code in codes:
        if code != "ALTCOIN" and code not in SYMBOLS:
            continue
        try:
            n = collect(conn, code, start, progress)
            out[code] = n
            sym = ALTCOIN_SOURCE if code == "ALTCOIN" else SYMBOLS[code]
            progress(f"  {code:<9} {sym:<22} {n:>5}행")
        except Exception as exc:  # noqa: BLE001
            progress(f"  {code:<9} 실패: {type(exc).__name__}: {str(exc)[:50]}")
    return out
