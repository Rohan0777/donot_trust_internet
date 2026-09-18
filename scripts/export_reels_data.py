import sys
import os
sys.path.insert(0, os.getcwd())

import sqlite3
import pandas as pd
import json
from app.backtest.engine import run_backtest

def export_btc_reels_data():
    conn = sqlite3.connect('data/serve.db')
    
    weights = {"tier_1": 1.0, "tier_2": 0.5, "tier_3": 0.2, "news": 1.0, "community": 0.5, "cafe": 0.2}
    code = "BTC"
    
    # 1. Run engine backtest for Internet Trader
    res = run_backtest(conn, code, weights, direction="forward", position_limit="long_only", fees_enabled=True)
    
    # 2. Fetch prices & daily sentiment breakdown
    prices_df = pd.read_sql_query(
        "SELECT kst_date, open, high, low, close, volume FROM prices WHERE code = ? ORDER BY kst_date",
        conn, params=(code,)
    )
    prices_df['kst_date'] = pd.to_datetime(prices_df['kst_date'])
    
    # Filter starting from backtest start date
    start_date = res.dates[0]
    prices_df = prices_df[prices_df['kst_date'] >= pd.to_datetime(start_date)].reset_index(drop=True)
    
    # Sentiment Breakdown by date
    sent_df = pd.read_sql_query(
        "SELECT s.signal_date, SUM(s.pos) pos, SUM(s.neg) neg, SUM(s.doc_cnt) doc_cnt "
        "FROM sentiment_daily s WHERE s.code = ? GROUP BY s.signal_date",
        conn, params=(code,)
    )
    sent_df['signal_date'] = pd.to_datetime(sent_df['signal_date'])
    sent_map = {row['signal_date'].strftime("%Y-%m-%d"): row for _, row in sent_df.iterrows()}
    
    # 3. Calculate DCA (매월 100만원 적립식 투자)
    dca_cash_invested = 0.0
    dca_btc_units = 0.0
    dca_equity_series = []
    dca_roi_series = []
    monthly_amount = 1_000_000.0
    prev_month = None
    
    for idx, row in prices_df.iterrows():
        curr_month = row['kst_date'].strftime("%Y-%m")
        close_price = float(row['close'] or 0.0)
        
        if prev_month != curr_month:
            dca_cash_invested += monthly_amount
            fee = monthly_amount * 0.00065
            net_buy = monthly_amount - fee
            dca_btc_units += net_buy / close_price if close_price > 0 else 0.0
            prev_month = curr_month
        
        current_val = dca_btc_units * close_price
        dca_equity_series.append(round(current_val))
        roi = ((current_val - dca_cash_invested) / dca_cash_invested) * 100.0 if dca_cash_invested > 0 else 0.0
        dca_roi_series.append(round(roi, 2))
        
    # 4. Build daily frames for front-end 60-second video player
    frames = []
    events = []
    
    peak_cap = res.summary['peak_capital']
    initial_price = float(prices_df.iloc[0]['open'] or prices_df.iloc[0]['close'])
    
    for i, date_str in enumerate(res.dates):
        price_val = float(prices_df.iloc[i]['close'] if i < len(prices_df) else 0.0)
        net_pnl = res.net_pnl[i]
        
        # Internet trader total value (peak_cap + net_pnl)
        trader_val = peak_cap + net_pnl
        trader_roi = (net_pnl / peak_cap) * 100.0 if peak_cap > 0 else 0.0
        
        # Buy & Hold total value (peak_cap + buy_hold_pnl)
        bh_pnl = res.buy_hold[i]
        bh_val = peak_cap + bh_pnl
        bh_roi = (bh_pnl / peak_cap) * 100.0 if peak_cap > 0 else 0.0
        
        # DCA total value & roi
        dca_val = dca_equity_series[i] if i < len(dca_equity_series) else 0
        dca_roi = dca_roi_series[i] if i < len(dca_roi_series) else 0.0
        
        # Sentiment metrics
        sent_info = sent_map.get(date_str)
        pos_cnt = int(sent_info['pos']) if sent_info is not None else 0
        neg_cnt = int(sent_info['neg']) if sent_info is not None else 0
        doc_cnt = int(sent_info['doc_cnt']) if sent_info is not None else 0
        raw_sig = res.raw_signal[i]
        
        # Check trade delta/events
        curr_pos = res.position[i]
        delta = res.delta[i]
        
        event_msg = None
        if delta > 0:
            event_msg = f"🔥 여론 긍정 전환 (+{raw_sig:.1f}) → 비트코인 {delta:.2f}개 매수!"
        elif delta < 0:
            event_msg = f"⚠️ 여론 부정 전환 ({raw_sig:.1f}) → {abs(delta):.2f}개 손절/매도!"
        elif doc_cnt > 100 and raw_sig > 15:
            event_msg = f"📢 뉴스/커뮤니티 버즈 폭발! (문서 {doc_cnt}건)"
        
        if event_msg:
            events.append({"frame_idx": i, "date": date_str, "text": event_msg})
            
        frames.append({
            "idx": i,
            "date": date_str,
            "price": price_val,
            "trader_val": trader_val,
            "trader_roi": trader_roi,
            "trader_pos": curr_pos,
            "dca_val": dca_val,
            "dca_roi": dca_roi,
            "bh_val": bh_val,
            "bh_roi": bh_roi,
            "raw_signal": raw_sig,
            "pos_cnt": pos_cnt,
            "neg_cnt": neg_cnt,
            "doc_cnt": doc_cnt,
            "delta": delta,
            "event": event_msg
        })
        
    data_out = {
        "asset_name": "비트코인 (BTC)",
        "start_date": res.dates[0],
        "end_date": res.dates[-1],
        "total_days": len(res.dates),
        "initial_capital": peak_cap,
        "summary": {
            "trader": {
                "initial_invested": peak_cap,
                "final_val": peak_cap + res.summary['net_pnl'],
                "roi_pct": res.summary['real_return_pct'],
                "net_pnl": res.summary['net_pnl'],
                "total_trades": res.summary['trades'],
                "fee_loss": res.summary['total_cost'] + res.summary['carry_total']
            },
            "dca": {
                "initial_invested": dca_cash_invested,
                "final_val": dca_equity_series[-1],
                "roi_pct": dca_roi_series[-1],
                "net_pnl": dca_equity_series[-1] - dca_cash_invested
            },
            "buy_hold": {
                "initial_invested": peak_cap,
                "final_val": peak_cap + res.summary['buy_hold_pnl'],
                "roi_pct": round((res.summary['buy_hold_pnl'] / peak_cap) * 100, 2),
                "net_pnl": res.summary['buy_hold_pnl']
            }
        },
        "frames": frames,
        "events": events
    }
    
    out_file = os.path.join("web", "reels_btc_data.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data_out, f, ensure_ascii=False, indent=2)
        
    print(f"Successfully exported Reels simulation dataset to {out_file}")
    print(f"Total Frames: {len(frames)}, Total Events: {len(events)}")
    conn.close()

if __name__ == "__main__":
    export_btc_reels_data()
