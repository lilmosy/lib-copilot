"""프롬프트 조립 — 파일에서 지문을 읽고, 책·서가를 문자열로 찍는다.

여기는 LLM을 부르지 않는다. **무엇을 보여줄지**만 정한다.

⚠️ **독립 fit 계약**(spec §2): 서가 블록에는 그 번호대 자기 정보만 들어간다.
   다른 후보 번호·출처(082/종합서지/키워드)·종합서지 득표수·검색 건수·서가 간 순위·
   현재 몇 단계인지·문턱·과거 판례는 **넣지 않는다.** 넣는 순간 점수가 독립 적합도가
   아니라 후보 비교 점수가 되고, 그러면 문턱을 재산정할 수 없다.
"""

from __future__ import annotations

import os
from pathlib import Path

from config import SHELF_DESC_CHARS
from schema import SHELF_FIT_RUBRIC, BookInput, CandidateNumber

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# 어느 파일을 읽었나 — 회차 json의 「이 회차 조건」이 쓴다.
# 실험할 땐 다른 파일을 지목할 수 있어서(아래), 나중에 "이 회차는 뭘로 돌렸지"를 알려면 남겨야 한다.
PROMPT_USED: dict[str, str] = {}


def read_prompt(name: str) -> str:
    """`prompts/<name>.txt` 를 읽어 문자열로.

    · `{SHELF_FIT_RUBRIC}` 자리에 schema.py의 점수 눈금이 끼워진다
      (프롬프트와 출력 스키마가 **같은 눈금**을 쓰게 하려는 것이다. 지우지 말 것)
    · 실험본은 환경변수로 지목한다:
        LIBCOPILOT_PROMPT_SYSTEMPROMPT_SHELF_FIT=prompts/exp_fit.txt python evaluate.py --label=fit
    """
    override = os.environ.get(f"LIBCOPILOT_PROMPT_{name.upper()}")
    path = Path(override) if override else PROMPTS / f"{name}.txt"
    PROMPT_USED[name] = str(path)
    return path.read_text(encoding="utf-8").replace("{SHELF_FIT_RUBRIC}", SHELF_FIT_RUBRIC)

_SKIP_DETAIL = {"분류기호", "청구기호", "KDC", "로컬분류"}


def format_book(b) -> str:
    """본교 장서 한 권을 통째로(상세정보 포함) 한 줄에.

    제목만으로는 서가 의미가 흐리다. 상세정보의 일반주제명·총서명·원서명 등이
    '이 번호대가 무슨 주제인가'를 말해준다. 무엇을 볼지는 LLM이 고른다(여기선 안 거른다).

    `책소개`는 서가 책 중 **ISBN이 있는 전부**에 붙는다(retrieve._add_descriptions).
    주제명이 통제어휘라 정확한 대신 거칠다면, 책소개는 그 책이 실제로 무엇을 다루는지
    문장으로 말해준다 — 특히 **주제명이 아예 없는 최근 한국 신간**에서 유일한 단서다.
    ⚠️ 출판사 홍보문이라 과장이 섞인다. 그래서 라벨을 `책소개`로 명시해 구분한다.
    """
    head = f"「{b.title}」"
    meta = " / ".join(x for x in (b.author, b.publisher, str(b.pub_year or "")) if x)
    detail = " | ".join(f"{k}: {v}" for k, v in b.detail.items()
                        if v and k not in _SKIP_DETAIL)
    parts = [head]
    if meta:
        parts.append(meta)
    out = "  - " + " · ".join(parts)
    if detail:
        out += f"\n      {detail}"
    desc = (getattr(b, "description", "") or "").strip()
    if desc:
        out += f"\n      책소개: {desc[:SHELF_DESC_CHARS]}"
    return out


def format_shelf(c: CandidateNumber) -> str:
    """후보 번호 **하나**의 서가 블록 = 머리 한 줄 + 그 번호대 본교 책 목록.

    ⚠️ 머리줄에 **개수와 목록을 구분해 적는다**(2026-08-13). 개수는 전량이라 정확하고,
       목록만 표본이다.
         306.446   본교 서가 37건 전량      → 40권 상한에 안 걸려 다 들어간다
         720.2     본교 서가 192건 중 40권  → 이쪽이 진짜 표본

    ⚠️ 예전엔 여기에 `[출처: 종합서지 · 키워드검색]`과 종합서지 득표수가 찍혔다.
       독립 fit에서는 **뺀다** — 후보가 어디서 왔는지는 이 서가와 이 책의 적합도와
       아무 상관이 없고, "타대학 42곳이 이 번호를 줬다"는 사실을 같이 보여주면
       점수가 적합도가 아니라 인기투표가 된다(spec §2 「넣지 말 것」).
    """
    n, m = c.shelf_count, len(c.shelf_books)
    where = f"본교 서가 {n}건 전량" if n == m else f"본교 서가 {n}건 중 {m}권"
    head = f"[후보 서가 {c.ddc_h} — {where}]"

    if not c.shelf_books:
        # 본교 0권이면 서가 의미를 읽을 수 없다. 파이프라인이 이 후보에는 fit을 부르지
        # 않지만(코드가 아는 것을 LLM에게 묻지 않는다), 화면 경로에서 올 수 있어 남긴다.
        return f"{head}\n  (본교 미보유 — 서가 의미를 읽을 수 없음)"
    return head + "\n" + "\n".join(format_book(b) for b in c.shelf_books)

def book_content_block(book: BookInput) -> str:
    """LLM-K/LLM-F에 주는 신규 도서 내용.

    업체 082는 후보를 찾는 코드의 prior일 뿐, 책의 내용이 아니다. 이 블록에는 넣지 않는다.
    그래야 키워드 추출이 틀린 082에 앵커링되지 않고, shelf_fit도 `(책, 서가)`만의 점수로
    해석할 수 있다.
    """
    return f"""[분류 대상 도서]
- 제목: {book.title}
- 부제: {book.subtitle or "-"}
- 저자: {book.author or "미상"}
- 번역서: {"예" if book.is_translation else "아니오"}
- 원제: {book.original_title or "-"}
- 키워드: {", ".join(book.keywords) or "-"}
- 목차/책소개: {", ".join(book.toc) or (book.description or "-")}"""


# 구 이름을 외부 실험 스크립트가 잠깐 참조해도 깨지지 않게 남긴다.
# 새 호출부는 반드시 `book_content_block()`을 쓴다.
book_block = book_content_block
