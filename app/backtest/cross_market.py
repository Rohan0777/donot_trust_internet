"""시장 간 비교 — "어느 시장이 여론을 따라가고, 어느 시장이 역행하는가".

개별 종목 백테스트와는 다른 질문이다. 백테스트는 "이 전략으로 얼마 벌었나"를 묻고,
여기서는 "여론과 가격의 관계가 시장마다 어떻게 다른가"를 묻는다.

시차 상관(lead-lag)을 쓴다:
    lag<0  여론이 가격을 선행  (여론 보고 사면 돈이 된다)
    lag=0  동시                (여론이 가격을 반영할 뿐)
    lag>0  가격이 여론을 선행  (사람들이 뒤늦게 떠든다 = 후행지표)

부호도 함께 본다:
    양수  여론과 가격이 같은 방향 (여론 추종이 유효)
    음수  역행 (여론과 반대로 가야 한다 — 이 사이트의 가설)

[주의 1: 이 수치는 인과가 아니다]
상관이 높다고 여론이 가격을 움직인 것은 아니다. 둘 다 같은 사건에 반응했을
수 있고, 표본이 짧으면 우연히 높게 나온다.

[주의 2: 지수의 lag>0 상관은 거의 항등식이다]
"코스피" 기사는 대부분 그날 지수가 어떻게 됐는지를 서술한다. 지수가 오르면
다음날 보도 논조가 긍정인 것은 시장 예측이 아니라 사실 전달이다. 실측 코스피
lag=+1에서 r=+0.72가 나오는데, 추세 공유가 아님을 차분으로 확인했음에도
(차분 후 +0.62) 이것을 "여론이 시장을 반영한다"는 발견으로 읽으면 안 된다.
개별 종목(+0.24)보다 지수에서 훨씬 높게 나오는 이유가 이것이다.
의미 있는 신호는 lag<0 쪽에 있다.

[주의 3: |z|는 유의성을 과대평가한다]
SE=1/sqrt(n-3)은 관측치가 독립일 때의 값이다. 일별 감성·수익률은 자기상관이
있어 실효 표본이 더 작다. |z|는 하한이 아니라 낙관적 상한으로 읽어야 한다.
"""
import math
import statistics as st

import pandas as pd

from app.config import SHRINKAGE_K

# 거래일 개념이 없는 자산은 달력 자체가 다르다. 코인은 주말에도 가격이 있다.
CALENDAR_247 = {"crypto"}


def _corr(a: list[float], b: list[float]) -> tuple[float, float]:
    """(상관계수, |z|). z는 대략적 유의성 지표로 SE=1/sqrt(n-3)을 가정한다."""
    n = len(a)
    if n < 10:
        return float("nan"), 0.0
    ma, mb = st.mean(a), st.mean(b)
    sa, sb = st.pstdev(a), st.pstdev(b)
    if not sa or not sb:
        return float("nan"), 0.0
    r = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n * sa * sb)
    return r, abs(r) * math.sqrt(max(n - 3, 1))


def entity_frame(conn, code: str, shrink: int = SHRINKAGE_K) -> pd.DataFrame:
    """엔티티의 (날짜, 온도지수, 글수, 수익률) 프레임."""
    sent = pd.read_sql_query(
        "SELECT signal_date AS d, SUM(pos) pos, SUM(neg) neg, SUM(doc_cnt) n "
        "FROM sentiment_daily WHERE code = ? GROUP BY signal_date", conn, params=(code,))
    if sent.empty:
        return pd.DataFrame()
    sent["d"] = pd.to_datetime(sent["d"])
    denom = (sent["pos"] + sent["neg"]).astype(float)
    sent["temp"] = ((sent["pos"] - sent["neg"]) / denom.replace(0.0, float("nan"))) \
        * (denom / (denom + shrink))
    sent["net"] = sent["pos"] - sent["neg"]

    px = pd.read_sql_query(
        "SELECT kst_date AS d, open, close FROM prices WHERE code = ? ORDER BY kst_date",
        conn, params=(code,))
    if px.empty:
        # 가격 소스가 없는 엔티티(지수·채권 등)는 감성만 반환한다.
        sent["ret"] = float("nan")
        return sent[["d", "temp", "net", "n", "ret"]]
    px["d"] = pd.to_datetime(px["d"])
    px["ret"] = px["close"].pct_change()
    out = sent.merge(px[["d", "ret"]], on="d", how="inner")
    return out[["d", "temp", "net", "n", "ret"]]


# 시차를 적용하면 실효 표본이 max_lag만큼 줄어든다. 30일 미만은 어떤 lag에서도
# 우연히 큰 상관이 나온다 — 실측 BTC 15일이 lag=+5에서 r=+0.947(실효 10개)을 냈다.
MIN_DAYS = 30


