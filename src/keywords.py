"""키워드 추출 — 본교 장서를 뒤질 검색어를 만든다. LLM 1회.

082 서가가 오답인 케이스에서 후보를 만들 유일한 경로다. 종합서지는 실전에서 거의
비어 있다(union_db는 골든셋 12권의 득표만 갖고 있다).
"""

from __future__ import annotations

import llm
import prompt
from config import MODEL_KEYWORD
from schema import BookInput, KeywordExtraction

SYSTEM_KEYWORD = prompt.read_prompt("systemprompt_keyword")


def extract_keywords(book: BookInput) -> KeywordExtraction:
    """책 정보만 보고 검색어 3~5개를 만든다(한국어 + 영어).

    ⚠️ **서가를 보여주지 않는다.** 이게 이 호출을 따로 두는 이유다.
       082 서가 40권을 보여주면 그 어휘로 물든다 — `#7 게임으로 철학하기`는 082=102(철학)
       서가 649권 중 40권을 보고 있어서 '철학' 쪽 검색어가 나오고, 그러면 검색도
       193(1435권)·100(797권)으로 끌려간다. 그런데 **082가 오답인 케이스야말로 키워드
       검색이 필요한 자리**다. 목적과 반대로 돈다.

    ⚠️ 3단계에 들어갈 때만 부른다. 082·종합서지에서 끝나는 책에서는 아예 안 돈다.
       입력이 책 정보뿐이라 약 1,000토큰으로 작다.
    """
    user = f"""{prompt.book_content_block(book)}

이 책과 주제가 비슷한 책이 본교 어디에 꽂혀 있는지 찾아낼 검색어 3~5개를 만드세요.
한국어와 영어(LCSH 표목 형식)를 섞으세요. 형식어·장르어는 넣지 마세요."""
    return llm.ask(MODEL_KEYWORD, SYSTEM_KEYWORD, user, KeywordExtraction, step="키워드 추출")
