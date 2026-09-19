"""실험 ②: (종목,일)당 표본 N건만 채점했을 때 극성지수 T 의 오차.
T(t) = (pos-neg)/(pos+neg) * n/(n+k), k=10.  전수(라벨된 대표글 전부) 대비
  (a) cap-N: 실제 daily_cap 과 같은 우선순위(뉴스=매체등급, 커뮤니티=반응순) 상위 N
  (b) rand-N: 무작위 N (5회 평균)
오차 = mean|T_N - T_full| (n_full > N 인 날만; 그 외는 동일), 그리고 20d MA 상관.
"""
import sqlite3, sys, random, statistics as st
DB = sys.argv[1]; CODES = sys.argv[2:] or ["005930", "KOSPI", "BTC", "NASDAQ"]
K = 10; NS = [5, 10, 20, 50, 100, 200]
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
NONOP = ("vendor", "algo", "pr", "aggregator")
tier_rank = {"major": 0, "daily": 1, "online": 2, "unknown": 3, "blog": 4}

def T(pos, neg):
    n = pos + neg
    return 0.0 if n == 0 else (pos - neg) / n * n / (n + K)

def ma(xs, w=20):
    out = []
    for i in range(len(xs)):
        seg = xs[max(0, i - w + 1):i + 1]; out.append(sum(seg) / len(seg))
    return out

def corr(a, b):
    if len(a) < 3: return float("nan")
    ma_, mb = st.mean(a), st.mean(b)
    num = sum((x - ma_) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma_) ** 2 for x in a) ** .5; db = sum((y - mb) ** 2 for y in b) ** .5
    return num / (da * db) if da and db else float("nan")

print(f"{'code':8}{'N':>5}{'days>N':>8}{'err(cap)':>10}{'err(rand)':>10}{'r20(cap)':>10}{'r20(rand)':>10}   days_total")
for code in CODES:
    rows = c.execute(
        "SELECT d.published_kst_date, d.label, m.channel, m.tier, COALESCE(d.engagement,-1) "
        "FROM documents d JOIN media m ON m.media_id=d.media_id "
        "WHERE d.code=? AND d.is_canonical=1 AND d.label IN (1,0,-1) AND COALESCE(d.is_relevant,1)=1 "
        f"AND m.channel NOT IN {NONOP} AND d.published_kst_date >= '2024-01-01'", (code,)).fetchall()
    bydate = {}
    for dt, lab, ch, tier, eng in rows:
        bydate.setdefault(dt, []).append((lab, ch, tier, eng))
    dates = sorted(bydate)
    full = [T(sum(l == 1 for l, *_ in bydate[d]), sum(l == -1 for l, *_ in bydate[d])) for d in dates]
    for N in NS:
        errs_cap, errs_rand, ser_cap, ser_rand = [], [], [], []
        for d, tf in zip(dates, full):
            docs = bydate[d]
            if len(docs) <= N:
                ser_cap.append(tf); ser_rand.append(tf); continue
            # (a) daily_cap 과 동일한 순서: 뉴스 먼저(매체등급), 커뮤니티는 반응순
            ordered = sorted(docs, key=lambda x: (x[1] in ("community", "cafe"),
                                                  tier_rank.get(x[2], 5) if x[1] not in ("community", "cafe") else 0,
                                                  -x[3]))
            top = ordered[:N]
            tc = T(sum(l == 1 for l, *_ in top), sum(l == -1 for l, *_ in top))
            errs_cap.append(abs(tc - tf)); ser_cap.append(tc)
            # (b) 무작위
            rs = []
            for s in range(5):
                random.seed(s * 1000 + N); smp = random.sample(docs, N)
                rs.append(T(sum(l == 1 for l, *_ in smp), sum(l == -1 for l, *_ in smp)))
            tr = sum(rs) / 5
            errs_rand.append(abs(tr - tf)); ser_rand.append(tr)
        ec = st.mean(errs_cap) if errs_cap else 0.0
        er = st.mean(errs_rand) if errs_rand else 0.0
        print(f"{code:8}{N:>5}{len(errs_cap):>8}{ec:>10.3f}{er:>10.3f}{corr(ma(ser_cap), ma(full)):>10.3f}{corr(ma(ser_rand), ma(full)):>10.3f}   {len(dates)}")
    n_per_day = sorted(len(v) for v in bydate.values())
    print(f"  {code}: docs/day median {n_per_day[len(n_per_day)//2]}, p90 {n_per_day[int(len(n_per_day)*.9)]}, max {n_per_day[-1]}, labeled docs {len(rows):,} since 2024-01-01")
