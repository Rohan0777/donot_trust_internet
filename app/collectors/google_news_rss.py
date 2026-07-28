"""Google News RSS 백필 수집기 (source='google_rss').

네이버 검색 API가 백필에 쓸 수 없고(1000건 상한 + 날짜범위 미지원) 빅카인즈는
승인 절차가 필요하므로, 승인 없이 오늘 당장 과거를 채울 수 있는 경로다.

  https://news.google.com/rss/search?q=<종목명>+after:2026-07-01+before:2026-07-08
      &hl=ko&gl=KR&ceid=KR:ko

제약 3가지 (측정 결과):
  - 쿼리당 최대 100건. 100건이 꽉 차면 그 구간은 잘린 것이므로 coverage에
    'partial'로 남기고 창을 더 좁혀 재수집해야 한다. 이 판정을 자동화했다.
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
        "&hl=ko&gl=KR&ceid=KR%3Ako")
PAGE_CAP = 100          # 구글이 쿼리당 반환하는 상한
REQUEST_DELAY_SEC = 1.0
_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{1,30}$")   # "제목 - 매체명" 꼬리표


def _strip_outlet(title: str) -> str:
    return _SUFFIX_RE.sub("", title or "").strip()


def _fetch(query: str, start: str, end: str) -> list[dict]:
    url = FEED.format(q=quote(query), start=start, end=end)
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
    row = conn.execute("SELECT exclude_json FROM stocks WHERE code = ?", (code,)).fetchone()
    return json.loads(row["exclude_json"]) if row and row["exclude_json"] else []


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
                step_days: int = 3, progress=print) -> dict:
    aliases = stock_aliases(conn, code) or (name,)
    excludes = _excluded(conn, code)
    collected = now_utc()
    seen = existing_urls(conn, code, SOURCE)

    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()

    stats = {"saved": 0, "windows": 0, "offtopic": 0, "truncated": 0, "dates": 0}
    touched: set[str] = set()

    cursor = start
    while cursor <= end:
        win_end = min(cursor + timedelta(days=step_days), end + timedelta(days=1))
        stats["windows"] += 1
        try:
            items = _fetch(name, cursor.isoformat(), win_end.isoformat())
        except Exception as exc:  # noqa: BLE001
            progress(f"  [경고] {cursor}~{win_end} 실패: {type(exc).__name__}: {exc}")
            for d in _days(cursor, win_end):
                upsert_coverage(conn, code, SOURCE, d, "failed", error=str(exc)[:200])
            cursor = win_end
            continue

        # 절단 판정은 응답 건수가 아니라 "요청한 창 안에 든 건수"로 한다.
        # 구글은 창 밖 기사도 섞어서 100건을 채워 보내므로, 응답이 100건이라는
        # 사실만으로 partial을 찍으면 이미 완전히 수집한 날짜까지 미완으로 남는다.
        window_days = set(_days(cursor, win_end))
        # per_date는 "이번 응답에서 이 날짜로 확인된 기사 수"다. 신규 저장분만 세면
        # 이미 수집을 마친 날짜가 'empty'(글 없음)로 뒤집힌다 — 둘은 다른 상태다.
        per_date: dict[str, int] = {}
        for it in items:
            if it["published"]:
                d = to_utc_iso(it["published"])[:10]
                if d in window_days:
                    per_date[d] = per_date.get(d, 0) + 1
        in_window = sum(per_date.values())
        truncated = in_window >= PAGE_CAP
        stats["truncated"] += 1 if truncated else 0

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

        stats["saved"] += insert_documents(conn, code, SOURCE, batch, aliases)

        for d in _days(cursor, win_end):
            touched.add(d)
            # 100건이 꽉 찼으면 그 구간은 잘렸다 -> completed로 단정하지 않는다.
            upsert_coverage(conn, code, SOURCE, d,
                            "partial" if truncated else ("completed" if per_date.get(d) else "empty"),
                            doc_count=per_date.get(d, 0),
                            last_cursor=f"{cursor}~{win_end}" if truncated else None)
        conn.commit()
        progress(f"  {cursor}~{win_end}  신규 {len(batch):>3} / 창내 {in_window:>3} / 응답 {len(items):>3}"
                 f"{'  [상한 도달 - 창을 좁혀 재수집 필요]' if truncated else ''}")
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
