"""파이프라인 배선 — **버전 B (조건부)**.

`lib_copilot_a`와 **이 파일 하나만** 다르다. 프롬프트·검색·병합·스키마는 글자까지 같다 —
그래야 "이 차이 때문이다"라고 말할 수 있다.

  ①②③④  A와 동일 (보강 → 승계 → 082 후보 → 082 정합성 → 게이트)
  ⑤ 종합서지 득표 3위 + 서가 40권 + 알라딘
  ⑥ ★LLM ② 082 + 종합서지만 놓고 판단        ← A에 없는 관문
      1위 ≥ THRESHOLD_2 → 확정, 종료 (LLM 2회)
  ⑦~⑪  A의 ⑥~⑩과 같은 함수 (키워드 추출 → 검색 → 병합 → 알라딘 → 최종 판단 → 코드 판정)

**이 관문이 이번 비교의 쟁점이다.** 트리거가 "LLM이 얼마나 확신하나"인데,
우리가 고치려는 건 *확신하며 틀린* 건이다. 실측에서 #10 「어느 완벽한 이중언어자」는
0.88로 자동확정된 뒤 틀렸다 — B에서는 그런 건이 키워드 단계를 영영 못 탄다.
반대로 B는 대부분의 책에서 후보를 3~4개로 유지한다. 후보를 15→3으로 줄여 정확도가
올라간 전례가 있으니(config.MAX_UNION_CANDIDATES) 그 이점도 실재한다. 그래서 재본다.
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
VERSION = "B"   # 조건부

from schema import (BookInput, Candidate, ClassificationResult,
                    KeywordExtraction, PriorCheck, RetrieveResult)


def decide_escalate(cands: list[Candidate]) -> tuple[bool, str]:
    """사람 사서에게 넘길 것인가 — **코드가 정한다**(2026-08-05).

    LLM은 점수(`shelf_fit`)만 낸다. 임계값은 `config.THRESHOLD_2`이고, 저장된 회차 JSON의
    점수만으로 다시 쓸어볼 수 있다 — 재실행도 비용도 없다.
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
    result: ClassificationResult
    inherited: bool = False
    prior: PriorCheck | None = None
    keywords: KeywordExtraction | None = None
    # B 전용 — 키워드 단계로 넘어갔나. 안 넘어갔으면 ⑥에서 끝난 것이다.
    went_keyword: bool = False
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
    """082 게이트에서 확정된 산출물. 뒤 단계를 부르지 않는다.

    ⚠️ 이 경로는 `decide_escalate`를 지나지 않는다 — 그게 게이트의 정의다.
       그래서 THRESHOLD_1이 틀리면 곧바로 "틀린 채 확정"이 된다.
    """
    h = r.prior_candidate.ddc_h
    cand = Candidate(h=h, shelf_fit=prior.shelf_fit,
                     label="업체번호(082)로 확정", reasoning=prior.verdict)
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
    """신규 도서 한 건을 관통시켜 ▼h 후보를 만든다 (버전 B)."""
    # ── ① 입력 보강 ──
    book = aladin.enrich(book)

    # ── ② 0차 승계 ──
    rec = sogang_db.find_inheritance(book, holdout=holdout)
    if rec is not None:
        return _inheritance_result(book, rec)

    # ── ③ 082 후보 + 알라딘 A ──
    prior_cand, msgs = retrieve.retrieve_prior(book, holdout)
    if prior_cand is not None:
        retrieve.add_descriptions([prior_cand], book.title)
    r = RetrieveResult(retriever="default", messages=msgs, prior_candidate=prior_cand)

    # ── ④ LLM ① 082 정합성 → 게이트 ──
    prior = check_prior(book, r)
    if prior is not None and prior.shelf_fit >= THRESHOLD_1:
        return _prior_confirmed(book, r, prior)

    # ── ⑤ 종합서지 + 알라딘 B ──
    prior_h = prior_cand.ddc_h if prior_cand else ""
    union_cands, m = retrieve.retrieve_union(book, prior_h, holdout)
    r.union_candidates = union_cands
    r.messages += m
    retrieve.add_descriptions(union_cands, book.title)

    # ── ⑥ ★LLM ② 082 + 종합서지만 놓고 판단 (A에 없는 관문) ──
    # A의 최종 판단과 **같은 프롬프트 파일**을 쓴다. 후보 목록만 다르다.
    hide = set() if SEEN_SHELF else {c.ddc_h for c in ([prior_cand] if prior_cand else [])}
    judged = classify(book, r, prior=prior, hide_shelf=hide)
    esc, why = decide_escalate(judged.candidates)
    if not esc:
        r.messages.append(
            f"082+종합서지 판단에서 1순위 {judged.candidates[0].h}가 "
            f"{judged.candidates[0].shelf_fit:.2f}로 확정 — 키워드 검색으로 넘어가지 않음.")
        res = ClassificationResult(candidates=judged.candidates, notes=judged.notes,
                                   escalate=False, escalate_reason=why)
        return PipelineOutput(retrieve=r, result=res, prior=prior,
                              went_keyword=False, trace=_classify.take_trace())

    # ── ⑦ LLM ③ 키워드 추출 ──
    kw = extract_keywords(book)

    # ── ⑧ 키워드 검색 + 병합 ──
    known = ([prior_cand] if prior_cand else []) + union_cands
    exclude = {c.ddc_h for c in known}
    kcands, rows, min_hit, m = retrieve.retrieve_keyword(kw.keywords, exclude, holdout)
    retrieve.merge_candidates(known, rows)
    r.keyword_candidates = kcands
    r.keyword_query = list(kw.keywords)
    r.keyword_hits_all = rows
    r.keyword_min_hit = min_hit
    r.messages += m

    # ── ⑨ 알라딘 C (키워드 후보에만 — 082·종합서지는 이미 받았다) ──
    retrieve.add_descriptions(kcands, book.title)

    # ── ⑩ LLM ④ 최종 판단 ──
    # SEEN_SHELF=off면 앞에서 이미 본 후보의 서가를 생략한다.
    # ⚠️ B에서 그 대상은 082 **와 종합서지 전부**다(⑥에서 이미 봤으므로).
    #    A는 082 하나뿐이라, 같은 스위치라도 **B의 절약이 훨씬 크다**(40권 vs 160권).
    #    그만큼 위험도 크다 — 최종 판단이 키워드 후보 30권만 보고 순위를 매기게 된다.
    hide2 = set() if SEEN_SHELF else exclude
    judged = classify(book, r, prior=prior, hide_shelf=hide2)

    # ── ⑪ 판정 ──
    esc, why = decide_escalate(judged.candidates)
    res = ClassificationResult(candidates=judged.candidates, notes=judged.notes,
                               escalate=esc, escalate_reason=why)
    return PipelineOutput(retrieve=r, result=res, prior=prior, keywords=kw,
                          went_keyword=True, trace=_classify.take_trace())
