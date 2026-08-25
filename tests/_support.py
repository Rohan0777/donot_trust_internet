"""테스트 공용 유틸 — 인메모리 DB 픽스처."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.conn import SCHEMA_PATH  # noqa: E402


def memory_conn() -> sqlite3.Connection:
    """schema.sql 전체를 적용한 인메모리 DB. 각 테스트가 독립 소유한다."""
    conn = sqlite3.connect(":memory:", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn
