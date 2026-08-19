"""판정 — **여기에 LLM 호출이 없다.**

LLM은 후보마다 `ShelfFitAssessment`(점수 + 근거)를 낼 뿐이고, 정렬·조기 종료·
자동확정·사서 이관은 전부 이 파일이 정한다. 그래야 저장된 회차 json의 점수만으로
문턱을 다시 쓸어볼 수 있다 — 재실행 0회, 비용 0원(2026-08-05).

**점수를 더하거나 가중합하지 않는다**(spec §7). 벡터를 정렬해 정책을 적용할 뿐이다.
"""

from __future__ import annotations

import os

from config import AUTO_CONFIRM_ENABLED, THRESHOLD_1, THRESHOLD_2
from schema import CandidateDecision, ShelfFitAssessment

# ── 게이트를 실제로 닫을 것인가 ───────────────────────────
# **이름이 하는 일을 그대로 말하게 둔다.** on이면 1단계에서 문턱을 넘은 순간 파이프라인이
# 멈추고 뒤 후보를 만들지도 않는다. off면 판정은 똑같이 계산해 기록하되 **멈추지 않는다**.
#
# 기본은 off다(2026-08-19). 문턱을 재산정하려면 12건 전부가 3단계까지 가서 082·종합서지·
# 키워드 후보 전량의 점수가 나와야 하는데, 게이트가 닫히면 그 케이스는 뒤 후보 점수가
# 아예 안 생겨 분포에 구멍이 난다. 문턱이 정해진 뒤 on으로 바꾼다.
EARLY_STOP = (
    AUTO_CONFIRM_ENABLED
    and os.environ.get("LIBCOPILOT_EARLY_STOP", "off").lower() == "on"
)


def rank(assessments: list[ShelfFitAssessment]) -> list[ShelfFitAssessment]:
    """점수 내림차순. 동점이면 번호 문자열로 — 회차마다 순서가 흔들리면 안 된다."""
    return sorted(assessments, key=lambda a: (-a.shelf_fit, a.h))


def gate_1(prior: ShelfFitAssessment | None) -> tuple[bool, str]:
    """1단계 082 게이트. 넘으면 **뒤 후보를 만들지도 않고** 확정이다.

    그래서 THRESHOLD_2보다 보수적으로 둔다. 척도가 달라서가 아니라(이제 같은 눈금이다)
    **뒤를 안 보고 끝내는 결정이라서** 그렇다.

    ⚠️ 반환값의 첫 항목은 "문턱을 넘었나"이지 "멈춘다"가 아니다. 실제로 멈출지는
       `EARLY_STOP`이 정한다. 넘었는지는 어느 쪽이든 기록된다.
    """
    if prior is None:
        return False, "082가 없거나 그 번호대 본교 장서가 0권이라 1단계를 건너뜁니다."
    if prior.shelf_fit >= THRESHOLD_1:
        return True, (f"082({prior.h}) 서가 적합도가 {prior.shelf_fit:.2f}로 "
                      f"1단계 문턱({THRESHOLD_1:.2f})을 넘습니다.")
    return False, (f"082({prior.h}) 서가 적합도가 {prior.shelf_fit:.2f}로 "
                   f"1단계 문턱({THRESHOLD_1:.2f})에 못 미칩니다 — 종합서지를 봅니다.")


def decide(assessments: list[ShelfFitAssessment]) -> CandidateDecision:
    """최종 판정 — 파일럿에서는 사서 검토, 자동확정은 명시적으로 켰을 때만.

    ⚠️ 1위와 2위의 격차(gap)는 정책에 쓰지 않는다. 예전에 gap 문턱을 함께 뒀는데
       24 케이스-회차 동안 단독으로 발동한 적이 한 번도 없었다(항상 점수가 먼저 걸렸다).
       한 번도 안 걸린 조건은 값을 정할 근거가 없다. 독립 fit에서는 격차가 비로소
       같은 눈금의 차이가 되어 의미를 갖지만, **먼저 분포를 관측한 뒤**에 정한다
       (spec §7). 그때까지는 기록용 관측값이다.
    """
    ranked = rank(assessments)
    if not ranked:
        return CandidateDecision(
            assessments=[], auto_confirm_eligible=False, escalate=True,
            escalate_reason="점수를 매긴 후보가 하나도 없습니다. "
                            "검색이 못 건졌거나 후보 번호대에 본교 장서가 없습니다.")
    top = ranked[0]
    eligible = top.shelf_fit >= THRESHOLD_2
    if not eligible:
        return CandidateDecision(
            assessments=ranked, auto_confirm_eligible=False, escalate=True,
            escalate_reason=(f"1순위 {top.h}의 서가 적합도가 {top.shelf_fit:.2f}로 "
                             f"문턱({THRESHOLD_2:.2f})에 못 미칩니다 — "
                             f"제시된 어느 번호대에도 뚜렷하게 어울리지 않습니다."))
    if not AUTO_CONFIRM_ENABLED:
        return CandidateDecision(
            assessments=ranked, auto_confirm_eligible=True, escalate=True,
            escalate_reason=(f"파일럿 운영: 1순위 {top.h}의 서가 적합도는 {top.shelf_fit:.2f}로 "
                             f"현재 참고 문턱({THRESHOLD_2:.2f})을 넘지만, 자동확정은 꺼져 있습니다. "
                             "후보와 근거를 보고 사서가 확정합니다."))
    return CandidateDecision(
        assessments=ranked, auto_confirm_eligible=True, escalate=False,
        escalate_reason=(f"1순위 {top.h}의 서가 적합도가 {top.shelf_fit:.2f}로 "
                         f"문턱({THRESHOLD_2:.2f})을 넘습니다. 자동확정합니다."))
