"""크롤러 공용 HTTP 헬퍼.

[구 프로젝트에서 데이터 28,286건을 파손시킨 버그 — 절대 되돌리지 말 것]
finance.naver.com은 페이지마다 인코딩이 다르다:
    /item/news_news.naver  -> Content-Type: charset=EUC-KR
    /item/board.naver      -> Content-Type: charset=UTF-8   (종목토론방)
구 코드는 두 페이지 모두에 encoding="euc-kr"를 하드코딩해서, 종토방 제목이
전부 U+FFFD(복원 불가)로 깨진 채 저장됐다. 실측 28,114/28,286건(99.4%) 파손,
그 상태로 LLM 채점까지 나가서 99.1%가 neutral로 찍혔다.

따라서 인코딩은 호출부에서 지정하지 않는다. 서버 헤더 charset을 1순위로 쓰고,
헤더에 charset이 없을 때만 본문 바이트로 추정한다.
"""
import re
import time

import requests

from app.config import CRAWL_REQUEST_DELAY_SEC, CRAWL_TIMEOUT_SEC, HTTP_HEADERS

_META_CHARSET_RE = re.compile(rb'charset=["\']?([\w\-]+)', re.I)


def _resolve_encoding(resp: requests.Response) -> str:
    content_type = resp.headers.get("Content-Type", "")
    if "charset=" in content_type.lower():
        return resp.encoding or "utf-8"
    m = _META_CHARSET_RE.search(resp.content[:4096])
    if m:
        return m.group(1).decode("ascii", "ignore")
    return resp.apparent_encoding or "utf-8"


def get(url: str, referer: str | None = None) -> requests.Response:
    headers = dict(HTTP_HEADERS)
    if referer:
        # finance.naver.com의 일부 iframe 내부 페이지는 Referer 없이 호출하면
        # 빈 placeholder를 반환한다.
        headers["Referer"] = referer
    resp = requests.get(url, headers=headers, timeout=CRAWL_TIMEOUT_SEC)
    resp.encoding = _resolve_encoding(resp)
    time.sleep(CRAWL_REQUEST_DELAY_SEC)
    return resp


def assert_decoded(text: str, context: str, max_bad_ratio: float = 0.002) -> None:
    """디코딩 실패를 파이프라인 입구에서 잡는다. 저장된 뒤에는 복구가 불가능하므로
    조용히 넘어가는 대신 예외를 던진다."""
    if not text:
        return
    bad = text.count("�")
    if bad and bad / len(text) > max_bad_ratio:
        raise UnicodeError(
            f"{context}: 디코딩 실패 의심 (U+FFFD {bad}자 / 전체 {len(text)}자). "
            "인코딩 판정 로직을 확인하라."
        )
