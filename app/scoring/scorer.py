"""LLM 감성 채점기.

구 파이프라인의 실패 3가지를 구조적으로 막는다:
  1. 배치 30건 중 예외가 나면 30건 전부를 버렸다 -> 반으로 쪼개 재시도하고,
     1건까지 좁혀도 실패하면 그 1건만 포기한다.
  2. 응답에서 누락된 id는 영구 미채점으로 남았다 -> 누락분을 집계해 반환하고
     다음 실행에서 자동으로 다시 대상이 된다.
  3. 본문을 300자로 자르고 곧바로 파기했다 -> 채점 성공 시에만 raw_documents를
     지우고, 판단 근거(why)를 남긴다.

중복 그룹의 대표(is_canonical=1)만 채점한다. 재배포본은 대표의 라벨을 상속하므로
채점 비용이 중복률만큼 줄어든다.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from openai import OpenAI

from app.config import (NON_OPINION_CHANNELS, OPENAI_API_KEY, OPENAI_MODEL,
                        PROMPT_VERSION)
from app.db.dao import now_utc
from app.scoring.prompts import build_system

LABEL_TO_INT = {"positive": 1, "negative": -1, "neutral": 0}
BATCH_SIZE = 15
# LLM 호출은 네트워크 대기가 지배적이라 동시성이 거의 선형으로 확장된다
# (실측 1.6 -> 49.4건/초, 16워커에서 31배). SQLite는 단일 writer이므로
# 호출만 병렬로 하고 DB 반영은 메인 스레드에서 직렬로 처리한다.
MAX_WORKERS = 12
# 커뮤니티 글의 최소 반응(조회수). 이 미만은 아무도 읽지 않은 글이므로 "여론"으로 치지
# 않는다. 상한에 걸린 날만 반응순으로 자르면 표본 규칙이 날짜마다 달라져 지수에
# 계단이 생기므로, 모든 날에 같은 하한을 적용한다.
COMMUNITY_MIN_ENGAGEMENT = 20
MAX_BODY_CHARS = 600
REQUEST_TIMEOUT = 60.0
MAX_ATTEMPTS = 3


@dataclass
class ScoreStats:
    scored: int = 0
    relevant_false: int = 0
    missing: int = 0
    failed: int = 0
    batches: int = 0
    retries: int = 0
    inherited: int = 0
    by_label: dict = field(default_factory=lambda: {"positive": 0, "negative": 0, "neutral": 0})


def _pending(conn, code: str, limit: int | None, since_days: int | None, channel: str | None,
             daily_cap: int | None = None, include_non_opinion: bool = False):
    """미채점 대표글 목록.

    daily_cap이 있으면 (종목,날짜)당 그 수만큼만 채점 대상으로 삼는다. 일별 집계가
    목적이므로 하루치를 전수 채점할 필요가 없다 — 실측상 표본 200건이면 전수 대비
    극성지수 오차가 0.037(범위 -1~+1)이다. 200건을 넘는 (종목,일)은 5.8%뿐이지만
    그 소수에 문서가 극단적으로 몰려 있어(최대 11,311건/일) 채점량은 49% 줄어든다.

    표본 기준은 채널에 따라 다르다:
      뉴스      - 매체 등급 순 (major -> daily -> online ...)
      커뮤니티  - 반응 순 (조회수/추천). 아무도 안 읽은 글은 "여론"이 아니다.

    [주의: 표본 규칙이 날짜마다 달라지면 지수에 인공적 단절이 생긴다]
    상한에 걸린 날은 "반응 상위 200건", 안 걸린 날은 "그날 전부"가 된다. 두 집단은
    성격이 다르므로(전자는 합의된 의견, 후자는 혼잣말 포함) 물량이 상한을 넘나드는
    구간에서 지수가 계단처럼 튄다. 백테스트는 이 계단을 신호로 학습한다.

    그래서 커뮤니티는 상한 미만인 날에도 동일한 반응 하한을 적용한다
    (COMMUNITY_MIN_ENGAGEMENT). 모든 날에 같은 규칙이 걸리므로 단절이 없다.
    """
    sql = (
        "SELECT d.doc_id, d.title, m.channel, r.body FROM documents d "
        "JOIN media m ON d.media_id = m.media_id "
        "LEFT JOIN raw_documents r ON r.doc_id = d.doc_id "
        "WHERE d.code = ? AND d.label IS NULL AND d.is_canonical = 1 "
    )
    params: list = [code]
    if channel:
        sql += "AND m.channel = ? "
        params.append(channel)
    elif not include_non_opinion:
        # 가중치 0으로 고정된 채널은 채점 대상에서 뺀다. --channel 로 명시하면
        # 그 선택이 이긴다(디버깅·재분류용).
        marks = ",".join("?" * len(NON_OPINION_CHANNELS))
        sql += f"AND m.channel NOT IN ({marks}) "
        params.extend(NON_OPINION_CHANNELS)
    if since_days is not None:
        sql += "AND d.published_utc >= datetime('now', ?) "
        params.append(f"-{int(since_days)} days")

    # 커뮤니티는 반응 하한을 모든 날에 동일하게 적용한다(위 주석 참조).
    # engagement가 NULL인 건 구버전 수집분이라 하한을 적용하지 않는다.
    sql += ("AND (m.channel NOT IN ('community','cafe') OR d.engagement IS NULL "
            f"     OR d.engagement >= {int(COMMUNITY_MIN_ENGAGEMENT)}) ")

    if daily_cap:
        # 뉴스는 매체 등급 순, 커뮤니티는 반응 순으로 상위 N건을 남긴다.
        order = (
            "  PARTITION BY d.published_kst_date ORDER BY "
            "    CASE WHEN m.channel IN ('community','cafe') THEN 1 ELSE 0 END, "
            "    CASE WHEN m.channel IN ('community','cafe') THEN NULL ELSE "
            "      CASE m.tier WHEN 'major' THEN 0 WHEN 'daily' THEN 1 WHEN 'online' THEN 2 "
            "                  WHEN 'unknown' THEN 3 WHEN 'blog' THEN 4 ELSE 5 END END, "
            "    COALESCE(d.engagement, -1) DESC, d.doc_id"
        )
        sql = (
            "SELECT doc_id, title, channel, body FROM (" + sql.replace(
                "SELECT d.doc_id, d.title, m.channel, r.body",
                "SELECT d.doc_id, d.title, m.channel, r.body, ROW_NUMBER() OVER (" + order + ") rn"
            ) + ") WHERE rn <= ? ORDER BY doc_id DESC"
        )
        params.append(int(daily_cap))
    else:
        sql += "ORDER BY d.published_utc DESC"

    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def _call(client, channel: str, stock_name: str, items: list[dict]) -> dict:
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": build_system(channel)},
            {"role": "user",
             "content": f"[대상 종목: {stock_name}]\n[분석 대상]\n"
                        + json.dumps(items, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        timeout=REQUEST_TIMEOUT,
    )
    return json.loads(resp.choices[0].message.content)


def _apply(conn, results: list[dict], wanted: set[int], stats: ScoreStats, purge: bool):
    got = set()
    for res in results:
        try:
            doc_id = int(res.get("id"))
        except (TypeError, ValueError):
            continue
        if doc_id not in wanted:
            continue
        label_str = str(res.get("label", "")).strip().lower()
        if label_str not in LABEL_TO_INT:
            continue
        relevant = res.get("relevant", True)
        relevant = 0 if relevant in (False, "false", 0) else 1
        why = (res.get("why") or "")[:150] or None
        try:
            conf = float(res.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0

        conn.execute(
            "UPDATE documents SET label = ?, is_relevant = ?, confidence = ?, ai_reasoning = ?, "
            "label_model = ?, prompt_version = ?, labeled_at = ? WHERE doc_id = ?",
            (LABEL_TO_INT[label_str], relevant, conf, why,
             OPENAI_MODEL, PROMPT_VERSION, now_utc(), doc_id),
        )
        if purge:
            conn.execute("DELETE FROM raw_documents WHERE doc_id = ?", (doc_id,))
        got.add(doc_id)
        stats.scored += 1
        stats.by_label[label_str] += 1
        if not relevant:
            stats.relevant_false += 1
    stats.missing += len(wanted - got)


def _fetch_batch(client, channel, stock_name, rows, stats, progress, lock):
    """LLM 호출만 담당한다. DB는 건드리지 않는다(스레드에서 실행되므로).
    실패하면 반으로 쪼개 재시도하고, 1건까지 좁혀도 실패하면 그 1건만 포기한다.
    반환: [(rows_chunk, results)] — 호출자가 메인 스레드에서 직렬 반영한다."""
    items = [{"id": r["doc_id"],
              "title": r["title"],
              "text": ((r["body"] or "")[:MAX_BODY_CHARS].replace("\n", " ") or None)}
             for r in rows]

    for attempt in range(MAX_ATTEMPTS):
        try:
            data = _call(client, channel, stock_name, items)
            return [(rows, data.get("results", []))]
        except Exception as exc:  # noqa: BLE001 - 모든 실패를 분할/재시도로 흡수한다
            with lock:
                stats.retries += 1
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            if len(rows) == 1:
                with lock:
                    stats.failed += 1
                progress(f"    [포기] doc_id={rows[0]['doc_id']}: {type(exc).__name__}")
                return []
            mid = len(rows) // 2
            progress(f"    [분할] {len(rows)}건 -> {mid}+{len(rows)-mid} ({type(exc).__name__})")
            return (_fetch_batch(client, channel, stock_name, rows[:mid], stats, progress, lock)
                    + _fetch_batch(client, channel, stock_name, rows[mid:], stats, progress, lock))
    return []


def inherit_duplicate_labels(conn, code: str) -> int:
    """대표글의 라벨을 같은 중복 그룹의 재배포본에 상속시킨다.
    재배포본을 따로 채점하는 것은 같은 텍스트에 돈을 두 번 쓰는 것이다."""
    cur = conn.execute(
        "UPDATE documents SET label = (SELECT c.label FROM documents c "
        "  WHERE c.dup_group_id = documents.dup_group_id AND c.code = documents.code"
        "    AND c.published_kst_date = documents.published_kst_date AND c.is_canonical = 1), "
        " is_relevant = (SELECT c.is_relevant FROM documents c "
        "  WHERE c.dup_group_id = documents.dup_group_id AND c.code = documents.code"
        "    AND c.published_kst_date = documents.published_kst_date AND c.is_canonical = 1), "
        " label_model = 'inherited', prompt_version = ?, labeled_at = ? "
        "WHERE code = ? AND is_canonical = 0 AND label IS NULL AND dup_group_id IS NOT NULL "
        "  AND EXISTS (SELECT 1 FROM documents c WHERE c.dup_group_id = documents.dup_group_id "
        "    AND c.code = documents.code AND c.published_kst_date = documents.published_kst_date "
        "    AND c.is_canonical = 1 AND c.label IS NOT NULL)",
        (PROMPT_VERSION, now_utc(), code),
    )
    return cur.rowcount


def score_pending(conn, code: str, stock_name: str, *, limit: int | None = None,
                  since_days: int | None = None, channel: str | None = None,
                  purge_body: bool = True, daily_cap: int | None = None,
                  include_non_opinion: bool = False,
                  progress=print) -> ScoreStats:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 미설정 (.env 확인)")

    rows = _pending(conn, code, limit, since_days, channel, daily_cap,
                    include_non_opinion=include_non_opinion)
    stats = ScoreStats()
    if not rows:
        progress(f"[score] {code}: 미채점 대표글이 없습니다.")
        stats.inherited = inherit_duplicate_labels(conn, code)
        conn.commit()
        return stats

    client = OpenAI(api_key=OPENAI_API_KEY)
    # 채널마다 프롬프트가 다르므로 채널별로 묶는다.
    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(r["channel"] or "news", []).append(r)

    total = len(rows)
    progress(f"[score] {code}({stock_name}): 대표글 {total:,}건 채점 시작 "
             f"(채널 {', '.join(f'{k}:{len(v)}' for k, v in buckets.items())})")

    # 채널별 배치를 만들어 한 번에 워커풀에 던진다.
    jobs: list[tuple[str, list]] = []
    for ch, bucket in buckets.items():
        for i in range(0, len(bucket), BATCH_SIZE):
            jobs.append((ch, bucket[i:i + BATCH_SIZE]))

    lock = threading.Lock()
    done = 0
    workers = max(1, min(MAX_WORKERS, len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_batch, client, ch, stock_name, chunk, stats, progress, lock): chunk
                   for ch, chunk in jobs}
        for fut in as_completed(futures):
            chunk = futures[fut]
            try:
                pairs = fut.result()
            except Exception as exc:  # noqa: BLE001
                progress(f"    [배치 실패] {type(exc).__name__}: {exc}")
                pairs = []
            # DB 반영은 메인 스레드에서만 (SQLite 단일 writer).
            for rows_chunk, results in pairs:
                _apply(conn, results, {r["doc_id"] for r in rows_chunk}, stats, purge_body)
                stats.batches += 1
            conn.commit()
            done += len(chunk)
            if done % (BATCH_SIZE * workers) < BATCH_SIZE or done >= total:
                progress(f"  [{done:>6}/{total}] {done/total*100:5.1f}% | "
                         f"채점 {stats.scored:,} 무관 {stats.relevant_false:,} "
                         f"누락 {stats.missing} 포기 {stats.failed}")

    stats.inherited = inherit_duplicate_labels(conn, code)
    conn.commit()
    return stats
