"""프로젝트 전역 설정."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MODEL_DIR = BASE_DIR / "models"
DB_PATH = DATA_DIR / "tni.db"

DATA_DIR.mkdir(exist_ok=True)
MODEL_DIR.mkdir(exist_ok=True)

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

KST = ZoneInfo("Asia/Seoul")
UTC = ZoneInfo("UTC")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = "gpt-4o-mini"
# 프롬프트를 바꾸면 이 값을 반드시 올린다. 과거 라벨과 새 라벨이 섞이면
# 감성지수에 인공적인 단절(regime shift)이 생기고 백테스트가 그걸 학습한다.
PROMPT_VERSION = "v2"

NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
BIGKINDS_API_KEY = os.environ.get("BIGKINDS_API_KEY", "")

# --- 매체 등급 기본 가중치 (런타임 파라미터 — 조회 시점에 덮어쓸 수 있다) ---
DEFAULT_TIER_WEIGHTS = {
    "major": 5.0,
    "daily": 2.0,
    "online": 1.0,
    "unknown": 1.0,
    "blog": 0.05,
    "community": 0.001,
}

# --- 감성 집계 윈도우 ---
# [D-1 15:30 KST, D 08:50 KST] 에 게시된 글 = D일 개장 전에 알 수 있던 정보.
# 이 신호로 D일 시가에 체결한다 (익일 시가로 미루면 하루를 낭비한다).
WINDOW_START_HHMM = (15, 30)
WINDOW_END_HHMM = (8, 50)

# 글이 거의 없는 날의 감성지수 노이즈 보정: S_adj = S * n / (n + SHRINKAGE_K)
MIN_DOCS_PER_DAY = 20
SHRINKAGE_K = 10

# --- 중복 판정 ---
SIMHASH_MAX_DISTANCE = 3
FUZZY_MIN_RATIO = 95
MAX_DOCS_PER_AUTHOR_PER_DAY = 3

# --- 크롤러 ---
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
CRAWL_REQUEST_DELAY_SEC = 0.5
CRAWL_TIMEOUT_SEC = 10
