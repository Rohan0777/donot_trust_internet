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
PROMPT_VERSION = "v3"

NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

# --- 매체 등급 기본 가중치 (런타임 파라미터 — 조회 시점에 덮어쓸 수 있다) ---
DEFAULT_TIER_WEIGHTS = {
    "major": 5.0,
    "daily": 2.0,
    "online": 1.0,
    "unknown": 1.0,
    "blog": 0.05,
    # 종목토론방 기본 가중치 0. 데이터가 없어서가 아니라 커버리지가 불균형해서다.
    # 대형주는 하루 2만 건이 올라오는데 게시판 페이지네이션이 1,001p에서 끊긴다
    # (실측 SK하이닉스: 19,279건 = 1.5일치). 뉴스는 180일을 덮는데 커뮤니티는
    # 이틀뿐이라, 켜두면 그 이틀이 6개월 곡선 전체를 좌우한다.
    # 균형 잡힌 커뮤니티 소스를 확보하기 전까지는 사용자가 명시적으로 켜야 한다.
    "community": 0.0,
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
