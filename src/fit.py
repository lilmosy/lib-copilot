"""독립 shelf_fit — **책 한 권 × 서가 한 곳**.

이 모듈의 계약은 한 문장이다(spec §1).

  `shelf_fit(book, shelf)`은 **다른 후보를 보지 않고**, 이 신규 책이 이 특정 번호대
  서가에 들어가도 자연스러운 정도를 0.0~1.0으로 평가한 값이다.

이 모듈은 후보를 찾지도, 후보끼리 순위를 매기지도, 사서에게 넘길지 정하지도 않는다.
정렬과 판정은 `decide.py`가, 후보 찾기는 `retrieve.py`가 한다.

왜 이렇게 바꿨나(2026-08-19): 예전에는 후보 5~8개를 한 프롬프트에 넣고 한꺼번에 점수를
매겼다. 그래서 같은 책·같은 번호라도 **옆에 누가 들어왔느냐로 점수가 달라졌고**,
문턱(THRESHOLD_2)이 회차마다 다른 뜻을 갖게 돼 재산정이 원리적으로 불가능했다.
게다가 082는 1차·최종에서 두 번 평가돼 같은 서가에 점수가 둘 났다(devlog 2026-08-18).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
import unicodedata

import llm
import prompt
from config import FIT_CONCURRENCY, MODEL_FIT
from schema import BookInput, CandidateNumber, ShelfFitAssessment

SYSTEM_FIT = prompt.read_prompt("systemprompt_shelf_fit")


def _title_key(title: str) -> str:
    """LLM 인용 제목과 DB 제목을 비교할 때 쓰는 느슨한 키.

    따옴표·공백·문장부호 차이만 흡수한다. 부분일치로 넓히면 서로 다른 판본/권을 잘못
    통과시킬 수 있으므로, 정규화 뒤 정확히 같은 제목만 인정한다.
    """
    normalized = unicodedata.normalize("NFKC", title or "").casefold()
    return re.sub(r"[^\w가-힣]", "", normalized)


def _validated_citations(out: ShelfFitAssessment, c: CandidateNumber) -> list[str]:
    """출력 인용을 현재 ShelfSnapshot의 실제 제목으로 대조·정규화한다.

    LLM 원문은 `llm.py` trace에 그대로 보존된다. 여기서는 화면과 최종 결과 JSON에
    남길 XAI 앵커만 검증한다. 일치하지 않는 인용은 조용히 버리고, 점수 자체는 버리지
    않는다 — 인용 오류가 적합도 재측정을 뜻하지는 않기 때문이다.
    """
    titles = {_title_key(book.title): book.title for book in c.shelf_books if _title_key(book.title)}
    valid: list[str] = []
    seen: set[str] = set()
    for cited in out.cited_books:
        actual = titles.get(_title_key(cited))
        if actual and actual not in seen:
            valid.append(actual)
            seen.add(actual)
    return valid


def assess(book: BookInput, c: CandidateNumber) -> ShelfFitAssessment | None:
    """후보 하나를 평가한다. LLM 1회.

    ⚠️ **본교 0권이면 부르지 않는다**(None). 이 호출이 하는 일은 "서가 책들과 어울리나"인데
       볼 책이 없으면 0.00 말고 나올 답이 없다. 코드가 이미 아는 것을 LLM에게 묻지 않는다
       (CLAUDE.md 규약 5). 케이스6(082=363.19262, 본교 0권)에서 실제로 그 0.00을 받으려고
       모델을 한 번 태운 적이 있다.

    ⚠️ `h`는 **코드가 찍는다.** 스키마에 필드가 있어 LLM도 채워 보내지만, 그대로 믿으면
       엉뚱한 번호가 들어올 수 있다. 어느 서가를 보여줬는지는 코드가 안다.
    """
    if c.shelf_count == 0 or not c.shelf_books:
        return None
    user = f"""{prompt.book_content_block(book)}

{prompt.format_shelf(c)}

위 서가에 꽂힌 책들을 읽고, 이 신규 도서가 그 자리에 들어가도 자연스러운지 판단하세요.
다른 번호는 제안하지 마세요."""
    out = llm.ask(MODEL_FIT, SYSTEM_FIT, user, ShelfFitAssessment,
                  step=f"fit {c.ddc_h}")
    if out is not None:
        out.h = c.ddc_h
        out.cited_books = _validated_citations(out, c)
    return out


def assess_many(book: BookInput, cands: list[CandidateNumber]) -> list[ShelfFitAssessment]:
    """한 단계에서 **새로 생긴 후보들**을 한꺼번에 평가한다.

    서로를 안 보는 호출이라 동시에 던져도 결과가 달라지지 않는다 — 지연만 준다.
    단계 사이는 순차다(앞 단계 결과로 다음 단계 진입을 정하므로).

    ⚠️ 앞 단계에서 이미 점수가 있는 후보는 **여기 넣지 않는다.** 다시 매기면 같은 서가에
       점수가 둘 생기고, 그게 바로 이 구조가 없애려던 문제다. 누구를 넘길지는
       pipeline이 정한다.
    """
    todo = [c for c in cands if c.shelf_count > 0 and c.shelf_books]
    if not todo:
        return []
    if len(todo) == 1 or FIT_CONCURRENCY <= 1:
        return [a for a in (assess(book, c) for c in todo) if a is not None]
    with ThreadPoolExecutor(max_workers=min(FIT_CONCURRENCY, len(todo))) as ex:
        out = list(ex.map(lambda c: assess(book, c), todo))
    return [a for a in out if a is not None]
