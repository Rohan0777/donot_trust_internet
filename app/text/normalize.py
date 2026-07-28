"""제목 정규화 · 해시 · SimHash.

중복판정 2단계의 1단계(완전일치)와 2단계(근사일치) 후보 생성을 모두 담당한다.
외부 의존성 없이 표준 라이브러리만 쓴다 (rapidfuzz는 최종 확인 단계에서만 사용).
"""
import hashlib
import re
import unicodedata

# [속보] [단독] [특징주] (종합) (종합2보) <표> 등 매체가 붙이는 접두 태그.
# 이걸 제거하지 않으면 같은 기사의 재배포본이 유사도 95 밑으로 떨어져 중복을 놓친다.
_BRACKET_RE = re.compile(r"[\[\(<【〔][^\]\)>】〕]{0,20}[\]\)>】〕]")
# 커뮤니티 도배글 "가즈아 (1)", "가즈아 2222" 대응: 끝자락 숫자/반복문자 제거
_TRAILING_NUM_RE = re.compile(r"[\s\-_.]*\d+\s*$")
_NON_WORD_RE = re.compile(r"[^0-9A-Za-z가-힣]+")
_REPEAT_RE = re.compile(r"(.)\1{2,}")


def normalize_title(title: str, stock_aliases: tuple[str, ...] = ()) -> str:
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", title)
    s = _BRACKET_RE.sub(" ", s)
    for alias in stock_aliases:
        if alias:
            s = s.replace(alias, " ")
    s = _TRAILING_NUM_RE.sub("", s)
    s = _REPEAT_RE.sub(r"\1\1", s)          # "ㅋㅋㅋㅋㅋ" -> "ㅋㅋ"
    s = _NON_WORD_RE.sub("", s)
    return s.lower()


def title_hash(norm_title: str) -> str:
    return hashlib.sha1(norm_title.encode("utf-8")).hexdigest()


def body_hash(body: str | None) -> str | None:
    """본문 파기 전에 반드시 호출해야 한다. 파기 후에는 복구 불가."""
    if not body:
        return None
    collapsed = _NON_WORD_RE.sub("", unicodedata.normalize("NFKC", body))
    return hashlib.sha1(collapsed.encode("utf-8")).hexdigest() if collapsed else None


def _shingles(s: str, k: int = 3) -> list[str]:
    if len(s) < k:
        return [s] if s else []
    return [s[i : i + k] for i in range(len(s) - k + 1)]


def simhash64(norm_title: str) -> int:
    """char 3-gram 기반 64bit SimHash. 해밍거리 <=3 이면 근사중복 후보로 본다."""
    grams = _shingles(norm_title)
    if not grams:
        return 0
    vector = [0] * 64
    for gram in grams:
        h = int.from_bytes(hashlib.md5(gram.encode("utf-8")).digest()[:8], "big")
        for bit in range(64):
            vector[bit] += 1 if (h >> bit) & 1 else -1
    out = 0
    for bit in range(64):
        if vector[bit] > 0:
            out |= 1 << bit
    # SQLite INTEGER는 부호있는 64bit이므로 그대로 넣으면 오버플로한다.
    return out - (1 << 64) if out >= (1 << 63) else out


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


def blocking_key(norm_title: str, n: int = 8) -> str:
    """일별 전수비교(최대 16,909건 = 1.4억 쌍)를 피하기 위한 버킷 키.
    같은 버킷 안에서만 유사도를 계산한다."""
    return norm_title[:n]
