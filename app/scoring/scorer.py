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
import time
from dataclasses import dataclass, field

from openai import OpenAI

from app.config import OPENAI_API_KEY, OPENAI_MODEL, PROMPT_VERSION
from app.db.dao import now_utc
from app.scoring.prompts import build_system

LABEL_TO_INT = {"positive": 1, "negative": -1, "neutral": 0}
BATCH_SIZE = 25
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


def _pending(conn, code: str, limit: int | None, since_days: int | None, channel: str | None):
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
    if since_days is not None:
        sql += "AND d.published_utc >= datetime('now', ?) "
        params.append(f"-{int(since_days)} days")
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


def _score_batch(conn, client, channel, stock_name, rows, stats, purge, progress, depth=0):
    """실패하면 반으로 쪼개 재시도한다. 1건까지 좁혀도 실패하면 그 1건만 포기."""
    items = [{"id": r["doc_id"],
              "title": r["title"],
              "text": ((r["body"] or "")[:MAX_BODY_CHARS].replace("\n", " ") or None)}
             for r in rows]
    wanted = {r["doc_id"] for r in rows}

    for attempt in range(MAX_ATTEMPTS):
        try:
            data = _call(client, channel, stock_name, items)
            _apply(conn, data.get("results", []), wanted, stats, purge)
            conn.commit()
            stats.batches += 1
            return
        except Exception as exc:  # noqa: BLE001 - 모든 실패를 분할/재시도로 흡수한다
            stats.retries += 1
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            if len(rows) == 1:
                stats.failed += 1
                progress(f"    [포기] doc_id={rows[0]['doc_id']}: {type(exc).__name__}")
                return
            mid = len(rows) // 2
            progress(f"    [분할] {len(rows)}건 -> {mid}+{len(rows)-mid} ({type(exc).__name__})")
            _score_batch(conn, client, channel, stock_name, rows[:mid], stats, purge, progress, depth + 1)
            _score_batch(conn, client, channel, stock_name, rows[mid:], stats, purge, progress, depth + 1)
            return


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
                  purge_body: bool = True, progress=print) -> ScoreStats:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 미설정 (.env 확인)")

    rows = _pending(conn, code, limit, since_days, channel)
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

    done = 0
    for ch, bucket in buckets.items():
        for i in range(0, len(bucket), BATCH_SIZE):
            chunk = bucket[i:i + BATCH_SIZE]
            _score_batch(conn, client, ch, stock_name, chunk, stats, purge_body, progress)
            done += len(chunk)
            progress(f"  [{done:>5}/{total}] {done/total*100:5.1f}% | "
                     f"채점 {stats.scored:,} 무관 {stats.relevant_false} "
                     f"누락 {stats.missing} 포기 {stats.failed}")

    stats.inherited = inherit_duplicate_labels(conn, code)
    conn.commit()
    return stats
