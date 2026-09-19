"""실험 ①: GDELT DOC API timelinetone(일별 평균 톤) vs TNI 극성지수 T (2022-01 ~ 현재).
호출은 분기 단위, 6초 간격 (GDELT: 5초/1회 제한). 결과는 stdout + gdelt_cache.json.
비교: 일별 상관, 20d MA 상관, lag -5..+5 에서 최대 상관 (양수 lag = GDELT 가 선행).
"""
import json, sqlite3, sys, time, statistics as st, urllib.request, urllib.parse, os
DB = sys.argv[1]
Q = {"GOLD": '"gold price"', "UST": '"treasury yields"', "NASDAQ": "nasdaq", "SPX": '"S&P 500"', "BTC": "bitcoin",
     "TSLA": "tesla", "NVDA": "nvidia"}
START, END = "2022-01-01", "2026-09-19"
K = 10
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gdelt_cache.json")
cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}

def quarters():
    y, q = 2022, 1
    while True:
        s = f"{y}{(q-1)*3+1:02d}01000000"
        e = f"{y}{q*3:02d}{[31,30,30,31][q-1]}235959"
        yield s, e
        if (y, q) == (2026, 3): break
        q += 1
        if q == 5: y, q = y + 1, 1

def fetch(query, s, e):
    key = f"{query}|{s}|{e}"
    if key in cache: return cache[key]
    u = ("https://api.gdeltproject.org/api/v2/doc/doc?query=" + urllib.parse.quote(f"{query} sourcelang:english")
         + f"&mode=timelinetone&startdatetime={s}&enddatetime={e}&format=json&timelinesmooth=0")
    for attempt in range(8):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            body = urllib.request.urlopen(req, timeout=90).read().decode()
            data = json.loads(body)["timeline"][0]["data"]
            cache[key] = data; json.dump(cache, open(CACHE, "w")); time.sleep(6)
            return data
        except Exception as ex:
            print(f"   retry {attempt} {query} {s[:8]}: {str(ex)[:80]}", flush=True); time.sleep(60 * (attempt + 1))
    return []

def corr(a, b):
    if len(a) < 10: return float("nan")
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x-ma)*(y-mb) for x, y in zip(a, b))
    da = sum((x-ma)**2 for x in a) ** .5; db = sum((y-mb)**2 for y in b) ** .5
    return num/(da*db) if da and db else float("nan")

def ma20(xs):
    return [sum(xs[max(0, i-19):i+1])/len(xs[max(0, i-19):i+1]) for i in range(len(xs))]

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
print(f"{'code':7}{'days':>6}{'r_daily':>9}{'r_ma20':>8}{'best_lag':>9}{'r_lag':>7}   query")
for code, query in Q.items():
    tni = {}
    for d, pos, neg in c.execute("SELECT kst_date, SUM(pos), SUM(neg) FROM sentiment_daily WHERE code=? AND kst_date BETWEEN ? AND ? GROUP BY 1", (code, START, END)):
        n = (pos or 0) + (neg or 0)
        if n: tni[d] = (pos - neg) / n * n / (n + K)
    g = {}
    for s, e in quarters():
        for p in fetch(query, s, e):
            g[p["date"][:4] + "-" + p["date"][4:6] + "-" + p["date"][6:8]] = p["value"]
    days = sorted(set(tni) & set(g))
    if len(days) < 30:
        print(f"{code:7}{len(days):>6}   (TNI 라벨 부족 — 비교 불가)   {query}"); continue
    a = [tni[d] for d in days]; b = [g[d] for d in days]
    am, bm = ma20(a), ma20(b)
    best = (0, corr(am, bm))
    for lag in range(-5, 6):
        if lag >= 0: x, y = am[lag:], bm[:len(bm)-lag]
        else: x, y = am[:lag], bm[-lag:]
        r = corr(x, y)
        if r == r and abs(r) > abs(best[1]): best = (lag, r)
    print(f"{code:7}{len(days):>6}{corr(a, b):>9.3f}{corr(am, bm):>8.3f}{best[0]:>9}{best[1]:>7.3f}   {query}", flush=True)
