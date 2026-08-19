"""종합서지(KERIS 종합목록) 조회 창구 — 타대학 소장 청구기호.

**역할:** 신규 도서 제목으로 종합목록에서 "타대학이 이 책에 뭘 매겼나"를 찾아,
DDC 분류기호를 **voting(대학 수)** 으로 센다. 이 득표는 *분류 기준이 아니라*
"많이 쓰인 번호부터 본교에서 확인해보자"는 **순서 힌트**다(design.md 논의).

**불변식(sogang_db.py와 같은 결):**
  - 내부는 그냥 list[dict] + Counter (306행이라 pandas 불필요).
  - 밖으로는 dict/집계값만. 원자료(union_db.json)는 손대지 않는다.

**두 홀드아웃 중 하나가 여기:**
  - 서강대(=본교)는 voting에서 **항상 제외**한다. 자기 답을 종합서지에서 베끼면
    순환이다(design.md §8). config.HOME_LIBRARY_KEY로 판별.

**KDC 제외:** 서강은 DDC관. KDC는 DDC로 1:1 변환이 안 되므로 세지 않는다(AGENTS.md).
"""

from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache

from config import HOME_LIBRARY_KEY, UNION_PATH
from tokens import tokens as _tokens


@lru_cache(maxsize=1)
def _load() -> list[dict]:
    with open(UNION_PATH, encoding="utf-8") as f:
        return json.load(f)


def _match_book(query_title: str) -> str | None:
    """신규 도서 제목 → 종합목록의 책제목(정본) 하나. 토큰 최다 겹침으로 고른다.

    종합목록 책제목은 부제까지 붙어 있어(예: "가장 인간적인 도시 : 실리콘밸리…"),
    입력 제목 토큰이 가장 많이 겹치는 책을 고른다. 겹침 0이면 None.
    """
    q = _tokens(query_title)
    if not q:
        return None
    best, best_score = None, 0
    for bt in {h["book_title"] for h in _load()}:
        score = len(q & _tokens(bt))
        if score > best_score:
            best, best_score = bt, score
    return best


def voting(query_title: str) -> tuple[dict[str, int], dict]:
    """제목으로 종합목록 조회 → DDC 득표(서강 제외) + 메타.

    반환:
      votes: {ddc_h: 대학수}  내림차순 dict (DDC only, 서강 제외)
      meta:  {matched_title, ddc_libraries, kdc_libraries, home_excluded,
              rows:[표시용 소장행]}  — 트레이스/화면용
    """
    matched = _match_book(query_title)
    if matched is None:
        return {}, {"matched_title": None, "ddc_libraries": 0,
                    "kdc_libraries": 0, "home_excluded": 0, "rows": []}

    rows = [h for h in _load() if h["book_title"] == matched]
    home_excluded = sum(1 for h in rows if HOME_LIBRARY_KEY in h["university"])
    kept = [h for h in rows if HOME_LIBRARY_KEY not in h["university"]]

    ddc = Counter(h["class_no"] for h in kept
                  if h["scheme"] == "DDC" and h["class_no"])
    kdc_n = sum(1 for h in kept if h["scheme"] == "KDC")

    votes = dict(ddc.most_common())  # 득표순
    meta = {
        "matched_title": matched,
        "ddc_libraries": sum(votes.values()),
        "kdc_libraries": kdc_n,       # 참고만 (세지 않음)
        "home_excluded": home_excluded,
        "rows": [
            {"university": h["university"], "scheme": h["scheme"],
             "class_no": h["class_no"], "author_mark": h["author_mark"],
             "is_home": HOME_LIBRARY_KEY in h["university"]}
            for h in rows
        ],
    }
    return votes, meta
