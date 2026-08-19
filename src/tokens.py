"""제목 토큰 매칭 규칙 — **한 곳**.

두 곳이 이 파일 하나를 공유한다 — 둘 다 "이게 같은 책인가"를 판정하기 때문이다:
  `sogang_db`  홀드아웃(그 책 자신인가) · 승계 검색(기존 판본인가)
  `union_db`   종합서지에서 그 책 찾기

기준이 갈라지면 **홀드아웃이 뺀 책과 종합서지가 찾은 책이 달라진다.** 조용히 어긋나서
눈치채기 어려우므로 규칙은 여기 하나만 둔다 (design.md §8).

의도된 한계: 글자가 안 겹치면 못 잡는다.
  - "AI시대" ≠ "인공지능시대"  (동의어 불가)
  - "건축가가" ≠ "건축"         (한국어 조사가 붙어 다른 토큰이 됨)
→ 임베딩 검색(retrieve_embed, 예정)을 붙이면 비교해볼 지점.
"""

from __future__ import annotations

import re

# 조사·관사 등 의미 없는 토큰
STOP = {"의", "를", "을", "이", "가", "은", "는", "와", "과", "에",
        "for", "the", "of", "a"}


def tokens(text: str) -> set[str]:
    """문자열 → 토큰 집합. 한 글자와 STOP은 버린다."""
    return {
        t.lower()
        for t in re.findall(r"[0-9A-Za-z가-힣]+", text or "")
        if len(t) > 1 and t.lower() not in STOP
    }
