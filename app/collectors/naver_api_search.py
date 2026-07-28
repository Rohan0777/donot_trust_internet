"""네이버 검색 오픈API 수집기 — **일간 증분 전용**.

[구 프로젝트에서 1년치 백필을 실패시킨 제약 — 반드시 지킬 것]
이 API는 sort=date만 지원하고 날짜 범위를 지정할 수 없다. days_back은 "언제까지
받고 멈출지"의 cutoff일 뿐이라, 값을 키워도 항상 같은 최신 1000건(start<=1000)이
돌아온다. 구 코드는 이걸 모르고 days_back을 늘려가며 73회 루프를 돌렸고, 결과는
전부 중복 조회 + 신규 0건이었다 (SK하이닉스가 6일치만 남은 원인).

  → 장기 백필은 naver_news(스크래핑) 또는 빅카인즈/Google News RSS가 담당한다.
  → 이 모듈은 매일 1회 하루치를 덧붙이는 용도로만 쓴다.

cafearticle 검색은 게시일 필드를 아예 주지 않는다. 수집시각으로 대체하되
ts_confidence='approx'로 표시해, 개장 전 컷오프 실험에서 배제할 수 있게 한다.
"""
import html
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from app.config import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
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

API_BASE = "https://openapi.naver.com/v1/search/{endpoint}.json"
DISPLAY, MAX_START, DELAY = 100, 1000, 0.1
_TAG_RE = re.compile(r"</?b>")
_KR_TLDS = {"co.kr", "or.kr", "ne.kr", "go.kr", "pe.kr", "re.kr"}


def _clean(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def registrable_domain(host: str) -> str:
    host = host.removeprefix("www.")
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _KR_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _search(endpoint: str, query: str, start: int) -> list[dict]:
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise RuntimeError("NAVER_CLIENT_ID/SECRET 미설정 (.env 확인)")
    resp = requests.get(
        API_BASE.format(endpoint=endpoint),
        headers={"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET},
        params={"query": query, "display": DISPLAY, "start": start, "sort": "date"},
        timeout=10,
    )
    resp.raise_for_status()
    time.sleep(DELAY)
    return resp.json().get("items", [])


def crawl_daily(conn, code: str, name: str, days_back: int = 1, cafe_max: int = 300) -> dict:
    collected = now_utc()
    aliases = stock_aliases(conn, code)
    cutoff = datetime.now().astimezone() - timedelta(days=days_back)
    dates: set[str] = set()
    saved = {"news": 0, "cafe": 0}

    # --- 뉴스: pubDate가 있어 날짜 커서가 동작한다 ---
    src = "naver_api_news"
    seen = existing_urls(conn, code, src)
    for start in range(1, MAX_START, DISPLAY):
        items = _search("news", name, start)
        if not items:
            break
        batch, reached = [], False
        for it in items:
            try:
                pub = parsedate_to_datetime(it["pubDate"])
            except (KeyError, TypeError, ValueError):
                pub = None
            if pub and pub < cutoff:
                reached = True
                continue
            url = it.get("link") or it.get("originallink")
            if not url or url in seen:
                continue
            seen.add(url)
            origin = it.get("originallink") or url
            host = urlparse(origin).netloc
            press = registrable_domain(host) if host else "미상 언론사"
            pub_utc = to_utc_iso(pub)
            if pub_utc:
                dates.add(pub_utc[:10])
            batch.append({
                "url": url, "title": _clean(it.get("title", "")),
                "body": _clean(it.get("description", "")) or None,
                "published_utc": pub_utc, "collected_utc": collected,
                "media_id": resolve_media_id(conn, press, "news"),
                "ts_confidence": "exact" if pub_utc else "approx",
            })
        saved["news"] += insert_documents(conn, code, src, batch, aliases)
        conn.commit()
        if reached:
            break

    # --- 카페: 게시일 없음 → approx ---
    src = "naver_api_cafe"
    seen = existing_urls(conn, code, src)
    cafe_mid = resolve_media_id(conn, "네이버 카페", "cafe", "community")
    for start in range(1, min(cafe_max, MAX_START), DISPLAY):
        items = _search("cafearticle", name, start)
        if not items:
            break
        batch = []
        for it in items:
            url = it.get("link")
            if not url or url in seen:
                continue
            seen.add(url)
            batch.append({
                "url": url, "title": _clean(it.get("title", "")),
                "body": _clean(it.get("description", "")) or None,
                "published_utc": None, "collected_utc": collected,
                "media_id": cafe_mid, "ts_confidence": "approx",
            })
        saved["cafe"] += insert_documents(conn, code, src, batch, aliases)
        conn.commit()
    dates.add(collected[:10])

    for d in sorted(dates):
        # 증분수집은 그 날짜를 끝까지 훑었다고 보장할 수 없다 (1000건 상한).
        upsert_coverage(conn, code, "naver_api_news", d, "partial")
    mark_duplicates(conn, code, dates)
    conn.commit()
    return saved
