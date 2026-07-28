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

from app.db.conn import init_db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    init_db()
    import uvicorn

    print(f"  http://{args.host}:{args.port}")
    uvicorn.run("app.api.server:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
