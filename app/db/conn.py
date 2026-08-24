"""SQLite 연결 및 스키마 초기화."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import DB_PATH

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


@contextmanager
def get_conn(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# schema.sql 은 CREATE TABLE IF NOT EXISTS 라서 이미 있는 테이블에 컬럼을 더하지
# 못한다. 새 컬럼은 여기에 한 줄씩 추가한다 — 재실행해도 안전해야 한다.
_ADD_COLUMNS = {
    "prices": {"source": "TEXT"},
}


def _migrate(conn):
    for table, cols in _ADD_COLUMNS.items():
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone():
            continue
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(db_path=None):
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn)
