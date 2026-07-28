"""Zone 1(주가 + 감성 오버레이)용 시계열 생성.

백테스트와 달리 게시일(kst_date) 기준으로 집계한다 — 사용자는 "이 날 나온 뉴스의
감성"을 보고 싶어하기 때문. 신호 귀속일 기준은 engine.py가 쓴다.
"""
import numpy as np
import pandas as pd

FREQ_RULE = {"daily": None, "weekly": "W-FRI", "monthly": "ME"}


def _polarity(pos: pd.Series, neg: pd.Series) -> pd.Series:
    """극성지수 = (pos − neg) / (pos + neg). 중립은 분모에서도 빠진다.
    긍정·부정이 하나도 없는 날은 0이 아니라 NaN이다 — 0으로 두면 '중립 여론'으로
    오독되지만 실제로는 '판단 근거 없음'이다."""
    denom = (pos + neg).astype(float).replace(0.0, np.nan)
    return (pos.astype(float) - neg.astype(float)) / denom


RANGE_DAYS = {"3m": 90, "6m": 180, "1y": 365, "all": None, "data": None}


def _clip_range(conn, code: str, df: pd.DataFrame, rng: str) -> pd.DataFrame:
    """기본값 'data' = 감성 데이터가 존재하는 구간에 맞춘다.

    3년치 가격에 감성 29일만 겹쳐 그리면 오버레이가 점처럼 보여 아무것도 읽히지 않는다.
    """
    if df.empty:
        return df
    if rng == "data":
        row = conn.execute(
            "SELECT MIN(kst_date) mn, MAX(kst_date) mx FROM sentiment_daily WHERE code = ?", (code,)
        ).fetchone()
        if not row or not row["mn"]:
            return df
        lo = pd.Timestamp(row["mn"]) - pd.Timedelta(days=10)
        hi = pd.Timestamp(row["mx"]) + pd.Timedelta(days=5)
        return df[(df["kst_date"] >= lo) & (df["kst_date"] <= hi)]
    days = RANGE_DAYS.get(rng)
    if days:
        return df[df["kst_date"] >= df["kst_date"].max() - pd.Timedelta(days=days)]
    return df


def price_series(conn, code: str, freq: str = "daily", rng: str = "data") -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT kst_date, open, high, low, close, volume FROM prices WHERE code = ? ORDER BY kst_date",
        conn, params=(code,),
    )
    if df.empty:
        return df
    df["kst_date"] = pd.to_datetime(df["kst_date"])
    df = _clip_range(conn, code, df, rng)
    rule = FREQ_RULE.get(freq)
    if not rule:
        return df
    out = df.set_index("kst_date").resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"]).reset_index()
    return out


def sentiment_series(conn, code: str, freq: str = "daily", by: str = "total",
                     media_ids: list[int] | None = None) -> dict:
    """by: total | tier | media"""
    sql = (
        "SELECT s.kst_date, m.media_id, m.name, m.tier, SUM(s.pos) pos, SUM(s.neg) neg,"
        " SUM(s.neu) neu, SUM(s.doc_cnt) doc_cnt "
        "FROM sentiment_daily s JOIN media m ON s.media_id = m.media_id WHERE s.code = ? "
    )
    params = [code]
    if by == "media" and media_ids:
        sql += f"AND m.media_id IN ({','.join('?' * len(media_ids))}) "
        params += media_ids
    sql += "GROUP BY s.kst_date, m.media_id"

    df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        return {"dates": [], "series": {}}
    df["kst_date"] = pd.to_datetime(df["kst_date"])

    key = {"total": None, "tier": "tier", "media": "name"}[by]
    rule = FREQ_RULE.get(freq)

    def _agg(frame: pd.DataFrame) -> pd.DataFrame:
        g = frame.groupby("kst_date")[["pos", "neg", "neu", "doc_cnt"]].sum()
        if rule:
            g = g.resample(rule).sum()
        g["polarity"] = _polarity(g["pos"], g["neg"])
        return g

    if key is None:
        g = _agg(df)
        dates = [d.strftime("%Y-%m-%d") for d in g.index]
        return {"dates": dates,
                "series": {"종합": [None if pd.isna(v) else round(v, 4) for v in g["polarity"]]},
                "counts": {"종합": [int(v) for v in g["doc_cnt"]]}}

    all_idx = _agg(df).index
    series, counts = {}, {}
    for name, sub in df.groupby(key):
        g = _agg(sub).reindex(all_idx)
        series[name] = [None if pd.isna(v) else round(v, 4) for v in g["polarity"]]
        counts[name] = [0 if pd.isna(v) else int(v) for v in g["doc_cnt"]]
    return {"dates": [d.strftime("%Y-%m-%d") for d in all_idx], "series": series, "counts": counts}


def coverage_gaps(conn, code: str) -> list[list[str]]:
    """데이터가 없는 구간 [시작, 끝] 목록. 차트에서 회색 음영으로 표시한다."""
    rows = conn.execute(
        "SELECT DISTINCT kst_date FROM sentiment_daily WHERE code = ? ORDER BY kst_date", (code,)
    ).fetchall()
    have = {r["kst_date"] for r in rows}
    days = [r["kst_date"] for r in conn.execute(
        "SELECT kst_date FROM prices WHERE code = ? ORDER BY kst_date", (code,))]
    gaps, start = [], None
    for d in days:
        if d not in have:
            start = start or d
        elif start:
            gaps.append([start, d])
            start = None
    if start:
        gaps.append([start, days[-1]])
    return gaps


def media_catalog(conn, code: str, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        "SELECT m.media_id, m.name, m.tier, SUM(s.doc_cnt) n FROM sentiment_daily s "
        "JOIN media m ON s.media_id = m.media_id WHERE s.code = ? "
        "GROUP BY m.media_id ORDER BY n DESC LIMIT ?", (code, limit)
    ).fetchall()
    return [{"media_id": r["media_id"], "name": r["name"], "tier": r["tier"], "docs": r["n"]} for r in rows]