def lead_lag(conn, code: str, max_lag: int = 5, use: str = "net") -> dict:
    """시차별 상관. lag는 '감성을 며칠 뒤 수익률과 맞추는가'다."""
    df = entity_frame(conn, code)
    # 감성이 아직 하나도 없는 엔티티는 빈 프레임이라 'ret' 컬럼 자체가 없다.
    # (수집은 됐지만 채점 전인 상태에서 실제로 발생한다.)
    if df.empty or "ret" not in df.columns:
        return {"code": code, "days": 0, "lags": {}, "best": None,
                "error": "감성 데이터 없음"}
    df = df.dropna(subset=["ret"])
    if len(df) < MIN_DAYS:
        return {"code": code, "days": len(df), "lags": {}, "best": None,
                "error": f"표본 부족({len(df)}일 / 최소 {MIN_DAYS}일)"}
    s = df[use].fillna(0).tolist()
    r = df["ret"].tolist()

    lags = {}
    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            a, b = s[:lag], r[-lag:]          # 감성이 앞선다
        elif lag > 0:
            a, b = s[lag:], r[:-lag]          # 가격이 앞선다
        else:
            a, b = s, r
        c, z = _corr(a, b)
        if not math.isnan(c):
            lags[lag] = {"r": round(c, 4), "z": round(z, 2), "n": len(a)}

    best = max(lags.items(), key=lambda kv: abs(kv[1]["r"]), default=None)

    # 추세 공유로 인한 허위상관 점검: 차분 후에도 부호·크기가 유지되는지.
    ds = [s[i] - s[i - 1] for i in range(1, len(s))]
    dr = r[1:]
    diff_r = None
    if best:
        lag = best[0]
        if lag < 0:
            a, b = ds[:lag], dr[-lag:]
        elif lag > 0:
            a, b = ds[lag:], dr[:-lag]
        else:
            a, b = ds, dr
        c, _ = _corr(a, b)
        diff_r = None if math.isnan(c) else round(c, 4)

    return {"code": code, "days": len(df), "lags": lags,
            "best": {"lag": best[0], **best[1]} if best else None,
            "diff_r": diff_r,
            "trend_shared": round(_corr(s, list(range(len(s))))[0], 3) if len(s) > 10 else None}


def compare(conn, codes: list[str] | None = None, max_lag: int = 5) -> list[dict]:
    """여러 시장을 한 번에 비교한다. 결과는 |r| 내림차순."""
    if codes is None:
        codes = [r["code"] for r in conn.execute(
            "SELECT code FROM entities WHERE is_active=1 AND priority=1 ORDER BY code")]
    out = []
    for code in codes:
        row = conn.execute("SELECT name, kind FROM entities WHERE code=?", (code,)).fetchone()
        res = lead_lag(conn, code, max_lag)
        res["name"] = row["name"] if row else code
        res["kind"] = row["kind"] if row else "?"
        out.append(res)
    out.sort(key=lambda x: -(abs(x["best"]["r"]) if x.get("best") else 0))
    return out


def sentiment_matrix(conn, codes: list[str], freq: str = "W") -> dict:
    """시장 간 감성 동조화. 여론 자체가 시장을 넘나들며 같이 움직이는지 본다.
    (가격이 없는 엔티티도 포함할 수 있다 — 감성끼리의 비교이므로.)"""
    frames = {}
    for code in codes:
        df = entity_frame(conn, code)
        if df.empty:
            continue
        g = df.set_index("d")["temp"].resample(freq).mean()
        if g.notna().sum() >= 4:
            frames[code] = g
    if len(frames) < 2:
        return {"codes": list(frames), "matrix": {}, "dates": []}

    merged = pd.DataFrame(frames).dropna(how="all")
    mat = {}
    for a in merged.columns:
        mat[a] = {}
        for b in merged.columns:
            if a == b:
                # merged[[a, a]] 는 같은 이름의 컬럼 2개짜리 DataFrame이 되어
                # pair[a] 가 Series가 아닌 DataFrame을 돌려준다. 대각선은 정의상 1.0.
                mat[a][b] = 1.0
                continue
            pair = merged[[a, b]].dropna()
            if len(pair) < 4:
                mat[a][b] = None
                continue
            c, _ = _corr(pair[a].tolist(), pair[b].tolist())
            mat[a][b] = None if math.isnan(c) else round(c, 3)
    return {"codes": list(merged.columns),
            "dates": [d.strftime("%Y-%m-%d") for d in merged.index],
            "matrix": mat,
            "series": {c: [None if pd.isna(v) else round(v, 4) for v in merged[c]]
                       for c in merged.columns}}
