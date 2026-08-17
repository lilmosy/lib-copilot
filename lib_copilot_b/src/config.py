"""파이프라인 설정. 모델·경로 등 한 곳에서 관리."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# .env는 lib_copilot/ 또는 상위(모노레포 루트 yozm_ai_agent/)에 있을 수 있다.
# 현재 위치에서 위로 거슬러 올라가며 첫 .env를 찾아 로드한다.
load_dotenv(find_dotenv(usecwd=True))

# LLM 모델 — 호출이 셋이라 상수도 셋. 지금은 같은 값이다.
#   **모델을 바꾸면 임계값 둘을 다시 찾아야 한다**(점수 성향이 모델마다 다르다).
#   2026-08-13에 Opus → terra로 갈아탔는데 기본값이 Opus로 남아 있어 코드와 실제가
#   어긋나 있었다. 회차 md의 "이 회차 조건"만 진실을 말하고 있었다 → 기본값을 맞춘다.
MODEL_PRIOR = os.environ.get("LIBCOPILOT_MODEL_PRIOR", "gpt-5.6-terra")     # 1차 082 정합성
MODEL_KEYWORD = os.environ.get("LIBCOPILOT_MODEL_KEYWORD", "gpt-5.6-terra")  # 키워드 추출
MODEL_MAIN = os.environ.get("LIBCOPILOT_MODEL", "gpt-5.6-terra")            # 최종 판단
MODEL = MODEL_MAIN                                                          # 하위호환

# classify 단계 최대 출력 토큰.
# adaptive thinking이 이 예산을 함께 쓴다. 서가 책을 상세정보까지 넣으면서
# 근거가 길어져 4000에서 JSON이 잘렸다(2026-08-01) → 넉넉히 둔다.
MAX_TOKENS = 16000

# ── 1차 검색기 선택 (funnel) ──
# "default": 082 + 종합서지 득표로 후보를 모으고, 서가 의미는 본교에서 읽는다 (retrieve.py)
# "embed":   임베딩 의미 유사 검색 (예정, retrieve_embed.py)
# 어느 구현이든 같은 RetrieveResult를 반환하므로 classify/pipeline은 안 바뀐다.
RETRIEVER = os.environ.get("LIBCOPILOT_RETRIEVER", "default")

# 1차 필터 파라미터
# 종합서지 득표 후보를 몇 위까지 볼까 (082는 이 상한과 무관하게 항상 들어간다).
#
# 15였는데 3으로 내렸다(2026-08-13). 15는 한 번도 안 걸린 값이라 사실상 상한이 없었고,
# **타대학 1곳만 준 번호까지 다 들어왔다.** 12건에서 3위까지만 남겨도 정답을 잃는 건이 0건이고,
# 오히려 오답이 빠진다 — `#12 일리아스`에서 두 모델이 여섯 회차 내내 골랐던 `883`(5곳)이
# 최다 `028.9`(11곳)의 절반도 안 되면서 4위였다. 프롬프트도 40% 줄어든다(#12: 10만자 → 6만자).
#
# ⚠️ **동점은 다 넣는다.** `#11 부린 왕자`가 179.9(3) · 811.36(1) · 332.6324(1)로
#    정답이 3위인데 뒤 둘이 동점이다. 딱 3개로 자르면 순서가 흔들릴 때 정답이 밀린다.
# ⚠️ 12건에서만 확인한 값이다. 실전에서 정답이 4위 이하로 밀리는 책이 나오면 올려야 한다.
MAX_UNION_CANDIDATES = int(os.environ.get("LIBCOPILOT_MAX_UNION", "3"))
MAX_CANDIDATE_NUMBERS = MAX_UNION_CANDIDATES        # 옛 이름(하위호환)
SHELF_SAMPLE = 40        # 후보 번호대에서 LLM에 보여줄 책 수 (가진 만큼 다, 상한 40)

# 서가 책 중 **앞 몇 권에 알라딘 책소개를 붙일까**(2026-08-12, 교수님 제안).
#
# 처음엔 10으로 뒀다가 40(=전부)으로 올렸다. 10권만 쏘니 실제로 붙는 건 3~5권뿐이었는데,
# 그 앞 10권이 하필 **주제명 우선 정렬 때문에 옛날 책**이었고 알라딘엔 옛날 책이 없다.
# 서가 표본을 반반(주제명 절반 + 최신 절반)으로 바꾸면서 최신 책이 뒤쪽에도 들어오므로
# 전부에 쏜다. 못 받은 책은 빈 채로 두면 그 줄이 안 찍힐 뿐이다.
#
# 비용은 **첫 회차만** 든다 — 서가 책은 안 바뀌므로 캐시(`data/aladin_cache.json`)가
# 그대로 재사용되고, 캐시 파일을 커밋해 팀원끼리 공유한다.
# 0으로 두면 알라딘을 안 쏜다(이 기능을 끄는 스위치).
SHELF_DESC_TOP = int(os.environ.get("LIBCOPILOT_SHELF_DESC_TOP", "40"))

# 서가 책소개를 프롬프트에 넣을 때 자를 길이. 마케팅 문구가 길게 이어지는 책이 있고,
# 서가 성격을 읽는 데는 앞부분이면 충분하다.
SHELF_DESC_CHARS = int(os.environ.get("LIBCOPILOT_SHELF_DESC_CHARS", "300"))

# ── 키워드 채널 (2026-08-18) ──────────────────────────────
# 종합서지는 실전에서 거의 비어 있다 — union_db.json은 골든셋 12권의 득표만 갖고 있고
# 새 책에는 타대학 득표가 없다(design.md §9). 그때 후보를 만들 유일한 경로가 키워드 검색이다.
# 사서도 실제로 그렇게 한다(제목·주제어로 본교를 뒤진다).
#
# ⚠️ **몇 개 걸려야 후보로 볼까** — 이게 이 채널의 주력 손잡이다.
#    OR(1개만 걸려도 통과)로 두면 흔한 낱말이 큰 서가를 통째로 끌어온다. 실측:
#      #7 게임철학  1개 이상 → 193(1435권)·100(797)·194(787) … 정답 794.801(6권)이 파묻힘
#      #10 이중언어 1개 이상 → 306.44(146)·400.19(54) … 정답 306.446(30)이 4위로 밀림
#                   2개 이상 → **306.446(17) 1위** · 400.42(3) ← 경합 두 번호가 나란히
#    → 2로 시작한다. 다만 조어 제목·희귀 주제에서 0개가 될 수 있어 폴백을 둔다.
KEYWORD_MIN_HIT = int(os.environ.get("LIBCOPILOT_KEYWORD_MIN_HIT", "2"))

# 위 문턱으로 걸러 후보 번호대가 이 수보다 적으면 1개 이상으로 완화해 재검색한다.
KEYWORD_FALLBACK_BELOW = int(os.environ.get("LIBCOPILOT_KEYWORD_FALLBACK_BELOW", "2"))

# 키워드 후보를 몇 개까지 프롬프트에 넣을까. 종합서지와 같은 폭(3)으로 시작한다.
# 실측에서 정답이 대체로 3~4위 안에 들어왔다(#10 1·2위 · #11 2위 · #12 3위 · #5 4위).
# ⚠️ 후보를 늘리면 오히려 나빠진 전례가 있다 — MAX_UNION을 15→3으로 줄이자
#    #12에서 두 모델이 여섯 회차 내내 고르던 오답 883이 빠졌다.
MAX_KEYWORD_CANDIDATES = int(os.environ.get("LIBCOPILOT_MAX_KEYWORD", "3"))

# 키워드 후보의 서가 표본. 082·종합서지(40권)보다 적게 준다 —
# 근거의 성격이 다르다: 종합서지는 "타대학이 **이 책**에 매긴 번호"(직접 증거)고
# 키워드는 "이 주제어가 걸린 자리"(간접 증거)다.
KEYWORD_SHELF = int(os.environ.get("LIBCOPILOT_KEYWORD_SHELF", "10"))

# 키워드 후보 머리줄에 찍을 650(일반주제명) 빈출어 개수.
# 10권만 보여주므로 못 보여준 나머지 서가의 성격을 이 한 줄이 대신 말해준다.
# ⚠️ **번호대 전체**를 센다(표본 40권이 아니라). 표본만 세면 우연에 흔들린다 — 실측:
#      102(철학 649권)  전체 Philosophy(140)·철학상담(6)  ↔  표본 Philosophy(7)·비교문법(3)
#    082·종합서지 후보에는 붙이지 않는다(40권이면 서가를 거의 다 보여주므로 중복).
SUBJECT_TOP = int(os.environ.get("LIBCOPILOT_SUBJECT_TOP", "3"))

# ── 앞 단계에서 이미 본 후보의 서가를 최종 판단에 다시 넣을까 ──
# 교수님 제안(2026-08-17): 앞 호출에서 판단한 후보는 점수만 넘기고 서가를 생략하면
# 토큰이 준다. 서가 책 1권이 약 187토큰이라 절약이 실재한다.
#   A에서 끄면  082 서가 40권이 빠진다 (약 7,500토큰)
#   B에서 끄면  082 40권 + 종합서지 3×40권 = 160권이 빠진다 (약 3만 토큰)
# ⚠️ 대가는 **정보 비대칭**이다 — 다른 후보는 서가 40권을 보여주고 이 후보만 숫자면,
#    LLM이 그 후보를 재검토할 방법이 없다. 실측에서 #4는 082가 1위→2위로 밀렸는데
#    (0.80→0.72) 서가가 없으면 그 판단이 안 나올 수 있다.
# 기본은 켜둔다(지금까지의 동작). 회차로 비교한다.
SEEN_SHELF = os.environ.get("LIBCOPILOT_SEEN_SHELF", "on").lower() != "off"

# ── 임계값 둘 — 사서 업무 순서 그대로 ────────────────────
# 사서는 082를 본교 서가에 대보고, 거기서 끝나면 종합서지를 펴지 않는다.
# 두 단계에 각각 문턱이 하나씩 붙는다.
#
#   THRESHOLD_1   1차 호출(082만 본 점수)이 이 값 이상 → **082로 확정. 종합서지로 안 간다.**
#   THRESHOLD_2   2차 호출(종합서지까지 본 점수) 1순위가 이 값 미만 → 사서에게 넘긴다.
#
# ⚠️ THRESHOLD_1을 낮추면 위험하다. 1차 점수는 082의 정오와 상관이 약하다 —
#    케이스7(082=102 '철학 일반', 정답 794.801 '게임')이 1차에서 0.85~0.90을 받았다.
#    그 번호대에 본교 책이 649권이나 있어서 뭘 갖다 대도 어울려 보인 것이다.
#    반대로 케이스6(082=363.19262, 사실상 정답)은 그 번호대 본교 책이 0권이라 건너뛴다.
#
# **0.90 → 0.92 (2026-08-18).** Opus 시절엔 관측 최고점이 0.85라 0.90이 "닫아둔 값"이었는데,
# terra로 갈아타니 점수 분포가 올라가 게이트가 실제로 열렸다. 그리고 열리자마자 오답을 냈다:
#      r1  #3(0.90)→641.3383 오답 · #5(0.91)→오답 · #7(0.90)→오답 · #9(0.95)→정답
#      r2  #5(0.90)→오답 · #7(0.90)→오답 · #9(0.96)→정답
#    0.92로 올리면 두 회차 모두 **#9만 통과하고 그건 정답**이다.
# ⚠️ 게이트로 확정된 건은 사서에게 안 넘어간다(pipeline._prior_confirmed가 escalate=False).
#    그게 게이트의 정의지만, 그래서 이 값이 틀리면 바로 "틀린 채 확정"이 된다.
# ⚠️ 표본이 4건뿐이고 terra 기준이다. 프롬프트를 고치면 다시 찾아야 한다.
THRESHOLD_1 = float(os.environ.get("LIBCOPILOT_THRESHOLD_1", "0.92"))

# 값 근거 (2026-08-05). **같은 조건 두 회차를 돌려 정했다** — 한 회차로 정하면 안 된다.
#   같은 책의 1순위 점수가 회차마다 최대 0.10 흔들린다
#     #10 어느 완벽한 이중언어자  0.62 ↔ 0.72
#     #3 향신료 0.88 ↔ 0.80 · #12 일리아스 0.72 ↔ 0.80 · #8 야구장 0.72 ↔ 0.65
#   그래서 임계값은 "한 회차에서 잘 드는 값"이 아니라 **두 회차 모두에서 안전한 값**이어야 한다.
#
#   0.80 → B회차에서 #12(일리아스)가 정확히 0.80을 찍어 **틀린 답이 확정됐다**(883, 정답 028.9).
#   0.82 → 두 회차 모두 바로 확정 6/12, 틀린 채 확정 0건. **여기로 정했다.**
#   0.85+ → 여전히 안전하지만 자동화만 4~5건으로 줄어든다.
#
# ⚠️ 프롬프트나 서가 표본(SHELF_SAMPLE)을 고치면 점수 분포가 통째로 움직인다.
#    그때는 **두 회차 이상 돌려 다시 찾아야 한다.**
# ⚠️ **이 값은 Opus 회차에서 뽑은 것이다.** terra + 키워드 채널 기준으로 아직 재산정하지
#    않았다. 회차를 돌린 뒤 저장된 json만으로 문턱을 쓸어보며 다시 정한다(재실행 불필요).
THRESHOLD_2 = float(os.environ.get("LIBCOPILOT_THRESHOLD_2", "0.82"))

# 경로
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
# 본교(서강대 로욜라) 장서 — MARC 화면 전량 크롤(884,577건, SQLite). 읽기 전용으로만 연다.
# 구 `sogang_db.json`(상세정보 화면 1,682건)을 대체했다. 파일명이 곧 모듈명이다(sogang_db.py).
COLLECTION_DB = DATA_DIR / "sogang_db_final.db"
UNION_PATH = DATA_DIR / "union_db.json"          # 종합서지 (KERIS 변환, 대학별 소장)
SCENARIO_DIR = DATA_DIR / "scenarios"
OUTPUT_DIR = PROJECT_ROOT / "output"   # 회차마다 <YYYYMMDD-HHMM>[_label].md + .json
RUNS_DIR = PROJECT_ROOT / "runs"       # 평가 정량 로그 jsonl

# 서강대 = 본교. 종합서지 voting에서 제외(자기 답 베끼기 방지, design.md §8)
HOME_LIBRARY_KEY = "서강"


# ── 골든셋 판정 ───────────────────────────────────────────
# 평가 대상 12권은 이미 본교 장서에 등록돼 있다. 그대로 조회하면 0차 승계가
# **정답을 그대로 베낀다**(케이스7에서 실제로 났다). 그래서 골든셋 책일 때만
# 그 책 자신을 조회에서 제외한다(holdout).
#
# 실전 새 책에는 켜면 안 된다 — 홀드아웃은 제목 기준이라 **같은 제목의 다른 판본까지**
# 빼버려서, 정작 업무 규칙인 "기존 판본 승계"(0차)를 막는다.
from functools import lru_cache as _lru


@_lru(maxsize=1)
def _golden() -> dict[str, tuple[str, ...]]:
    """{골든셋 제목: 본교 DB에서 뺄 레코드 ID들}.

    ID는 시나리오 JSON의 `holdout_ids`에 **못 박아 둔다**(2026-08-05).
    예전에는 조회할 때마다 제목으로 "이게 그 책 자신인가"를 추측했는데,
    그러다 **승계해야 할 다른 판본까지 빼버리는 사고**가 났다(바빌론 개정판 4권).
    무엇을 빼는지는 눈으로 확인할 수 있어야 하고, 규칙을 손대도 안 흔들려야 한다.
    """
    import json
    out = {}
    for p in SCENARIO_DIR.glob("*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        t = (d.get("input", {}).get("title") or "").strip()
        if t:
            out[t] = tuple(d.get("holdout_ids") or ())
    return out


def golden_titles() -> set[str]:
    return set(_golden())


def is_golden(title: str) -> bool:
    """이 제목이 골든셋 12건 중 하나인가 → holdout을 켤지 결정."""
    return (title or "").strip() in _golden()


def holdout_ids(title: str) -> tuple[str, ...]:
    """그 책 자신의 본교 DB 레코드 ID. 골든셋이 아니면 빈 튜플."""
    return _golden().get((title or "").strip(), ())
