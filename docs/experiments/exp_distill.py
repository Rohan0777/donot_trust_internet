"""실험 ③: gpt-4o-mini 라벨 31만 건으로 '제목 전용' 경량 분류기를 학습(증류)하면
LLM 일별 극성지수를 얼마나 재현하는가. 시간 분할(2025-06-01 이전 학습 / 이후 검증).
모델: char n-gram TF-IDF + 로지스틱 회귀 (CPU, 학습 수 분, 추론 수천 건/초).
"""
import csv, sys, time, statistics as st
from collections import defaultdict
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, f1_score

SPLIT = "2025-06-01"; K = 10
rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8")))
# 4클래스: -1/0/1 + irrelevant(is_relevant=0) -> 'X'
def y_of(r):
    if r["is_relevant"] in ("0", "0.0"): return "X"
    return {"1": "P", "0": "N0", "-1": "N"}.get(r["label"], "N0")
for r in rows: r["y"] = y_of(r); r["text"] = f"[{r['code']}] {r['title']}"
tr = [r for r in rows if r["date"] < SPLIT]; te = [r for r in rows if r["date"] >= SPLIT]
print(f"train {len(tr):,}  test {len(te):,}   classes(train):", {k: sum(r['y']==k for r in tr) for k in 'P N0 N X'.split()})

t0 = time.time()
vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=3, max_features=400_000, sublinear_tf=True)
Xtr = vec.fit_transform(r["text"] for r in tr); Xte = vec.transform(r["text"] for r in te)
clf = LogisticRegression(max_iter=2000, C=3.0, class_weight="balanced", n_jobs=-1)
clf.fit(Xtr, [r["y"] for r in tr])
print(f"fit {time.time()-t0:.0f}s, features {Xtr.shape[1]:,}")
t0 = time.time(); pred = clf.predict(Xte); print(f"predict {len(te):,} in {time.time()-t0:.1f}s")
ytrue = [r["y"] for r in te]
print(classification_report(ytrue, pred, digits=3))
print("macro-F1", round(f1_score(ytrue, pred, average="macro"), 3))

# 일별 극성지수 재현도 (엔티티별): LLM 라벨 vs 분류기 라벨
def T(pos, neg):
    n = pos + neg; return 0.0 if n == 0 else (pos - neg) / n * n / (n + K)
def ma(xs, w=20): return [sum(xs[max(0,i-w+1):i+1])/len(xs[max(0,i-w+1):i+1]) for i in range(len(xs))]
def corr(a, b):
    ma_, mb = st.mean(a), st.mean(b)
    num = sum((x-ma_)*(y-mb) for x, y in zip(a, b)); da = sum((x-ma_)**2 for x in a)**.5; db = sum((y-mb)**2 for y in b)**.5
    return num/(da*db) if da and db else float("nan")
print(f"\n{'code':8}{'days':>6}{'r_daily':>9}{'r_ma20':>8}{'mean|dT|':>10}")
for code in sorted({r["code"] for r in te}):
    llm = defaultdict(lambda: [0, 0]); ml = defaultdict(lambda: [0, 0])
    for r, p in zip(te, pred):
        if r["code"] != code: continue
        if r["y"] == "P": llm[r["date"]][0] += 1
        elif r["y"] == "N": llm[r["date"]][1] += 1
        if p == "P": ml[r["date"]][0] += 1
        elif p == "N": ml[r["date"]][1] += 1
    days = sorted(set(llm) | set(ml))
    a = [T(*llm[d]) for d in days]; b = [T(*ml[d]) for d in days]
    print(f"{code:8}{len(days):>6}{corr(a,b):>9.3f}{corr(ma(a),ma(b)):>8.3f}{st.mean(abs(x-y) for x,y in zip(a,b)):>10.3f}")
