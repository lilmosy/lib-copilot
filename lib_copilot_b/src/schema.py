"""파이프라인 전 구간의 데이터 계약(스키마).

funnel 구조(docs/design.md):
  ① 1차 retrieve (recall 넓게) → RetrieveResult(후보 번호 + 근거)
  ② 2차 classify (precision, LLM) → ClassificationResult(▼h 후보 + 에스컬레이션)

retrieve는 어느 구현(retrieve[기본] / retrieve_embed[예정])이든 이 RetrieveResult를 반환한다.
내부 방식이 달라도 계약은 동일 → classify/pipeline은 불변.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ── ① 입력 ────────────────────────────────────────────────
class BookInput(BaseModel):
    """분류 대상 신규 도서의 서지 정보."""

    title: str
    subtitle: str | None = None
    author: str | None = None
    publisher: str | None = None
    pub_year: int | None = None

    is_translation: bool = False
    original_title: str | None = None
    original_lang: str | None = None

    # 082: 업체 제공 DDC. 선택 입력(prior).
    ddc_082: str | None = None

    keywords: list[str] = Field(default_factory=list)
    toc: list[str] = Field(default_factory=list, description="목차")
    description: str | None = Field(default=None, description="책 소개")

    # 알라딘이 받아오는 ISBN13. 지금은 **기록용**이다 — 판단에는 안 쓴다.
    # 나중에 종합서지를 ISBN으로 조회하게 될 때 열쇠가 된다(design.md §9.1).
    isbn: str | None = Field(default=None, description="ISBN13 (알라딘 보강)")


# ── ② 1차 retrieve 산출물 ─────────────────────────────────
class CollectionBook(BaseModel):
    """본교 장서 DB(sogang_db_final.db) 한 건 = 이미 청구기호가 붙은 책.

    `ddc_h`는 MARC 852 ▼h(사서가 확정한 배가 위치)다. `detail`은 MARC 화면을
    구 상세정보 화면과 같은 한글 라벨로 옮긴 것(`sogang_db._MARC_LABEL`).
    """

    id: str
    title: str
    author: str = ""
    publisher: str = ""
    pub_year: str = ""
    call_number: str = Field(default="", description="전체 청구기호 ▼h+▼i+▼m")
    ddc_h: str = Field(description="분류기호만 (▼h)")
    searched_callno: str = Field(default="", description="어느 번호 검색에서 걸렸나")
    isbn: str = Field(default="", description="MARC 020. 알라딘 상세 조회의 열쇠(전체의 73%)")
    description: str = Field(
        default="",
        description="알라딘 책소개. 서가 책 중 **ISBN이 있는 전부**에 붙는다(aladin.describe_many). "
                    "한국 책만 있다 — 양서는 알라딘에 없어서 빈다(2026-08-12).",
    )
    detail: dict[str, str] = Field(
        default_factory=dict,
        description="상세페이지 원자료(th/td 통째로). 상세크롤 전에는 빈 dict",
    )


class CandidateNumber(BaseModel):
    """후보 청구기호(▼h) 하나 + 근거.

    1차가 082/종합서지/키워드로 후보 번호를 건진 뒤, 각 번호대의 본교 책을 붙인다.
    2차(LLM)는 shelf_books로 '주제'를 파악한다. 개수(votes/hits/count)는 보조 신호일 뿐이다.

    **번호대 하나당 후보 하나다.** 같은 번호가 여러 채널에서 나오면 후보를 새로 만들지 않고
    `sources`에 라벨만 더한다 — 같은 서가 40권을 프롬프트에 두 번 찍는 낭비를 막고,
    "증거가 겹쳤다"는 사실은 라벨로 남는다(retrieve.merge_candidates).
    """

    ddc_h: str
    is_082_prior: bool = Field(default=False, description="082 업체번호에서 온 후보인가")

    # 어느 채널에서 온 후보인가 — {"082"} | {"종합서지"} | {"종합서지","키워드검색"} …
    # 프롬프트 머리줄에 `[출처: 종합서지 · 키워드검색]`으로 찍힌다.
    sources: set[str] = Field(default_factory=set, description="이 번호를 데려온 채널들")

    union_votes: int = Field(
        default=0, description="종합서지(타대학) DDC 득표수 — 서강 제외. 순서 힌트일 뿐"
    )
    keyword_hits: int = Field(
        default=0, description="키워드 검색에서 이 번호대에 걸린 본교 책 수 (내부 정렬용)"
    )
    subject_top: list[tuple[str, int]] = Field(
        default_factory=list,
        description="이 번호대 **전체**의 650 빈출 주제명 상위 몇 개. "
                    "키워드 후보에만 붙인다 — 10권만 보여주므로 못 보여준 서가를 이 줄이 대신한다.",
    )
    shelf_count: int = Field(
        default=0,
        description="본교 이 번호대 책 수 — **전량이라 정확하다**(2026-08-05 DB 교체 후). "
                    "부정확한 건 아래 `shelf_books`(최대 40권 표본)뿐이다.",
    )
    shelf_books: list[CollectionBook] = Field(
        default_factory=list,
        description="이 번호대 본교 책 레코드 전체(detail 포함) — 서가 의미 파악용",
    )


class RetrieveResult(BaseModel):
    """1차 검색기 산출물 (검색기 구현이 달라도 이 계약은 동일).

    **업무 순서를 구조로 담는다** (design.md §2.1.1):
      prior_candidate  = 1단계. 082(업체번호) 하나 + 본교 서가 의미
      union_candidates = 2단계. 082가 안 맞을 때만 보는 종합서지 득표 후보
                         → **082 번호는 여기서 제외**한다. 1단계에서 이미 판단했고,
                           타대학이 같은 082를 승계했을 수 있어 독립 증거가 아니다.
    """

    retriever: str = Field(description="어느 구현이 만들었나 (default | embed | inheritance)")
    messages: list[str] = Field(
        default_factory=list, description="경과를 사람 말로 — 화면 근거 칸·로그에 그대로 쓴다"
    )

    prior_candidate: CandidateNumber | None = Field(
        default=None, description="1단계: 082 후보 (082가 없으면 None)"
    )
    union_candidates: list[CandidateNumber] = Field(
        default_factory=list, description="2단계: 종합서지 득표 후보 (082 제외)"
    )
    keyword_candidates: list[CandidateNumber] = Field(
        default_factory=list,
        description="3단계: 키워드 검색 후보 (082·종합서지에 이미 있는 번호는 제외)",
    )

    # 키워드 채널의 원자료 — **자른 뒤가 아니라 전량**을 남긴다.
    # ① 나중에 "몇 위까지 잘라야 사서가 본 책이 들어오나"를 재실행 없이 쓸어보려고
    # ② 꼬리(1~2권짜리 외딴 번호)가 재분류 큐의 씨앗이다(design.md §11.1)
    keyword_query: list[str] = Field(default_factory=list, description="LLM이 뽑은 검색어")
    keyword_hits_all: list[tuple[str, int]] = Field(
        default_factory=list, description="키워드 검색 결과 전량 [(청구기호, 걸린 권수)]"
    )
    keyword_min_hit: int = Field(
        default=0, description="실제로 적용된 문턱 (폴백이 발동했으면 1)"
    )


# ── shelf_fit 정의 — **한 곳에서만 쓴다** ──────────────────
# 이 점수가 시스템의 흐름을 정한다(config.THRESHOLD_2와 비교해 자동확정/넘김이 갈린다).
# 그래서 정의가 흔들리면 안 되는데, 예전엔 프롬프트와 필드 설명 두 군데에 따로
# 적혀 있어 한쪽만 고치면 조용히 어긋났다. 여기 한 번 쓰고 양쪽이 가져다 쓴다.
#
# 튜닝하는 사람이 점수 성향을 바꾸고 싶으면 **이 문자열을 고친다.**
# 고치면 점수 분포가 통째로 움직이므로 THRESHOLD_2를 다시 찾아야 한다.
SHELF_FIT_RUBRIC = """1.0 = 그 서가 책들과 사실상 같은 성격이다.
    0.7 = 주제는 맞는데 성격(전문서/교양서, 이론/실용, 역사/안내)이 조금 다르다.
    0.4 = 큰 갈래는 겹치지만 초점이 다르다.
    0.0 = 전혀 다른 자리다."""

SHELF_FIT_DESC = (
    "본교 이 번호대 서가에 꽂힌 책들과 이 도서의 주제·성격 적합도(0.0~1.0). "
    "'이 자리에 꽂혀도 어울리나'이지 '이 답이 맞을 확률'이 아니다. "
    "후보끼리 비교 가능하도록 같은 잣대로 매긴다."
)


# ── ③ 2차 classify 산출물 ─────────────────────────────────
class PriorCheck(BaseModel):
    """1차 호출 산출물 — **082(업체번호) 하나만** 놓고 본 정합성.

    2차와 분리한 이유(교수님 지시, 실제 업무 순서): 사서는 082를 먼저 본교 서가에 대보고,
    거기서 끝나지 않을 때만 종합서지를 편다. 한 번에 다 던지면 그 순서가 뭉개진다.

    ⚠️ `shelf_fit`은 **2차의 점수와 척도가 다르다**(경쟁자 없이 082 혼자 매긴 점수).
       그래서 문턱도 따로다 — 이 점수는 `config.THRESHOLD_1`, 2차 점수는 `THRESHOLD_2`가
       받는다. **두 점수를 한 줄에 놓고 비교하거나 같은 문턱을 대지 말 것.**
    """

    shelf_fit: float = Field(ge=0.0, le=1.0, description="082 번호대에 대한 " + SHELF_FIT_DESC)
    verdict: str = Field(
        description="왜 그 점수인지 1~2문장. 그 번호대 본교 서가가 무슨 자리였고 "
                    "이 책과 어디가 맞고 어디가 안 맞는지. 사서가 그대로 읽을 문장으로."
    )


class KeywordExtraction(BaseModel):
    """키워드 추출 호출 산출물 — 본교 장서를 뒤질 검색어.

    **082 서가를 보여주지 않고 부른다.** 서가를 보여주면 그 어휘로 물들어서, 082가 오답인
    케이스에서 키워드가 오답 쪽으로 끌려간다 — 그런데 082가 오답인 케이스야말로
    키워드 검색이 필요한 자리다(classify.extract_keywords 참고).

    ⚠️ 검색은 `title`과 `marc_650`에 LIKE로 걸린다. 650은 **LCSH 영어 표목**이라
       한국어만 뽑으면 거의 안 걸린다 — 실측: '이중언어' 3권 ↔ 'bilingual' 23권+.
    ⚠️ '역사'·'연구'·'입문' 같은 형식어를 넣으면 수천 권짜리 대서가가 끌려와 정답이 묻힌다.
       실측: ['향신료','spices','향료','spice'] → 정답 641.338309 나옴 /
            여기에 '역사','history'를 더하면 → 901(82권)·909(52권)에 파묻힘.
    """

    keywords: list[str] = Field(
        min_length=3, max_length=5,
        description="본교 장서 검색용 주제어 3~5개. 한국어와 영어(LCSH 표목 형식)를 섞는다. "
                    "주제를 특정하는 말만 — 형식어·장르어는 넣지 않는다.",
    )


class Candidate(BaseModel):
    """▼h 후보 하나 + 판단 근거(XAI) + 적합도 점수.

    ⚠️ '어디서 온 후보인가'(구 CandidateSource)를 다시 넣지 말 것 —
       `h == book.ddc_082` / `PipelineOutput.inherited`로 계산된다.

    ⚠️ `shelf_fit`은 구 `confidence`와 다르다. confidence는 프롬프트에 정의가 없어
       LLM 자의로 채워지던 값이었다. 이쪽은 **"본교 그 번호대 서가 책들과의 주제·성격 적합도"**로
       정의가 프롬프트에 박혀 있다. **이 점수가 흐름을 정한다** —
       `pipeline.decide_escalate()`가 `THRESHOLD_2`로 넘길지를 계산한다(2026-08-05).
    """

    h: str = Field(description="분류기호, 예: 720.2")
    shelf_fit: float = Field(ge=0.0, le=1.0, description=SHELF_FIT_DESC)
    label: str = Field(
        description="이 번호대가 무슨 주제인지 한 줄로. ⚠️ DDC 공식 항목명이 아니라 "
                    "서가 책들을 보고 네가 요약한 이름이다(화면에도 그렇게 표기한다)."
    )
    reasoning: str = Field(
        description="왜 이 번호인지. 서가의 어떤 책이 근거인지 제목을 들어 설명한다."
    )


class LLMJudgement(BaseModel):
    """2차 LLM이 **실제로 내놓는 것**. `escalate`가 여기 없는 게 핵심이다.

    예전엔 LLM이 escalate 불린을 직접 냈다. 그러면 넘기는 기준을 못 바꾼다 —
    불린은 숫자가 아니라 이미 내려진 결정이라, 회차 기록에 "문턱이 이랬으면"에
    해당하는 값이 없다. 기준을 바꾸려면 프롬프트를 고쳐 12건을 다시 돌려야 했고,
    실행마다 흔들려서 신호가 묻혔다(2026-08-05).

    지금은 LLM이 **점수만** 내고, 넘길지는 `pipeline.decide_escalate()`가 `config.THRESHOLD_2`로
    정한다. 저장된 회차 JSON만으로 문턱을 쓸어볼 수 있다(재실행 0회, 비용 0원).
    """

    candidates: list[Candidate] = Field(description="적합한 순서대로 후보(shelf_fit 내림차순)")
    notes: str | None = Field(
        default=None, description="판단 근거를 사람 언어로. 화면에 그대로 보여주는 문장."
    )


class ClassificationResult(BaseModel):
    """파이프라인 수준의 2차 산출물: LLM 점수 + **코드가 정한** 에스컬레이션.

    흐름을 정하는 스위치는 `escalate` 하나다(false=단일 답 / true=단일 강제 안 함).
    ⚠️ 이 값은 LLM이 채우지 않는다. `pipeline.decide_escalate()`가 `config.THRESHOLD_2`로 계산한다.

    ⚠️ 여기에 태그 필드를 다시 넣지 말 것:
      - 구 `signals`(cross_major/needs_toc/scattered_dist/org_disagree) — docx 부록1의
        '상 케이스 공통 신호'다. **참조는 프롬프트가 시키고, 이유는 notes가 문장으로 쓴다.**
        태그로 기록해봐야 흐름을 바꾸지 않았다(집계 말고는 쓰임이 없었다).
      - 구 `ambiguity`/`confidence` — 프롬프트에 정의가 없어 LLM 자의로 채워지던 값.
    임계값이 필요해지면 그때 정의와 함께 추가한다.
    """

    candidates: list[Candidate] = Field(description="적합한 순서대로 후보(shelf_fit 내림차순)")
    escalate: bool = Field(default=False, description="사람 사서 판단이 필요한가 (코드가 계산)")
    escalate_reason: str = Field(
        default="", description="왜 넘겼나/안 넘겼나 — 문턱과 실제 점수를 사람 말로. 화면·로그용"
    )
    notes: str | None = Field(
        default=None, description="판단 근거를 사람 언어로. 화면에 그대로 보여주는 문장."
    )

    # ⚠️ 구 `prior_verdict`(082 기각 사유)는 제거했다 — 호출을 둘로 나누면서
    #    1차 산출물 `PriorCheck.verdict`가 그 자리를 그대로 대신한다(2026-08-05).
