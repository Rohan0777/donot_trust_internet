"""Google News RSS 백필 수집기 (source='google_rss').

네이버 검색 API가 백필에 쓸 수 없고(1000건 상한 + 날짜범위 미지원) 빅카인즈는
승인 절차가 필요하므로, 승인 없이 오늘 당장 과거를 채울 수 있는 경로다.

  https://news.google.com/rss/search?q=<종목명>+after:2026-07-01+before:2026-07-08
      &hl=ko&gl=KR&ceid=KR:ko

제약 3가지 (측정 결과):
  - 쿼리당 최대 100건. 넓은 창은 조용히 잘린다 — 32일 창이 창내 99건을 주는데
    16일씩 쪼개면 186건이 나온다(실측). 그래서 창을 넓게 시작해 응답이 100건이면
    반으로 쪼개는 적응형 분할을 쓴다. 희소 구간은 32일 1회로 끝나고(코스피 2022:
    32일 창 51건) 밀집 구간만 1일까지 자동으로 쪼개진다.
  - link가 news.google.com 리다이렉트라 원문 URL을 바로 알 수 없다. 본문은
    수집하지 않고 제목만 쓴다(종목토론방과 같은 취급).
  - <source url="..."> 가 매체명과 도메인을 함께 주므로 매체 귀속은 정확하다.

'카카오' 같은 종목명은 계열사(카카오뱅크/카카오페이) 기사를 함께 끌어온다.
stocks.exclude_json의 제외어로 걸러낸다.
"""
import json
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse
from xml.etree import ElementTree as ET

import requests

from app.config import CRAWL_TIMEOUT_SEC, HTTP_HEADERS
from app.db.dao import (
    existing_urls,
    insert_documents,
    mark_duplicates,
    now_utc,
    resolve_media_id,
    stock_aliases,
    to_utc_iso,
    upsert_coverage,
)

SOURCE = "google_rss"
FEED = ("https://news.google.com/rss/search?q={q}+after:{start}+before:{end}"
        "&hl={hl}&gl={gl}&ceid={ceid}")
DEFAULT_LOCALE = ("ko", "KR", "KR:ko")
PAGE_CAP = 100          # 구글이 쿼리당 반환하는 상한
REQUEST_DELAY_SEC = 1.0
_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{1,30}$")   # "제목 - 매체명" 꼬리표


def _strip_outlet(title: str) -> str:
    return _SUFFIX_RE.sub("", title or "").strip()


def _fetch(query: str, start: str, end: str, locale=DEFAULT_LOCALE) -> list[dict]:
    hl, gl, ceid = locale
    url = FEED.format(q=quote(query), start=start, end=end,
                      hl=hl, gl=gl, ceid=quote(ceid))
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=CRAWL_TIMEOUT_SEC + 5)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SEC)

    root = ET.fromstring(resp.content)
    out = []
    for it in root.findall(".//item"):
        src = it.find("source")
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except (TypeError, ValueError):
            pub = None
        out.append({
            "title": _strip_outlet(it.findtext("title")),
            "url": it.findtext("link"),
            "published": pub,
            "outlet": (src.text if src is not None else None) or "미상 언론사",
            "outlet_url": src.get("url") if src is not None else None,
        })
    return out


def _excluded(conn, code: str) -> list[str]:
    row = conn.execute("SELECT exclude_json FROM entities WHERE code = ?", (code,)).fetchone()
    return json.loads(row["exclude_json"]) if row and row["exclude_json"] else []


def entity_locales(conn, code: str) -> list[tuple]:
    row = conn.execute("SELECT locales_json FROM entities WHERE code = ?", (code,)).fetchone()
    if not row or not row["locales_json"]:
        return [DEFAULT_LOCALE]
    return [tuple(x) for x in json.loads(row["locales_json"])]


