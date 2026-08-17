"""LLM 판단 — Claude. **호출은 이 파일 안에서만 한다**(CLAUDE.md 규약 3).

호출이 둘이다. 실제 사서 업무 순서를 구조로 옮긴 것이다(교수님 지시, 2026-08-05):

  ① check_prior()  082(업체번호) 하나만 본교 서가에 대본다 → PriorCheck
                   082가 없으면 부르지 않는다.
  ② classify()     082 + 종합서지 후보를 한 자리에 놓고 점수를 매긴다 → LLMJudgement

**"사람에게 넘길지"는 이 파일이 정하지 않는다.** LLM은 점수만 내고, 판정은
`pipeline.decide_escalate()`가 `config.THRESHOLD_2`으로 한다(2026-08-05).

왜 나눴나: 한 번에 다 던지면 "082를 먼저 보고, 안 맞을 때 종합서지를 편다"는 업무 순서가
뭉개진다. 실제로 082가 그럴듯해 보이면 거기서 닫아버리는 사고가 났었다(케이스5).
나누면 1차는 082만 놓고 판단하고, 2차는 **1차 결론을 알고서** 경쟁자와 비교한다.

⚠️ 두 호출의 `shelf_fit`은 **척도가 다르다**. 1차는 경쟁자 없이 082 혼자 매긴 점수고,
   2차는 후보끼리 같은 잣대로 매긴 점수다. 그래서 문턱도 둘로 나뉘어 있고
   (`THRESHOLD_1` / `THRESHOLD_2`), **두 값을 같게 두면 안 된다.**
   1차 점수와 2차 점수를 한 줄에 놓고 크기를 비교하는 것도 의미가 없다.

핵심 원칙(docs/design.md):
  - '주제 적합'을 최우선으로 판단한다. shelf_books(그 번호대 책 레코드 전체)로 주제를 읽는다.
  - '개수'(union_votes / shelf_count)는 보조 신호일 뿐이다.
      개수 최다가 정답이 아닌 케이스가 있다(예: 부린왕자 179.9 최다이나 오답).
      ⚠️ `shelf_count`는 **전량이라 정확하다**(2026-08-05 DB 교체 후). 부정확한 건
         프롬프트에 실리는 **목록**(최대 40권)뿐이고, 머리에 `N건 전량`/`N건 중 40권`으로
         구분해 적는다.
  - 082는 prior. 주제와 맞으면 유지, 안 맞으면 기각하고 근거를 남긴다.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import anthropic

from config import (MAX_TOKENS, MODEL_KEYWORD, MODEL_MAIN, MODEL_PRIOR,
                    SEEN_SHELF, SHELF_DESC_CHARS)
from schema import (SHELF_FIT_RUBRIC, BookInput, KeywordExtraction,
                    LLMJudgement, PriorCheck, RetrieveResult)

_client = anthropic.Anthropic()
_openai = None          # 필요할 때만 만든다 (키가 없어도 Claude 경로는 돌아야 하므로)


class _Usage:
    """OpenAI usage를 Claude 필드명으로 맞춘 껍데기.

    `_ask()`의 trace 기록이 `input_tokens`/`output_tokens`/`cache_read_input_tokens`를
    읽는데, OpenAI는 `prompt_tokens`/`completion_tokens`/`prompt_tokens_details.cached_tokens`다.
    여기서 한 번 옮겨두면 회차 md·비용 집계가 양쪽 모델에서 똑같이 동작한다.

    `reasoning_tokens`는 Claude엔 없는 항목이라 따로 둔다 — **출력 토큰에 포함되어
    같은 단가로 과금된다**(gpt-5.6-luna 출력 $1.20/M). 답 문장이 200토큰인데
    생각이 250토큰이면 비용의 절반이 안 보이는 쪽에서 나간 것이다.
    """

    def __init__(self, u):
        self.input_tokens = getattr(u, "prompt_tokens", None)
        self.output_tokens = getattr(u, "completion_tokens", None)
        pd = getattr(u, "prompt_tokens_details", None)
        self.cache_read_input_tokens = getattr(pd, "cached_tokens", None)
        cd = getattr(u, "completion_tokens_details", None)
        self.reasoning_tokens = getattr(cd, "reasoning_tokens", None)


def _ask_openai(model: str, system: str, prompt: str, output_format):
    """OpenAI 경로 — 모델 비교 실험용. `MODEL_*`이 'gpt-'로 시작하면 이쪽으로 온다.

    ⚠️ 실험 전용이다. Claude 경로와 **프롬프트는 같지만 추론 방식이 다르다.**
       gpt-5.6 계열은 reasoning 모델이라 Claude의 adaptive thinking과 성격이 비슷하지만
       (호출 하나에 reasoning 250토큰이 찍힌다), 예산을 나눠 쓰는 방식이 다르다.
       구 gpt-4o는 아예 그 단계가 없었다. 점수를 나란히 놓고 비교할 때
       "모델 차이"에는 이 차이도 섞여 있다.
       (프롬프트 캐싱은 양쪽 다 된다. OpenAI는 1024토큰 넘으면 자동이다.)

    ⚠️ 429를 재시도로 견딘다. **한도는 모델마다 다르다** —
       구 gpt-4o는 계정 TPM이 30,000이라 한 케이스(#12 일리아스, 32,945토큰)가
       분당 한도를 통째로 넘겨 재시도로도 못 살렸다(2026-08-05에 실제로 죽었다).
       gpt-5.6-luna는 200,000이라 같은 케이스도 한도의 16%다(2026-08-12 확인).
    """
    global _openai
    if _openai is None:
        import openai
        _openai = openai.OpenAI()
    import openai as _oa

    for attempt in range(8):
        try:
            resp = _openai.beta.chat.completions.parse(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": prompt}],
                response_format=output_format,
            )
            return resp.choices[0].message.parsed, _Usage(resp.usage)
        except _oa.RateLimitError:
            if attempt == 7:
                raise
            wait = 20 * (attempt + 1)   # 20·40·60… TPM은 분 단위라 짧게 기다려봐야 소용없다
            print(f"      · TPM 한도 — {wait}초 대기 후 재시도 ({attempt + 1}/7)", flush=True)
            time.sleep(wait)

# ── 시스템 프롬프트는 파일에서 읽는다 (prompts/*.txt) ──────
# 왜 코드 밖으로 뺐나(2026-08-13): 팀원이 각자 프롬프트를 튜닝하는데, 코드 안에 있으면
#   · 실험하려면 classify.py를 고쳐야 하고
#   · 여러 벌을 나란히 두고 갈아끼울 수가 없다
# 파일로 두면 `.txt`를 복사해 고치고 환경변수로 지목하기만 하면 된다.
#
#   LIBCOPILOT_PROMPT_2=prompts/exp_min2.txt python evaluate.py --label=min2
#
# `{SHELF_FIT_RUBRIC}` 자리에 schema의 눈금이 끼워진다. **두 프롬프트가 같은 눈금을
# 쓰게 하려는 것**이고(1차·2차의 shelf_fit이 같은 뜻이어야 한다), 눈금을 바꾸려면
# schema.SHELF_FIT_RUBRIC 하나만 고친다. 자리표시자를 지우면 정의 없는 점수가 된다.
#
#   prompts/systemprompt_1st.txt  →  SYSTEMPROMPT_1ST   (082 하나만 보는 호출)
#   prompts/systemprompt_2nd.txt  →  SYSTEMPROMPT_2ND     (후보 비교·점수 호출)
#
# ⚠️ 파일명은 **ASCII로 둔다.** 변수명은 파이썬 안에만 있지만 파일명은 git·다른 OS·클론을
#    건너다닌다. 한글 파일명은 macOS(NFC)와 옛 HFS+/윈도우(NFD)에서 정규화가 갈려
#    "파일 없음"이 난다. 사람이 읽는 산출물(output/*.md)은 코드가 이름으로 안 찾으니 무관.
#
# 옛 버전은 따로 보관하지 않는다 — git이 이미 이력을 갖고 있고, 회차 md에는
# **프롬프트 전문이 통째로 찍히므로** 그 회차를 재현할 근거도 남는다.
# 실험본을 만들 때만 다른 이름으로 복사해 환경변수로 지목한다.
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# 어느 파일을 읽었나 — 회차 json의 「이 회차 조건」이 쓴다.
# 실험할 땐 다른 파일을 지목할 수 있어서(아래), 나중에 "이 회차는 뭘로 돌렸지"를 알려면 남겨야 한다.
PROMPT_USED: dict[str, str] = {}


def read_prompt(name: str) -> str:
    """`prompts/<name>.txt` 를 읽어 문자열로.

    · `{SHELF_FIT_RUBRIC}` 자리에 schema.py의 점수 눈금이 끼워진다
      (프롬프트와 출력 스키마가 **같은 눈금**을 쓰게 하려는 것이다. 지우지 말 것)
    · 실험본은 환경변수로 지목한다:
        LIBCOPILOT_PROMPT_SYSTEMPROMPT_2ND=prompts/exp_min2.txt python evaluate.py --label=min2
    """
    override = os.environ.get(f"LIBCOPILOT_PROMPT_{name.upper()}")
    path = Path(override) if override else PROMPTS / f"{name}.txt"
    PROMPT_USED[name] = str(path)
    return path.read_text(encoding="utf-8").replace("{SHELF_FIT_RUBRIC}", SHELF_FIT_RUBRIC)


SYSTEM_1ST     = read_prompt("systemprompt_1st")      # 082 정합성
SYSTEM_KEYWORD = read_prompt("systemprompt_keyword")  # 검색어 만들기
SYSTEM_2ND     = read_prompt("systemprompt_2nd")      # 최종 판단


# 상세정보 중 프롬프트에서 뺄 항목 — **분류기호는 절대 보여주지 않는다.**
# 서가 책의 청구기호를 보여주면 LLM이 주제를 읽는 대신 그 번호를 그대로 베낀다.
_SKIP_DETAIL = {"분류기호", "청구기호", "KDC", "로컬분류"}


def _format_book(b) -> str:
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


_SOURCE_ORDER = ("082", "종합서지", "키워드검색")


def _format_number(c) -> str:
    """후보 번호 하나 = 머리 한 줄 + 그 번호대 본교 책 목록.

    ⚠️ 머리에서 **개수와 목록을 구분해 적는다**(2026-08-13). 예전에는 둘 다
       `N건(샘플, 부정확)`이라고 붙였는데, 구 DB(`sogang_db.json` 1,682건, 번호당 50건
       상한) 시절 문구가 그대로 남은 것이었다. 지금은 전량 크롤이라 **개수는 정확하고
       목록만 표본**이다 — LLM에게 "이 숫자 믿지 마라"고 잘못 말하고 있었다.
         306.446   전량 37건 · 목록 37권   → 40권 상한에 안 걸려 전량이 다 들어간다
         720.2     전량 192건 · 목록 40권  → 이쪽이 진짜 표본

    **출처 라벨**(2026-08-18): 같은 번호가 여러 채널에서 나올 수 있어서, 후보는 하나로
    합치고 어디서 왔는지만 라벨로 붙인다. 증거가 겹쳤다는 게 그 자체로 신호다.

    **주제 요약**은 키워드 후보에만 붙는다 — 10권만 보여주므로 못 보여준 서가를
    그 한 줄이 대신한다. 082·종합서지는 40권이라 중복이라 안 붙인다.
    """
    n, m = c.shelf_count, len(c.shelf_books)
    where = (f"본교 서가 {n}건 전량" if n == m else f"본교 서가 {n}건 중 {m}권")

    src = " · ".join(s for s in _SOURCE_ORDER if s in (c.sources or set())) or "082"
    bits = [f"출처: {src}", where]
    if c.subject_top:
        bits.append("주제: " + " · ".join(f"{t}({k})" for t, k in c.subject_top))
    head = f"■ {c.ddc_h}  [{' | '.join(bits)}]"

    if not c.shelf_books:
        if n > 0:      # 서가를 일부러 생략한 경우 (config.SEEN_SHELF=off)
            return f"{head}\n  (앞 단계에서 이미 이 서가를 보고 판단했습니다 — 목록 생략)"
        return f"{head}\n  (본교 미보유 — 서가 의미를 읽을 수 없음)"
    body = "\n".join(_format_book(b) for b in c.shelf_books)
    return f"{head}\n{body}"


def _hide(c, hide: set[str]):
    """이미 앞 호출에서 판단한 후보의 **서가 목록만** 지운 사본.

    `config.SEEN_SHELF=off`일 때 쓴다(교수님 제안, 2026-08-17). 머리줄·개수·출처는 남고
    책 목록만 빠져서, 프롬프트에 "앞 단계에서 이미 봤습니다"로 찍힌다.
    원본은 안 건드린다 — 회차 json에는 서가가 있었다는 사실이 그대로 남아야 한다.
    """
    return c.model_copy(update={"shelf_books": []}) if c.ddc_h in hide else c


def _format_candidates(r: RetrieveResult, hide: set[str] | None = None) -> str:
    """업무 순서 그대로 블록을 나눠 보여준다(design.md §2.1.1).

    `hide`: 앞 호출에서 이미 서가를 보고 판단한 번호들. 서가 목록을 생략한다.
      A는 082 하나, B는 082 + 종합서지가 대상이라 **버전마다 다르다** — 그래서
      pipeline이 정해서 넘긴다.
    """
    hide = hide or set()
    blocks = []

    if r.prior_candidate is not None:
        blocks.append(
            "[1단계 — 082(업체번호) 정합성 확인]\n"
            f"{_format_number(_hide(r.prior_candidate, hide))}\n"
            "→ 위 서가에 꽂힌 책들이 이 도서의 주제와 맞습니까? 잠정 판단만 하고,\n"
            "   **결론은 뒤 단계까지 본 뒤에** 내리세요."
        )
    else:
        blocks.append("[1단계 — 082 없음] 업체번호가 없으므로 2단계부터 판단합니다.")

    if r.union_candidates:
        excl = "  ※082 번호는 1단계에서 이미 판단했으므로 이 득표에서 제외돼 있습니다."
        blocks.append(
            "[2단계] 종합서지(타대학) 득표 후보 — **1단계 결론과 무관하게 반드시 확인**\n"
            f"{excl}\n" +
            "\n".join(_format_number(_hide(c, hide)) for c in r.union_candidates)
        )
    else:
        blocks.append("[2단계] 종합서지 득표 후보 없음.")

    # ── 3단계: 키워드 검색 후보 ──
    # 근거의 성격이 위와 다르다는 걸 LLM에게 명시한다.
    #   종합서지 = 타대학이 **이 책 자체**에 매긴 번호 (직접 증거)
    #   키워드   = 이 주제어가 걸린 자리          (간접 증거)
    # 그래서 서가도 10권만 주고, 대신 그 번호대 전체의 주제명 요약을 머리줄에 붙인다.
    if r.keyword_candidates:
        blocks.append(
            "[3단계] 키워드 검색 후보 — 본교 장서에서 같은 주제어가 걸린 자리\n"
            f"  ※검색어: {', '.join(r.keyword_query)}\n"
            "  ※이건 타대학이 이 책에 매긴 번호가 아니라 **비슷한 주제 책이 꽂힌 자리**입니다.\n"
            "    종합서지보다 약한 근거지만, 종합서지에 없던 자리를 보여줍니다.\n"
            "    서가는 10권만 보여드리므로 머리줄의 `주제:`로 나머지 성격을 가늠하세요.\n" +
            "\n".join(_format_number(c) for c in r.keyword_candidates)
        )
    elif r.keyword_query:
        blocks.append(
            f"[3단계] 키워드 검색({', '.join(r.keyword_query)}) — 새로 나온 후보 없음.")

    return "\n\n".join(blocks)


def _book_block(book: BookInput) -> str:
    return f"""[분류 대상 도서]
- 제목: {book.title}
- 부제: {book.subtitle or "-"}
- 저자: {book.author or "미상"}
- 082(업체 DDC): {book.ddc_082 or "없음"}
- 번역서: {"예" if book.is_translation else "아니오"}
- 키워드: {", ".join(book.keywords) or "-"}
- 목차/책소개: {", ".join(book.toc) or (book.description or "-")}"""


# ── 원물 기록 ────────────────────────────────────────────
# 팀원이 프롬프트를 튜닝하려면 **무엇을 넣었더니 무엇이 나왔는지**를 봐야 한다.
# 그런데 지금까지 회차 파일에 남는 건 파싱된 결과뿐이었다 — 프롬프트가 어디에도 없었다.
# 케이스당 2차 사용자프롬프트가 35,000자(전체의 95%)이고 거기에 서가 40권이 들어 있는데,
# 그게 안 보이면 "왜 이 번호를 골랐지"에 답할 수가 없다.
#
# 한 건 처리할 때마다 여기 쌓고, pipeline이 `take_trace()`로 가져가 비운다.
# ⚠️ 모듈 전역이라 **한 번에 한 건**을 전제한다(evaluate·app 둘 다 그렇다).
_TRACE: list[dict] = []


def take_trace() -> list[dict]:
    """쌓인 원물을 가져가고 비운다. pipeline이 한 건 끝날 때마다 부른다."""
    out, _TRACE[:] = list(_TRACE), []
    return out


def _ask_claude(model: str, system: str, prompt: str, output_format):
    """Claude 호출 + 재시도.

    ⚠️ **400(Invalid request data)도 재시도한다.** 보통 400은 요청이 잘못됐다는 뜻이라
       다시 보내봐야 소용없지만, 여기선 **같은 입력이 다른 회차에서는 통과했다**
       (2026-08-05: 12건 회차 셋 중 둘이 각각 #2, #11에서 죽었는데 나머지 회차는
       같은 케이스를 멀쩡히 지났다). 즉 요청이 아니라 그때그때의 일시적 실패다.
       12건 배치가 10건째에서 죽으면 회차 전체가 날아가고 .json도 안 남는다.

    ⚠️ **단, 사용 한도 소진(400)은 재시도하지 않는다**(2026-08-12). 이건 일시적 실패가
       아니라 다음 달까지 확정된 상태다. 재시도하면 30초를 버리고 똑같이 죽는다.
       실제로 frozen_c 회차가 여기서 날아갔다. 콘솔에서 한도를 올려야 풀린다.
    """
    last = None
    for attempt in range(4):
        try:
            resp = _client.messages.parse(
                model=model,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
                output_format=output_format,
            )
            return resp.parsed_output, resp.usage
        except (anthropic.BadRequestError, anthropic.APIStatusError) as e:
            last = e
            if "usage limit" in str(e).lower():
                raise RuntimeError(
                    "Anthropic API 사용 한도가 소진됐습니다. 재시도해도 풀리지 않습니다.\n"
                    "  · console.anthropic.com → Settings → Limits 에서 한도를 올리거나\n"
                    "  · 한도가 복구되는 날짜까지 기다려야 합니다 (오류 메시지에 적혀 있습니다).\n"
                    "  · 급하면 LIBCOPILOT_MODEL/LIBCOPILOT_MODEL_PRIOR로 다른 제공자를 쓰세요.\n"
                    f"원문: {e}") from e
            if attempt == 3:
                raise
            print(f"      · {type(e).__name__} — {5 * (attempt + 1)}초 후 재시도 "
                  f"({attempt + 1}/3)", flush=True)
            time.sleep(5 * (attempt + 1))
    raise last


def _ask(model: str, system: str, prompt: str, output_format, step: str = ""):
    t0 = time.time()
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        parsed, usage = _ask_openai(model, system, prompt, output_format)
    else:
        parsed, usage = _ask_claude(model, system, prompt, output_format)
    _TRACE.append({
        "step": step, "model": model, "system": system, "prompt": prompt,
        "output": parsed.model_dump() if parsed is not None else None,
        "sec": round(time.time() - t0, 1),
        "in_tokens": getattr(usage, "input_tokens", None),
        "out_tokens": getattr(usage, "output_tokens", None),
        # 캐시가 실제로 먹었는지 — 시스템프롬프트가 1024토큰 미만이면 안 먹는다
        "cache_read": getattr(usage, "cache_read_input_tokens", None),
        # OpenAI reasoning 모델만 채워진다. out_tokens에 이미 포함된 값이다(중복 합산 금지).
        "reasoning_tokens": getattr(usage, "reasoning_tokens", None),
    })
    return parsed


# ── ① 1차 호출: 082 정합성만 ──────────────────────────────
def check_prior(book: BookInput, r: RetrieveResult) -> PriorCheck | None:
    """082(업체번호) 하나만 본교 서가에 대본다. 082가 없으면 호출하지 않는다(None).

    **다른 번호는 보여주지 않는다.** 사서가 082를 먼저 서가에 대보는 그 단계 그대로다.

    ⚠️ **그 번호대 본교 책이 0권이면 묻지 않는다**(2026-08-05). 이 호출이 하는 일은
       "서가 책들과 어울리는가"인데, 볼 책이 없으면 0.00 말고 나올 답이 없다.
       케이스6(082=363.19262, 본교 0권)에서 실제로 그 0.00을 받으려고 Opus를 한 번
       태웠다. 코드가 이미 아는 것을 LLM에게 묻지 않는다(CLAUDE.md 규약 5).
       → None을 돌려주면 2차는 082를 후보로는 그대로 받되 1차 판정 블록만 빠진다.
    """
    if r.prior_candidate is None or r.prior_candidate.shelf_count == 0:
        return None
    prompt = f"""{_book_block(book)}

[업체가 준 082 = {r.prior_candidate.ddc_h} — 이 번호대의 본교 서가]
{_format_number(r.prior_candidate)}

위 서가에 꽂힌 책들을 읽고, 이 신규 도서가 그 자리에 들어가도 자연스러운지 판단하세요.
`shelf_fit`(0.0~1.0)과 그 근거(`verdict`)만 내세요. 다른 번호는 제안하지 마세요."""
    return _ask(MODEL_PRIOR, SYSTEM_1ST, prompt, PriorCheck, step="1차 082 정합성")


# ── ①-b 키워드 추출: 본교를 뒤질 검색어를 만든다 ──────────
def extract_keywords(book: BookInput) -> KeywordExtraction:
    """책 정보만 보고 검색어 3~5개를 만든다(한국어 + 영어).

    ⚠️ **082 서가를 보여주지 않는다.** 이게 이 호출을 따로 두는 이유다.
       1차 호출은 082 서가 40권을 보고 있어서, 거기에 키워드 추출을 얹으면 그 어휘로
       물든다 — `#7 게임으로 철학하기`는 082=102(철학) 서가 649권 중 40권을 보고 있어서
       '철학' 쪽 검색어가 나오고, 그러면 검색도 193(1435권)·100(797권)으로 끌려간다.
       그런데 **082가 오답인 케이스야말로 키워드 검색이 필요한 자리**다. 목적과 반대로 돈다.

    ⚠️ 게이트 뒤에서 부른다(pipeline). 082로 확정되는 하 케이스에서는 아예 안 돈다.
       입력이 책 정보뿐이라 약 1,000토큰으로 작다.
    """
    prompt = f"""{_book_block(book)}

이 책과 주제가 비슷한 책이 본교 어디에 꽂혀 있는지 찾아낼 검색어 3~5개를 만드세요.
한국어와 영어(LCSH 표목 형식)를 섞으세요. 형식어·장르어는 넣지 마세요."""
    return _ask(MODEL_KEYWORD, SYSTEM_KEYWORD, prompt, KeywordExtraction, step="키워드 추출")


# ── ② 2차 호출: 082 + 종합서지 후보를 놓고 점수 ────────────
def classify(book: BookInput, r: RetrieveResult,
             prior: PriorCheck | None = None,
             human_notes: str | None = None,
             hide_shelf: set[str] | None = None) -> LLMJudgement:
    """도서 + 1차 후보 → ▼h 후보 목록(shelf_fit·근거).

    ⚠️ **넘길지 말지는 여기서 정하지 않는다.** LLM은 점수만 내고,
       `pipeline.decide_escalate()`가 `config.THRESHOLD_2`으로 판정한다(2026-08-05).

    prior: 1차 호출 결과. 있으면 "앞 단계에서 이렇게 봤다"로 프롬프트에 붙는다.
      **결론을 강요하지는 않는다** — 1차는 경쟁자 없이 혼자 본 판정이라, 2차에서 뒤집힐 수 있다
      (케이스10에서 실제로 그랬다: 1차는 306.446을 기각, 2차는 경합 후보로 남김).
    human_notes: 사서가 앞 단계에서 내린 판정(데모 경로 전용, 선택).
    """
    blocks = [_book_block(book),
              f"[1차 검색이 좁힌 후보 청구기호]\n{_format_candidates(r, hide_shelf)}"]
    if prior is not None:
        blocks.append(
            f"[앞 단계에서 082({r.prior_candidate.ddc_h})만 놓고 본 판정]\n"
            f"- 서가 적합도: {prior.shelf_fit:.2f}\n"
            f"- 근거: {prior.verdict}\n"
            "→ 출발점으로 삼되 그대로 따르지 마세요. 경쟁자를 못 본 상태의 판정입니다."
        )
    if human_notes:
        blocks.append(f"[사서가 앞 단계에서 이미 판정한 것]\n{human_notes}")
    blocks.append(
        "**1단계부터 순서대로** 판단하세요. 각 후보 번호대에 꽂힌 책들(제목 + 상세정보의 "
        "일반주제명·지명주제명·요약 등)로 그 번호대가 무슨 주제인지 파악하고, 입력 도서와 "
        "가장 맞는 ▼h 후보와 `shelf_fit`, 판단 근거를 제시하세요. "
        "개수보다 주제 적합을 우선하세요."
    )
    return _ask(MODEL_MAIN, SYSTEM_2ND, "\n\n".join(blocks), LLMJudgement, step="최종 후보 비교·점수")
