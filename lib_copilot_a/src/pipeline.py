"""파이프라인 배선 — **버전 A (병합)**.

이 파일 하나가 `lib_copilot_a`와 `lib_copilot_b`의 유일한 차이다. 실제 일은 전부
다른 파일이 하고, 여기는 "누구를 어떤 순서로 부를까"만 정한다.

  ① 알라딘 입력 보강        비어 있는 칸만 (책소개·원제·부제)
  ② 0차 승계검색            원제 → 제목.  찾으면 종료.        LLM 0회
  ③ 082 후보 + 알라딘 A     종합서지는 아직 안 본다
  ④ LLM ① 082 정합성        ≥ THRESHOLD_1 → 082로 확정, 종료. LLM 1회
  ⑤ 종합서지 득표 3위
  ⑥ LLM ② 키워드 추출       책 정보만 (082 서가를 안 보여준다 — 오염 방지)
  ⑦ 키워드 검색             ③⑤에 이미 있는 번호는 출처 라벨만 추가
  ⑧ 알라딘 B                새로 생긴 후보 서가에만
  ⑨ LLM ③ 최종 판단         082 + 종합서지 + 키워드를 **한 자리에** 놓고 점수
  ⑩ 코드 판정               THRESHOLD_2로 넘길지 정한다

**A와 B의 차이는 ⑨ 하나뿐이다.** B는 ⑤ 다음에 "082+종합서지만 놓고 판단"하는 관문을
하나 더 두고, 거기서 확신하면 끝낸다. 즉 B는 확신도를 키워드 단계의 트리거로 쓴다.
그게 옳은지가 이번 비교의 질문이다 — 실측에서 #10은 0.88로 자동확정된 뒤 틀렸다.
확신하며 틀린 건은 B에서 키워드 단계를 영영 못 탄다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import aladin
import classify as _classify
import retrieve
import sogang_db
from classify import check_prior, classify, extract_keywords
from config import SEEN_SHELF, THRESHOLD_1, THRESHOLD_2
# 이 배선이 어느 버전인가. **a·b에서 다른 값을 갖는 유일한 상수**다 —
# evaluate.py가 회차 json에 찍는다. 여기 두는 이유는 evaluate.py를 두 벌로
# 갈라놓지 않으려는 것이다(run_all.sh의 동기화 검사가 그걸 잡는다).
VERSION = "A"   # 병합

from schema import (BookInput, Candidate, ClassificationResult,
                    KeywordExtraction, PriorCheck, RetrieveResult)


def decide_escalate(cands: list[Candidate]) -> tuple[bool, str]:
    """사람 사서에게 넘길 것인가 — **코드가 정한다**(2026-08-05).

    LLM은 점수(`shelf_fit`)만 낸다. 예전엔 LLM이 escalate 불린을 직접 냈는데,
    그러면 기준을 못 바꾼다(schema.LLMJudgement 참고). 임계값은 `config.THRESHOLD_2`이고,
    저장된 회차 JSON의 점수만으로 다시 쓸어볼 수 있다 — 재실행도 비용도 없다.
    """
    if not cands:
        return True, "후보가 하나도 없습니다. 검색이 아무것도 못 건졌습니다."
    top = cands[0]
    if top.shelf_fit < THRESHOLD_2:
        return True, (f"1순위 {top.h}의 서가 적합도가 {top.shelf_fit:.2f}로 "
                      f"임계값({THRESHOLD_2:.2f})에 못 미칩니다 — "
                      f"제시된 어느 번호대에도 뚜렷하게 어울리지 않습니다.")
    return False, (f"1순위 {top.h}의 서가 적합도가 {top.shelf_fit:.2f}로 "
                   f"임계값({THRESHOLD_2:.2f})을 넘습니다. 자동확정합니다.")


@dataclass
class PipelineOutput:
    retrieve: RetrieveResult
    result: ClassificationResult             # 최종 판단 산출물
    inherited: bool = False                  # 0차 승계로 끝났나 (LLM 안 씀)
    prior: PriorCheck | None = None          # 082 정합성 산출물 (082 없거나 승계면 None)
    keywords: KeywordExtraction | None = None  # 키워드 추출 산출물 (게이트로 끝나면 None)
    # LLM 호출 원물 — 프롬프트 전문·출력·토큰·지연. 회차 json이 이걸 쓴다.
    trace: list = field(default_factory=list)


def _inheritance_result(book: BookInput, rec) -> PipelineOutput:
    """0차 승계 산출물을 downstream과 같은 타입으로 합성(LLM 호출 없음)."""
    cand = Candidate(
        h=rec.ddc_h, shelf_fit=1.0, label="기존 서지/판본 승계",
        reasoning=(f"본교에 기존 판본/원서 「{rec.title}」"
                   f"(청구기호 {rec.call_number or rec.ddc_h})가 있어 그 청구기호를 승계합니다. "
                   f"결정론적 규칙(원서/기존 서지 우선)이라 주제 재추론이 불필요합니다."),
    )
    r = RetrieveResult(
        retriever="inheritance",
        messages=[f"0차 승계: 원제/기존서지 검색 → 「{rec.title}」({rec.ddc_h}) 발견 → 승계."],
    )
    res = ClassificationResult(
        candidates=[cand], escalate=False,
        escalate_reason="0차 승계는 결정론적 규칙이라 문턱을 대지 않습니다 — 사서가 볼 게 없습니다.",
        notes="0차 원제/기존서지 승계 — 본교 기존 청구기호를 그대로 계승(결정론, LLM 불필요).",
    )
    return PipelineOutput(retrieve=r, result=res, inherited=True)


def _prior_confirmed(book: BookInput, r: RetrieveResult, prior: PriorCheck) -> PipelineOutput:
    """082 게이트에서 확정된 산출물. 종합서지도 키워드도 부르지 않는다.

    ⚠️ **이 경로는 `decide_escalate`를 지나지 않는다** — escalate=False로 끝난다.
       그게 게이트의 정의다(하 케이스를 손 안 대고 끝내는 것). 그래서 THRESHOLD_1이
       틀리면 곧바로 "틀린 채 확정"이 된다. 0.90일 때 실제로 그랬다(config 주석).
    """
    h = r.prior_candidate.ddc_h
    cand = Candidate(
        h=h, shelf_fit=prior.shelf_fit, label="업체번호(082)로 확정",
        reasoning=prior.verdict,
    )
    r.messages.append(
        f"082({h})가 본교 서가와 {prior.shelf_fit:.2f}로 맞아 확정 — 종합서지·키워드를 보지 않음.")
    res = ClassificationResult(
        candidates=[cand], escalate=False,
        escalate_reason=(f"082 정합성 호출에서 {h}의 서가 적합도가 {prior.shelf_fit:.2f}로 "
                         f"임계값({THRESHOLD_1:.2f}) 이상입니다. 뒤 단계를 보지 않고 확정합니다."),
        notes=prior.verdict,
    )
    return PipelineOutput(retrieve=r, result=res, prior=prior,
                          trace=_classify.take_trace())


def classify_book(book: BookInput,
                  holdout: tuple[str, str] | None = None) -> PipelineOutput:
    """신규 도서 한 건을 관통시켜 ▼h 후보를 만든다.

    holdout=(title, author): 평가 시 '그 책 자신'을 본교 조회에서 제외(누출 방지).
      제목으로 시나리오 JSON의 `holdout_ids`를 찾아 **레코드 ID로** 뺀다. 데모/실전은 None.
      ⚠️ 키워드 검색에도 반드시 통과시킨다 — 안 걸면 그 책 자신이 걸려서 자기 번호를
         근거로 자기 번호를 찾는다(`794.801`은 6권인데 그중 1권이 「게임으로 철학하기」다).
    """
    # ── ① 입력 보강 ──
    # 트리거 없이 항상 부른다. "언제 쏠까"를 판정하면 정작 필요한 케이스를 놓친다.
    book = aladin.enrich(book)

    # ── ② 0차 승계 (있으면 종료) ──
    rec = sogang_db.find_inheritance(book, holdout=holdout)
    if rec is not None:
        return _inheritance_result(book, rec)

    # ── ③ 082 후보만 + 알라딘 A ──
    prior_cand, msgs = retrieve.retrieve_prior(book, holdout)
    if prior_cand is not None:
        retrieve.add_descriptions([prior_cand], book.title)
    r = RetrieveResult(retriever="default", messages=msgs, prior_candidate=prior_cand)

    # ── ④ LLM ① 082 정합성 → 게이트 ──
    # 사서는 082를 서가에 대보고 거기서 맞으면 다른 대학을 찾아보지 않는다.
    prior = check_prior(book, r)
    if prior is not None and prior.shelf_fit >= THRESHOLD_1:
        return _prior_confirmed(book, r, prior)

    # ── ⑤ 종합서지 ──
    prior_h = prior_cand.ddc_h if prior_cand else ""
    union_cands, m = retrieve.retrieve_union(book, prior_h, holdout)
    r.union_candidates = union_cands
    r.messages += m

    # ── ⑥ LLM ② 키워드 추출 ──
    kw = extract_keywords(book)

    # ── ⑦ 키워드 검색 + 병합 ──
    known = ([prior_cand] if prior_cand else []) + union_cands
    exclude = {c.ddc_h for c in known}
    kcands, rows, min_hit, m = retrieve.retrieve_keyword(kw.keywords, exclude, holdout)
    retrieve.merge_candidates(known, rows)     # 겹치는 번호는 출처 라벨만 추가
    r.keyword_candidates = kcands
    r.keyword_query = list(kw.keywords)
    r.keyword_hits_all = rows                  # 전량 — 재분류 큐·컷 재계산의 재료
    r.keyword_min_hit = min_hit
    r.messages += m

    # ── ⑧ 알라딘 B (새로 생긴 후보에만) ──
    retrieve.add_descriptions(union_cands + kcands, book.title)

    # ── ⑨ LLM ③ 최종 판단 ──
    # SEEN_SHELF=off면 앞에서 이미 본 후보의 서가를 생략한다. A에서 그 대상은 082 하나다.
    hide = set() if SEEN_SHELF else {c.ddc_h for c in ([prior_cand] if prior_cand else [])}
    judged = classify(book, r, prior=prior, hide_shelf=hide)

    # ── ⑩ 판정: **LLM이 아니라 코드가 문턱으로 정한다** ──
    esc, why = decide_escalate(judged.candidates)
    res = ClassificationResult(candidates=judged.candidates, notes=judged.notes,
                               escalate=esc, escalate_reason=why)
    return PipelineOutput(retrieve=r, result=res, prior=prior, keywords=kw,
                          trace=_classify.take_trace())
