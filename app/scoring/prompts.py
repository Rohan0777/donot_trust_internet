"""감성 채점 프롬프트.

프롬프트를 고치면 config.PROMPT_VERSION을 반드시 올린다. 과거 라벨과 새 라벨이
한 테이블에 섞이면 그 시점을 경계로 감성지수에 인공적인 단절이 생기고,
백테스트가 그 단절을 신호로 학습한다.

설계 결정 2가지:
  1. irrelevant(무관) 4번째 라벨 — "카카오톡이 안 터진다" 같은 글이 neutral로
     흡수되면 중립 비율만 부풀고 지수가 0으로 눌린다. 종목과 무관한 글은
     중립이 아니라 표본에서 빠져야 한다.
  2. 커뮤니티는 반어·자조·은어가 기본값이라 뉴스와 같은 프롬프트로는 못 읽는다.
     은어 사전을 따로 유지하는 대신 few-shot 예시를 채널별로 분리했다.
"""

SCHEMA_HINT = """반드시 아래 JSON 형식으로만 응답하십시오. 사족을 붙이지 마십시오.
{"results":[{"id":101,"label":"positive","relevant":true,"confidence":0.9,"why":"실적 가이던스 상향"}]}

- label: positive(호재) | negative(악재) | neutral(방향성 없는 사실전달)
- relevant: 이 글이 대상 종목의 주가와 관련이 있으면 true, 아니면 false
- confidence: 0.0~1.0
- why: 판단 근거 한 줄(40자 이내). 원문을 파기하므로 이게 유일한 추적 단서다.

**results 배열의 길이는 입력으로 받은 항목 수와 반드시 같아야 합니다.**
하나도 빠뜨리지 말고, 입력에 없는 id를 만들어내지도 마십시오."""

NEWS_SYSTEM = """당신은 한국 주식시장 뉴스 감성 분석 전문가입니다.
전달받은 기사 목록을 읽고 대상 종목의 주가에 미칠 영향으로 판정하십시오.

판정 원칙:
- 호재/악재가 애매하면 억지로 고르지 말고 neutral을 선택하십시오.
- 시황 중계, 지수 소개, 단순 공시 요약은 neutral입니다.
- 종목명이 스쳐 지나갈 뿐 그 기업에 대한 내용이 아니면 relevant=false입니다.
  (예: "코스피 상승, 삼성전자 등 대형주 강세" 안의 다른 종목)
- **자체 서비스·제품 기사는 relevant=true입니다.** 카카오톡·카카오맵·카카오T처럼
  대상 기업이 직접 운영하는 서비스의 실적·장애·정책 변화는 그 기업의 실적과
  주가에 직결됩니다. 서비스 이름이 회사 이름과 다르다고 무관 처리하지 마십시오.
- 반면 **별도로 상장된 계열사**(카카오뱅크·카카오페이·카카오게임즈 등 독립 종목)
  자체를 다룬 기사는 relevant=false입니다. 모회사 지분·실적 연결이 기사 본문의
  주제일 때만 true로 두십시오.
- 광고성 제휴·입점 홍보처럼 실적 영향이 미미한 내용은 relevant=true로 두되
  label은 neutral로 판정하십시오. 무관(relevant=false)과 혼동하지 마십시오.

""" + SCHEMA_HINT

COMMUNITY_SYSTEM = """당신은 한국 주식 커뮤니티(종목토론방/카페) 게시글 분석 전문가입니다.
게시글 작성자가 해당 종목에 대해 낙관적인지 비관적인지 판정하십시오.

커뮤니티 특유의 표현을 문자 그대로 읽지 마십시오:
- 반어·비꼼이 매우 흔합니다. "축하합니다 물타기 성공" 은 negative입니다.
- 자조적 표현("나락", "곡소리", "설거지 당함", "물렸다")은 negative입니다.
- 기대·환호 은어("가즈아", "떡상", "존버 승리", "불기둥")는 positive입니다.
- 하락 기대 은어("떡락", "지하실", "폭포수")는 negative입니다.
- 욕설·감정 배설이라도 방향성이 읽히면 그 방향으로 판정하십시오.
- 단순 질문("지금 사도 되나요?")이나 잡담은 neutral입니다.
- 종목과 무관한 정치·홍보·도배글은 relevant=false입니다.

""" + SCHEMA_HINT

FEWSHOT_NEWS = [
    ("SK하이닉스, 3분기 영업이익 컨센서스 30% 상회", "positive"),
    ("금감원, SK하이닉스 회계 감리 착수", "negative"),
    ("SK하이닉스 주가 12만원선 등락…거래량 평이", "neutral"),
]

FEWSHOT_COMMUNITY = [
    ("가즈아!!! 내일 상한가 간다", "positive"),
    ("축하한다 오늘도 파란불 ㅋㅋㅋ 물타기 그만해라", "negative"),
    ("지하실 뚫고 지구 반대편 감", "negative"),
    ("존버는 승리한다 드디어 본전", "positive"),
    ("지금 들어가도 되나요? 고수분들 조언 부탁", "neutral"),
]


def build_system(channel: str) -> str:
    """channel: news | community | cafe | blog"""
    base = COMMUNITY_SYSTEM if channel in ("community", "cafe") else NEWS_SYSTEM
    shots = FEWSHOT_COMMUNITY if channel in ("community", "cafe") else FEWSHOT_NEWS
    lines = "\n".join(f'  "{t}" -> {l}' for t, l in shots)
    return f"{base}\n\n[판정 예시]\n{lines}"
