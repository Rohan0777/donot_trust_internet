"""근사중복 판정 + 도배 억제.

3단계로 좁힌다. 하루 최대 수집량이 16,909건(실측)이라 전수 비교는 1.4억 쌍이고,
버킷팅 없이는 어떤 유사도 라이브러리를 써도 못 돈다.

  1) blocking  : 정규화 제목 앞 8자로 버킷 -> 후보를 지역화
  2) simhash   : 64bit를 4개 밴드(16bit)로 쪼개 같은 밴드값끼리만 비교
                 -> 앞부분이 달라 blocking을 통과 못한 재배포본도 잡힌다
  3) rapidfuzz : 후보쌍만 token_set_ratio >= 95 로 최종 확인

중복은 삭제하지 않는다. 대표(is_canonical=1) 하나만 남기고 나머지는 강등하되
행은 보존한다 — 재배포 횟수 자체가 사건의 파급력을 나타내는 피처이기 때문.

도배 억제는 별개 규칙이다. 같은 작성자가 하루에 N건을 초과해 올리면 초과분을
강등한다. 커뮤니티 한 사람의 연타가 그날 감성지수를 밀어버리는 것을 막는다.
"""
from collections import defaultdict

from rapidfuzz import fuzz

from app.config import FUZZY_MIN_RATIO, MAX_DOCS_PER_AUTHOR_PER_DAY, SIMHASH_MAX_DISTANCE
from app.text.normalize import blocking_key, hamming

BANDS = 4
BAND_BITS = 64 // BANDS


class _Union:
    def __init__(self, keys):
        self.parent = {k: k for k in keys}

    def find(self, a):
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _band_values(simhash: int) -> list[tuple[int, int]]:
    h = simhash & 0xFFFFFFFFFFFFFFFF
    return [(i, (h >> (i * BAND_BITS)) & ((1 << BAND_BITS) - 1)) for i in range(BANDS)]


def group_day(docs: list[dict]) -> dict[int, int]:
    """같은 (종목, 날짜)의 문서들을 중복 그룹으로 묶는다.
    반환: {doc_id: 대표 doc_id}"""
    if len(docs) < 2:
        return {d["doc_id"]: d["doc_id"] for d in docs}

    uf = _Union([d["doc_id"] for d in docs])
    by_id = {d["doc_id"]: d for d in docs}

    # 1) 완전일치: 정규화 제목 해시
    exact = defaultdict(list)
    for d in docs:
        exact[d["title_hash"]].append(d["doc_id"])
    for ids in exact.values():
        for other in ids[1:]:
            uf.union(ids[0], other)

    # 2) 후보 생성: blocking + simhash 밴딩
    candidates: set[tuple[int, int]] = set()
    buckets = defaultdict(list)
    for d in docs:
        buckets[("b", blocking_key(d["norm_title"]))].append(d["doc_id"])
        if d["simhash"]:
            for band in _band_values(d["simhash"]):
                buckets[("s", band)].append(d["doc_id"])

    for ids in buckets.values():
        # 한 버킷이 지나치게 크면(흔한 접두어) 비교 폭발을 막기 위해 건너뛴다.
        if len(ids) < 2 or len(ids) > 400:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                candidates.add((a, b) if a < b else (b, a))

    # 3) 확정: simhash 해밍거리 + 문자열 유사도
    for a, b in candidates:
        if uf.find(a) == uf.find(b):
            continue
        da, db = by_id[a], by_id[b]
        if da["simhash"] and db["simhash"] and hamming(da["simhash"], db["simhash"]) > SIMHASH_MAX_DISTANCE:
            continue
        if fuzz.token_set_ratio(da["norm_title"], db["norm_title"]) >= FUZZY_MIN_RATIO:
            uf.union(a, b)

    return {d["doc_id"]: uf.find(d["doc_id"]) for d in docs}


def spam_demotions(docs: list[dict], cap: int = MAX_DOCS_PER_AUTHOR_PER_DAY) -> set[int]:
    """같은 작성자의 하루 N건 초과분 doc_id. 오래된 것부터 cap개만 남긴다."""
    by_author = defaultdict(list)
    for d in docs:
        if d.get("author"):
            by_author[d["author"]].append(d["doc_id"])
    out: set[int] = set()
    for ids in by_author.values():
        if len(ids) > cap:
            out.update(sorted(ids)[cap:])
    return out
