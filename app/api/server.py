"""FastAPI 서빙 계층.

구 프로젝트의 치명적 실수 2가지를 구조적으로 차단한다:
  1. SimpleHTTPRequestHandler 상속 → CWD 전체가 정적 서빙되어 .env가 노출됐다.
     여기서는 StaticFiles(directory=web) 로 서빙 범위를 명시적으로 봉인한다.
  2. 요청마다 모델을 학습했다. 여기서는 사전집계된 sentiment_daily의 선형결합만
     수행하므로 가중치를 바꿔도 재집계가 없다.
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.backtest import series
from app.backtest.engine import DIRECTIONS, POSITION_LIMITS, run_backtest
from app.config import DEFAULT_TIER_WEIGHTS
from app.db.conn import get_conn

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
TIERS = tuple(DEFAULT_TIER_WEIGHTS.keys())

app = FastAPI(title="인터넷을 믿지 마세요", docs_url="/api/docs")


class Weights(BaseModel):
    major: float = Field(5.0, ge=0, le=1000)
    daily: float = Field(1.0, ge=0, le=1000)
    online: float = Field(0.05, ge=0, le=1000)
    unknown: float = Field(1.0, ge=0, le=1000)
    blog: float = Field(0.01, ge=0, le=1000)
    community: float = Field(0.001, ge=0, le=1000)


def _parse_weights(raw: str | None) -> dict[str, float]:
    """'major:5,community:0.001' 형태를 파싱한다. 범위를 벗어나면 400."""
    if not raw:
        return dict(DEFAULT_TIER_WEIGHTS)
    out = dict(DEFAULT_TIER_WEIGHTS)
    for chunk in raw.split(","):
        if ":" not in chunk:
            continue
        k, _, v = chunk.partition(":")
        k = k.strip()
        if k not in TIERS:
            raise HTTPException(400, f"알 수 없는 매체 등급: {k}")
        try:
            fv = float(v)
        except ValueError:
            raise HTTPException(400, f"가중치가 숫자가 아닙니다: {k}={v}")
        if not (0 <= fv <= 1000):
            raise HTTPException(400, f"가중치 범위 초과(0~1000): {k}={fv}")
        out[k] = fv
    return out


def _known_codes(conn) -> set[str]:
    return {r["code"] for r in conn.execute("SELECT code FROM entities WHERE is_active = 1")}


@app.get("/api/stocks")
def stocks():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.code, s.name, COUNT(DISTINCT d.published_kst_date) days, COUNT(d.doc_id) docs,"
            " MIN(d.published_kst_date) mn, MAX(d.published_kst_date) mx "
            "FROM entities s LEFT JOIN documents d ON s.code = d.code "
            "GROUP BY s.code HAVING docs > 0 ORDER BY docs DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/mapping-progress")
def mapping_progress():
    """미분류(unknown) 비중. UI에 경고 배지로 노출해 데이터 부채를 숨기지 않는다."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) total, SUM(m.tier = 'unknown') unknown FROM documents d "
            "JOIN media m ON d.media_id = m.media_id WHERE m.channel = 'news'"
        ).fetchone()
        mapped = conn.execute(
            "SELECT SUM(tier != 'unknown') mapped, COUNT(*) total FROM media WHERE channel = 'news'"
        ).fetchone()
    total = row["total"] or 1
    return {"unknown_docs": row["unknown"] or 0, "total_docs": row["total"] or 0,
            "unknown_pct": round((row["unknown"] or 0) / total * 100, 1),
            "media_mapped": mapped["mapped"] or 0, "media_total": mapped["total"] or 0}


@app.get("/api/chart")
def chart(code: str, freq: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
          by: str = Query("total", pattern="^(total|tier|media)$"),
          rng: str = Query("data", pattern="^(data|3m|6m|1y|all)$"),
          media_ids: str | None = None):
    with get_conn() as conn:
        if code not in _known_codes(conn):
            raise HTTPException(404, f"등록되지 않은 종목입니다: {code}")
        px = series.price_series(conn, code, freq, rng)
        ids = [int(x) for x in media_ids.split(",") if x.strip().isdigit()] if media_ids else None
        sent = series.sentiment_series(conn, code, freq, by, ids)
        return {
            "price": {
                "dates": [d.strftime("%Y-%m-%d") for d in px["kst_date"]] if not px.empty else [],
                "open": px["open"].round(0).tolist() if not px.empty else [],
                "close": px["close"].round(0).tolist() if not px.empty else [],
            },
            "sentiment": sent,
            "coverage_gaps": series.coverage_gaps(conn, code),
            "media_catalog": series.media_catalog(conn, code),
        }


@app.get("/api/backtest")
def backtest(code: str,
             direction: str = Query("forward", pattern="^(forward|contrarian)$"),
             position_limit: str = Query("long_only", pattern="^(long_only|unlimited)$"),
             fees: bool = True,
             w: str | None = None):
    weights = _parse_weights(w)
    with get_conn() as conn:
        if code not in _known_codes(conn):
            raise HTTPException(404, f"등록되지 않은 종목입니다: {code}")
        res = run_backtest(conn, code, weights, direction=direction,
                           position_limit=position_limit, fees_enabled=fees)
        # "1점 = 1주 = 현재 N원" 환산값. 종목 간 비교 가능성을 위해 항상 함께 내려준다.
        last = conn.execute(
            "SELECT close FROM prices WHERE code = ? ORDER BY kst_date DESC LIMIT 1", (code,)
        ).fetchone()
    return {"dates": res.dates, "raw_signal": res.raw_signal, "delta": res.delta,
            "position": res.position, "cash": res.cash, "equity": res.equity,
            "net_pnl": res.net_pnl, "buy_hold": res.buy_hold,
            "summary": res.summary, "weights": weights,
            "point_value_krw": round(last["close"]) if last else None}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
