"""네이버 종목토론방 수집기 (source='naver_board').

구 프로젝트 대비 바뀐 점 3가지:
  1. 인코딩을 호출부에서 지정하지 않는다. board.naver는 UTF-8이고 news_news.naver는
     EUC-KR인데, 구 코드가 둘 다 euc-kr로 강제해 제목 28,114건이 파손됐다.
  2. 작성자(author)를 수집한다. 도배글 일일 반영 상한 정책이 이것 없이는 불가능하다.
  3. coverage 원장의 completed 판정을 엄격히 한다 — 목표 날짜보다 오래된 글을
     실제로 본 날짜만 completed. 중단되면 partial + last_cursor(페이지)를 남긴다.

게시글 본문은 JS 렌더링이라 정적 크롤링으로 못 가져온다. 목록의 title 속성
(전체 제목)을 감성분석 대상 텍스트로 쓴다.
"""
from collections import defaultdict
from datetime import datetime, timedelta

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

SOURCE = "naver_board"
LIST_URL = "https://finance.naver.com/item/board.naver?code={code}&page={page}"
POST_URL = "https://finance.naver.com/item/board_read.naver?code={code}&nid={nid}"
MEDIA_NAME, MEDIA_CHANNEL, MEDIA_TIER = "네이버 종목토론방", "community", "community"


def _parse_page(html: str, code: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for row in soup.select("table.type2 tr"):
        a = row.select_one("td.title a")
        if not a:
            continue
        href = a.get("href", "")
        if "nid=" not in href:
            continue
        tds = row.select("td")
        if len(tds) < 3:
            continue
        try:
            published = datetime.strptime(tds[0].get_text(strip=True), "%Y.%m.%d %H:%M")
        except ValueError:
            continue
        out.append({
            "nid": href.split("nid=")[1].split("&")[0],
            "title": (a.get("title") or a.get_text(strip=True)).strip(),
            "author": tds[2].get_text(strip=True) or None,
            "published": published,
            "url": POST_URL.format(code=code, nid=href.split("nid=")[1].split("&")[0]),
        })
    return out


def crawl(conn, code: str, days_back: int = 30, max_pages: int = 3000,
          force: bool = False, progress=print) -> dict:
    """최신 페이지부터 거슬러 올라가며 cutoff까지 수집한다.

    force=False면 coverage에 completed/empty로 기록된 날짜는 건너뛴다.
    """
    cutoff = datetime.now() - timedelta(days=days_back)
    collected = now_utc()
    aliases = stock_aliases(conn, code)
    media_id = resolve_media_id(conn, MEDIA_NAME, MEDIA_CHANNEL, MEDIA_TIER)
    seen = existing_urls(conn, code, SOURCE)

    saved_by_date: dict[str, int] = defaultdict(int)
    seen_dates: set[str] = set()
    total_saved = 0
    last_page = 0
    reached_cutoff = False
    # 어떤 날짜를 "끝까지 훑었다"고 말하려면 그보다 오래된 글을 실제로 봐야 한다.
    oldest_seen: datetime | None = None

    for page in range(1, max_pages + 1):
        last_page = page
        resp = get(LIST_URL.format(code=code, page=page),
                   referer=f"https://finance.naver.com/item/board.naver?code={code}")
        assert_decoded(resp.text, f"{SOURCE} p{page}")
        rows = _parse_page(resp.text, code)
        if not rows:
            break

        batch = []
        for r in rows:
            if r["published"] < cutoff:
                reached_cutoff = True
                continue
            if oldest_seen is None or r["published"] < oldest_seen:
                oldest_seen = r["published"]
            d = r["published"].strftime("%Y-%m-%d")
            seen_dates.add(d)
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            batch.append({
                "url": r["url"], "title": r["title"], "author": r["author"],
                "published_utc": to_utc_iso(r["published"]),
                "collected_utc": collected, "media_id": media_id, "ts_confidence": "exact",
            })
            saved_by_date[d] += 1

        if batch:
            n = insert_documents(conn, code, SOURCE, batch, aliases)
            total_saved += n
        conn.commit()

        if page % 20 == 0:
            progress(f"  p{page} | 누적 {total_saved:,}건 | 최고(古) {oldest_seen}", flush=True)
        if reached_cutoff:
            break

    # 원장 갱신: 경계를 넘어간(더 오래된 글을 본) 날짜만 completed
    boundary = oldest_seen.strftime("%Y-%m-%d") if oldest_seen else None
    for d in sorted(seen_dates):
        complete = reached_cutoff and boundary is not None and d > boundary
        upsert_coverage(conn, code, SOURCE, d,
                        "completed" if complete else "partial",
                        doc_count=saved_by_date.get(d, 0),
                        last_cursor=None if complete else str(last_page))

    groups, demoted = mark_duplicates(conn, code, seen_dates)
    conn.commit()

    # 종료 사유를 구분해서 남긴다. 셋은 의미가 전혀 다르다:
    #   cutoff       - 요청한 기간을 다 채웠다 (정상)
    #   page_cap     - max_pages에 걸렸다 (상한을 올리면 더 받는다)
    #   board_end    - 게시판이 더는 페이지를 주지 않는다 (상한을 올려도 소용없다)
    # 대형주는 board_end가 흔하다. SK하이닉스는 1,001페이지에서 소진되는데
    # 하루 게시량이 2만 건이라 그래봐야 1.5일치다 — 종토방 장기 백필은 불가능하다.
    if reached_cutoff:
        stopped = "cutoff"
    elif last_page >= max_pages:
        stopped = "page_cap"
    else:
        stopped = "board_end"

    return {"saved": total_saved, "pages": last_page, "dates": len(seen_dates),
            "oldest": boundary, "dup_groups": groups, "dup_demoted": demoted,
            "stopped": stopped, "hit_page_cap": stopped == "page_cap"}
