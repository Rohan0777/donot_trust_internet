"""4chan /biz/ 수집기 (source='4chan').

[이 소스는 과거를 살 수 없다 — 오늘 수집하지 않으면 오늘 데이터는 영구 손실이다]
공식 JSON API(a.4cdn.org)는 살아있는 카탈로그만 제공한다. 만료된 스레드는 영구
삭제되며 과거 스레드 번호는 전부 404다(실측). 백필 경로가 존재하지 않는다.
따라서 이 수집기는 "매일 돌려서 쌓는" 용도이며, 놓친 날은 되돌릴 수 없다.

API 예절:
  - If-Modified-Since를 보내 변경 없으면 304로 끝낸다(4chan API 규약).
  - 요청 간 1초 이상 간격.
  - 카탈로그 1회(413KB)로 살아있는 스레드 전부의 메타를 얻는다. 본문까지 필요한
    스레드만 골라 thread/{id}.json을 추가 요청한다.

/biz/는 추천 개념이 없다. engagement에는 댓글 수를 넣는다(종토방의 조회수와
같은 자리). endorse_up/down은 NULL이다.
"""
import html
import re
import time
from datetime import datetime, timezone

import requests

from app.config import CRAWL_TIMEOUT_SEC, HTTP_HEADERS
from app.db.dao import (
    existing_urls,
    insert_documents,
    mark_duplicates,
    now_utc,
    resolve_media_id,
    upsert_coverage,
)

SOURCE = "4chan"
BOARD = "biz"
CATALOG = "https://a.4cdn.org/{board}/catalog.json"
THREAD = "https://a.4cdn.org/{board}/thread/{tid}.json"
POST_URL = "https://boards.4chan.org/{board}/thread/{tid}#p{pid}"
MEDIA_NAME, MEDIA_CHANNEL, MEDIA_TIER = "4chan /biz/", "community", "community"
REQUEST_DELAY_SEC = 1.2

_TAG_RE = re.compile(r"<[^>]+>")
_QUOTE_RE = re.compile(r"&gt;&gt;\d+")


def _clean(raw: str | None) -> str:
    """HTML 조각을 평문으로. 인용(>>123456)은 제거한다 — 답글 참조일 뿐 내용이 아니다."""
    if not raw:
        return ""
    txt = _QUOTE_RE.sub(" ", raw)
    txt = txt.replace("<br>", " ").replace("<br/>", " ")
    txt = _TAG_RE.sub(" ", txt)
    return " ".join(html.unescape(txt).split())


def _get(url: str, since: str | None = None):
    headers = dict(HTTP_HEADERS)
    if since:
        headers["If-Modified-Since"] = since
    resp = requests.get(url, headers=headers, timeout=CRAWL_TIMEOUT_SEC + 5)
    time.sleep(REQUEST_DELAY_SEC)
    return resp


def _matches(text: str, terms: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(t.lower() in low for t in terms if t)


def crawl(conn, code: str, terms: tuple[str, ...], board: str = BOARD,
          max_threads: int = 60, fetch_bodies: bool = True, progress=print) -> dict:
    """살아있는 스레드에서 terms가 언급된 글을 수집한다.

    terms는 엔티티 별칭(예: bitcoin/BTC)이다. /biz/ 전체가 아니라 대상 엔티티가
    언급된 글만 가져와야 종목별 감성이 된다.
    """
    collected = now_utc()
    media_id = resolve_media_id(conn, f"4chan /{board}/", MEDIA_CHANNEL, MEDIA_TIER)
    seen = existing_urls(conn, code, SOURCE)

    stats = {"threads_scanned": 0, "threads_fetched": 0, "saved": 0, "requests": 1}
    resp = _get(CATALOG.format(board=board))
    if resp.status_code != 200:
        progress(f"  [경고] 카탈로그 HTTP {resp.status_code}")
        return stats

    threads = [t for page in resp.json() for t in page.get("threads", [])]
    stats["threads_scanned"] = len(threads)

    # 대상 엔티티가 제목/본문에 언급된 스레드만 고른다. 카탈로그에는 최근 댓글
    # 일부만 들어 있으므로, 매칭된 스레드는 전체를 다시 받아온다.
    hits = []
    for t in threads:
        blob = f"{t.get('sub') or ''} {t.get('com') or ''}"
        if _matches(_clean(blob), terms):
            hits.append(t)
    hits.sort(key=lambda t: -(t.get("replies") or 0))
    hits = hits[:max_threads]

    batch, dates = [], set()
    for t in hits:
        tid = t["no"]
        posts = [t]
        if fetch_bodies:
            r = _get(THREAD.format(board=board, tid=tid))
            stats["requests"] += 1
            if r.status_code == 200:
                posts = r.json().get("posts", [t])
                stats["threads_fetched"] += 1
            elif r.status_code == 404:
                continue   # 수집 중 만료됨

        replies = t.get("replies") or 0
        for p in posts:
            text = _clean(f"{p.get('sub') or ''} {p.get('com') or ''}")
            if len(text) < 12 or not _matches(text, terms):
                continue
            ts = p.get("time")
            if not ts:
                continue
            pub = datetime.fromtimestamp(ts, tz=timezone.utc)
            url = POST_URL.format(board=board, tid=tid, pid=p["no"])
            if url in seen:
                continue
            seen.add(url)
            dates.add(pub.astimezone().strftime("%Y-%m-%d"))
            batch.append({
                "url": url,
                # /biz/ 글은 제목이 없는 경우가 대부분이라 본문 앞부분을 제목으로 쓴다
                # (종목토론방과 같은 취급 — 목록 텍스트가 곧 분석 대상).
                "title": text[:300],
                "author": p.get("name") or "Anonymous",
                "engagement": replies,
                "published_utc": pub.isoformat(timespec="seconds"),
                "collected_utc": collected,
                "media_id": media_id,
                "ts_confidence": "exact",
            })

    stats["saved"] = insert_documents(conn, code, SOURCE, batch, terms)
    for d in sorted(dates):
        # 카탈로그에 살아있는 것만 받으므로 그 날짜를 다 훑었다고 말할 수 없다.
        upsert_coverage(conn, code, SOURCE, d, "partial", doc_count=stats["saved"])
    mark_duplicates(conn, code, dates)
    conn.commit()
    progress(f"  [{code}] 스레드 {stats['threads_scanned']}개 중 매칭 {len(hits)}개 "
             f"-> 신규 {stats['saved']:,}건 (요청 {stats['requests']}회)")
    return stats
