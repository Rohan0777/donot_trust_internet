"""네이버 금융 종목뉴스 수집기 (source='naver_news').

목록은 순차 페이지네이션(날짜 커서)으로 넘기고, 본문 HTTP 요청만 쓰레드풀로
병렬 처리한다. 이 페이지는 EUC-KR이지만 인코딩은 http_utils가 헤더에서 판정한다.
"""
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from app.collectors.http_utils import assert_decoded, get
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

SOURCE = "naver_news"
LIST_URL = "https://finance.naver.com/item/news_news.naver?code={code}&page={page}&clusterId="
ARTICLE_URL = "https://n.news.naver.com/mnews/article/{oid}/{aid}"
CONTENT_WORKERS = 10
_HREF_RE = re.compile(r"article_id=(\d+)&office_id=(\d+)")


def _fetch_body(oid: str, aid: str) -> str:
    try:
        resp = get(ARTICLE_URL.format(oid=oid, aid=aid))
    except requests.exceptions.RequestException:
        return ""
    if resp.status_code != 200:
        return ""
    node = BeautifulSoup(resp.text, "lxml").select_one("#dic_area")
    return node.get_text(" ", strip=True) if node else ""


def crawl(conn, code: str, days_back: int = 30, max_pages: int = 5000, progress=print) -> dict:
    cutoff = datetime.now() - timedelta(days=days_back)
    collected = now_utc()
    aliases = stock_aliases(conn, code)
    seen = existing_urls(conn, code, SOURCE)

    saved_by_date: dict[str, int] = defaultdict(int)
    seen_dates: set[str] = set()
    total_saved = last_page = 0
    reached_cutoff = False
    oldest_seen: datetime | None = None

    for page in range(1, max_pages + 1):
        last_page = page
        resp = get(LIST_URL.format(code=code, page=page),
                   referer=f"https://finance.naver.com/item/news.naver?code={code}")
        assert_decoded(resp.text, f"{SOURCE} p{page}")
        table = BeautifulSoup(resp.text, "lxml").select_one("table.type5")
        if not table:
            break
        rows = [r for r in table.select("tbody tr") if r.select_one("td.title a")]
        if not rows:
            break

        candidates = []
        for row in rows:
            a = row.select_one("td.title a")
            m = _HREF_RE.search(a.get("href", ""))
            if not m:
                continue
            aid, oid = m.group(1), m.group(2)
            try:
                published = datetime.strptime(row.select_one("td.date").get_text(strip=True), "%Y.%m.%d %H:%M")
            except (ValueError, AttributeError):
                continue
            if published < cutoff:
                reached_cutoff = True
                continue
            if oldest_seen is None or published < oldest_seen:
                oldest_seen = published
            seen_dates.add(published.strftime("%Y-%m-%d"))
            url = ARTICLE_URL.format(oid=oid, aid=aid)
            if url in seen:
                continue
            seen.add(url)
            press = row.select_one("td.info")
            candidates.append({
                "oid": oid, "aid": aid, "url": url,
                "press": press.get_text(strip=True) if press else "미상 언론사",
                "title": a.get_text(strip=True), "published": published,
            })

        if candidates:
            with ThreadPoolExecutor(max_workers=CONTENT_WORKERS) as pool:
                bodies = list(pool.map(lambda c: _fetch_body(c["oid"], c["aid"]), candidates))
            batch = []
            for c, body in zip(candidates, bodies):
                batch.append({
                    "url": c["url"], "title": c["title"], "body": body or None,
                    "published_utc": to_utc_iso(c["published"]), "collected_utc": collected,
                    "media_id": resolve_media_id(conn, c["press"], "news"),
                    "ts_confidence": "exact",
                })
                saved_by_date[c["published"].strftime("%Y-%m-%d")] += 1
            total_saved += insert_documents(conn, code, SOURCE, batch, aliases)
        conn.commit()

        if page % 20 == 0:
            progress(f"  p{page} | 누적 {total_saved:,}건 | 최고(古) {oldest_seen}", flush=True)
        if reached_cutoff:
            break

    boundary = oldest_seen.strftime("%Y-%m-%d") if oldest_seen else None
    for d in sorted(seen_dates):
        complete = reached_cutoff and boundary is not None and d > boundary
        upsert_coverage(conn, code, SOURCE, d, "completed" if complete else "partial",
                        doc_count=saved_by_date.get(d, 0),
                        last_cursor=None if complete else str(last_page))

    groups, demoted = mark_duplicates(conn, code, seen_dates)
    conn.commit()
    return {"saved": total_saved, "pages": last_page, "dates": len(seen_dates),
            "oldest": boundary, "dup_groups": groups, "dup_demoted": demoted,
            "hit_page_cap": not reached_cutoff and last_page >= max_pages}