def entity_queries(conn, code: str, fallback: str) -> list[str]:
    """검색 질의어 목록. aliases의 각 항목이 독립적인 질의가 된다.
    시장 단위 엔티티는 '코스피|KOSPI|코스피지수'처럼 표기가 여러 개라
    하나만 쓰면 절반을 놓친다."""
    row = conn.execute("SELECT aliases_json FROM entities WHERE code = ?", (code,)).fetchone()
    if row and row["aliases_json"]:
        qs = json.loads(row["aliases_json"])
        if qs:
            return qs
    return [fallback]


def _is_offtopic(title: str, aliases: tuple[str, ...], excludes: list[str]) -> bool:
    """제외어를 지운 뒤에도 종목명이 남아 있으면 본 종목 기사로 본다.
    '카카오뱅크 순익 증가'는 제외, '카카오, 카카오뱅크 지분 매각'은 유지."""
    if not excludes:
        return False
    stripped = title
    for ex in excludes:
        stripped = stripped.replace(ex, " ")
    return not any(a and a in stripped for a in aliases)


def crawl_range(conn, code: str, name: str, start_date: str, end_date: str,
                step_days: int = 32, progress=print, adaptive: bool = True,
                locale=None, query=None) -> dict:
    """구간을 훑어 수집한다.

    adaptive=True면 넓은 창으로 시작해서, 100건 상한에 걸린 창만 반으로 쪼개
    다시 시도한다(채점기의 배치 분할과 같은 패턴). 과거 데이터는 밀도가 낮아
    — 실측상 2024년 코스피가 하루 6건, 2022년 10건 — 하루 단위로 훑으면
    빈 창에 요청을 낭비한다. 최근 구간만 자동으로 잘게 쪼개진다.
    """
    aliases = stock_aliases(conn, code) or (name,)
    excludes = _excluded(conn, code)
    collected = now_utc()
    seen = existing_urls(conn, code, SOURCE)
    loc = locale or DEFAULT_LOCALE
    q = query or name

    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()

    stats = {"saved": 0, "windows": 0, "offtopic": 0, "truncated": 0, "dates": 0, "splits": 0}
    touched: set[str] = set()

    def handle(win_start, win_end, depth=0):
        """한 창을 처리한다. 상한에 걸리면 반으로 쪼개 재귀한다."""
        stats["windows"] += 1
        try:
            items = _fetch(q, win_start.isoformat(), win_end.isoformat(), loc)
        except Exception as exc:  # noqa: BLE001
            progress(f"  [경고] {win_start}~{win_end} 실패: {type(exc).__name__}: {exc}")
            for d in _days(win_start, win_end):
                upsert_coverage(conn, code, SOURCE, d, "failed", error=str(exc)[:200])
            return

        window_days = set(_days(win_start, win_end))
        per_date: dict[str, int] = {}
        for it in items:
            if it["published"]:
                d = to_utc_iso(it["published"])[:10]
                if d in window_days:
                    per_date[d] = per_date.get(d, 0) + 1
        in_window = sum(per_date.values())
        # [분할 판정은 응답 총건수로 한다 — 창내 건수로 하면 안 된다]
        # 구글은 날짜 필터를 느슨하게 적용해 창 밖 기사로 100건을 채운다. 그래서
        # 창내 건수는 상한에 못 미치는데 실제로는 잘린 경우가 생긴다. 실측:
        # 32일 창이 창내 99건(응답 100)을 주는데, 16일씩 쪼개면 186건이 나온다.
        # 응답이 100건이면 결과셋이 잘린 것이므로, 창내 건수와 무관하게 분할한다.
        capped = len(items) >= PAGE_CAP
        span = (win_end - win_start).days

        if capped and span > 1 and depth < 8:
            stats["splits"] += 1
            mid = win_start + timedelta(days=span // 2)
            progress(f"  [분할] {win_start}~{win_end} ({span}일, 창내 {in_window}건 상한) "
                     f"-> {span//2}+{span-span//2}일")
            handle(win_start, mid, depth + 1)
            handle(mid, win_end, depth + 1)
            return

        batch = []
        for it in items:
            if not it["title"] or not it["url"]:
                continue
            if _is_offtopic(it["title"], aliases, excludes):
                stats["offtopic"] += 1
                continue
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            pub_utc = to_utc_iso(it["published"]) if it["published"] else None
            host = urlparse(it["outlet_url"] or "").netloc
            media_id = resolve_media_id(conn, it["outlet"], "news")
            if host:
                conn.execute("UPDATE media SET domain = COALESCE(domain, ?) WHERE media_id = ?",
                             (host.removeprefix("www."), media_id))
            batch.append({
                "url": it["url"], "title": it["title"], "published_utc": pub_utc,
                "collected_utc": collected, "media_id": media_id,
                "ts_confidence": "exact" if pub_utc else "approx",
            })

        # 여기 도달했다는 것은 (a) 응답이 안 잘렸거나 (b) 1일까지 쪼갰는데도 응답이
        # 100건인 경우다. (b)는 이 API로 더 좁힐 방법이 없으므로 완전성을 보장할 수
        # 없다 -> partial. (창내 건수가 100 미만이어도 마찬가지다. 구글이 창 밖
        # 기사로 응답을 채우기 때문에 창내 건수만으로는 잘렸는지 알 수 없다.)
        stuck = capped and span <= 1
        stats["saved"] += insert_documents(conn, code, SOURCE, batch, aliases)
        if stuck:
            stats["truncated"] += 1
        for d in _days(win_start, win_end):
            touched.add(d)
            upsert_coverage(conn, code, SOURCE, d,
                            "partial" if stuck else ("completed" if per_date.get(d) else "empty"),
                            doc_count=per_date.get(d, 0),
                            last_cursor=f"{win_start}~{win_end}" if stuck else None)
        conn.commit()
        progress(f"  {win_start}~{win_end}  신규 {len(batch):>3} / 창내 {in_window:>3} / 응답 {len(items):>3}"
                 f"{'  [1일까지 쪼갰으나 응답 상한 - 완전성 미보장]' if stuck else ''}")

    # adaptive면 넓은 창으로 시작, 아니면 step_days 고정
    stride = max(step_days, 32) if adaptive else step_days
    cursor = start
    while cursor <= end:
        win_end = min(cursor + timedelta(days=stride), end + timedelta(days=1))
        handle(cursor, win_end)
        cursor = win_end

    stats["dates"] = len(touched)
    mark_duplicates(conn, code, touched)
    conn.commit()
    return stats


def _days(a, b) -> list[str]:
    out, cur = [], a
    while cur < b:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def crawl_entity(conn, code: str, name: str, start_date: str, end_date: str,
                 step_days: int = 32, progress=print, adaptive: bool = True) -> dict:
    """엔티티의 모든 (로케일 × 질의어) 조합을 돌린다.

    시장 단위 엔티티는 표기가 여러 개다 — '코스피'/'KOSPI'/'코스피지수'는 서로 다른
    기사 집합을 반환한다. 하나만 쓰면 절반을 놓친다. 중복은 URL UNIQUE로 걸러지므로
    질의를 겹쳐 돌려도 안전하다.
    """
    locales = entity_locales(conn, code)
    queries = entity_queries(conn, code, name)
    total = {"saved": 0, "windows": 0, "offtopic": 0, "truncated": 0,
             "dates": 0, "splits": 0, "combos": 0}
    dates: set[str] = set()

    for loc in locales:
        for q in queries:
            # 한국어 질의를 영어권 로케일에 던지는 것은 의미가 없다(반대도 마찬가지).
            is_ko_query = any("가" <= ch <= "힣" for ch in q)
            if is_ko_query != (loc[0] == "ko"):
                continue
            total["combos"] += 1
            progress(f"--- [{code}] {q!r} @ {loc[0]}")
            st = crawl_range(conn, code, name, start_date, end_date,
                             step_days=step_days, progress=progress,
                             adaptive=adaptive, locale=loc, query=q)
            for k in ("saved", "windows", "offtopic", "truncated", "splits"):
                total[k] += st.get(k, 0)
            dates |= {d for d in _days(
                datetime.strptime(start_date, "%Y-%m-%d").date(),
                datetime.strptime(end_date, "%Y-%m-%d").date() + timedelta(days=1))}
    total["dates"] = len(dates)
    return total
