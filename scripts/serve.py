"""웹 서버 실행기.

  python -m scripts.serve            # http://127.0.0.1:8000
  python -m scripts.serve --host 0.0.0.0 --port 8000 --reload

기본 바인딩은 127.0.0.1이다. 구 프로젝트는 ("", 8000)으로 전체 인터페이스에
노출된 채 CWD 전체를 정적 서빙해 .env가 읽혔다. 외부 공개는 명시적으로
--host 0.0.0.0을 줄 때만 일어난다.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.conn import get_conn


# 웹은 DB에 쓰지 않는다. 그런데 여기서 init_db()를 부르면 스키마 전체가 생성되어
# 서빙 스냅샷에 documents / raw_documents / coverage 가 **빈 테이블로 만들어진다.**
# 이 스냅샷의 존재 이유 중 하나가 "원문이 웹 호스트에 아예 존재하지 않는다"인데
# 그것이 무효가 되고, export_snapshot --verify 도 배포본에서 실패한다.
# 게다가 "이 DB에 X 테이블이 있는가"로 분기하는 코드가 전부 오작동한다.
_REQUIRED = ("entities", "sentiment_daily", "prices")


def _require_tables():
    with get_conn() as conn:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in _REQUIRED if t not in have]
    if missing:
        raise SystemExit(
            f"  서빙에 필요한 테이블이 없습니다: {', '.join(missing)}\n"
            "  스냅샷을 만들거나(python -m scripts.export_snapshot) "
            "TNI_DB가 올바른 파일을 가리키는지 확인하십시오.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    _require_tables()
    import uvicorn

    print(f"  http://{args.host}:{args.port}")
    uvicorn.run("app.api.server:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
