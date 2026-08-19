"""LLM 호출 인프라 — **모든 API 호출은 이 파일을 통한다**(CLAUDE.md 규약 3).

여기는 "무엇을 묻는가"를 모른다. 모델을 고르고, 재시도하고, 원물을 기록할 뿐이다.
무엇을 묻는지는 `fit.py`(서가 적합도)와 `keyword.py`(검색어)가 정한다.

왜 갈랐나(2026-08-19): 예전 `classify.py` 하나가 호출 인프라·프롬프트 조립·세 종류
호출을 다 갖고 있어서, 프롬프트를 고치려 해도 재시도 로직을 지나가야 했다.
"""

from __future__ import annotations

import time

import anthropic

from config import MAX_TOKENS

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

def ask_openai(model: str, system: str, prompt: str, output_format):
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

# ── 원물 기록 ────────────────────────────────────────────
# 팀원이 프롬프트를 튜닝하려면 **무엇을 넣었더니 무엇이 나왔는지**를 봐야 한다.
# 케이스당 서가 40권이 프롬프트에 들어가는데 그게 안 보이면 "왜 이 점수를 줬지"에
# 답할 수가 없다. 한 건 처리할 때마다 여기 쌓고, pipeline이 `take_trace()`로 가져가 비운다.
# ⚠️ 모듈 전역이라 **한 번에 한 건**을 전제한다(evaluate·app 둘 다 그렇다).
# ⚠️ fit 호출은 단계 안에서 동시에 나간다 — append는 원자적이라 안전하지만
#    **순서는 완료순**이다. 어느 후보의 기록인지는 `step`에 번호가 찍혀 있다.
_TRACE: list[dict] = []


def take_trace() -> list[dict]:
    """쌓인 원물을 가져가고 비운다. pipeline이 한 건 끝날 때마다 부른다."""
    out, _TRACE[:] = list(_TRACE), []
    return out

def ask_claude(model: str, system: str, prompt: str, output_format):
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
                    "  · 급하면 LIBCOPILOT_MODEL_FIT/LIBCOPILOT_MODEL_KEYWORD로 다른 제공자를 쓰세요.\n"
                    f"원문: {e}") from e
            if attempt == 3:
                raise
            print(f"      · {type(e).__name__} — {5 * (attempt + 1)}초 후 재시도 "
                  f"({attempt + 1}/3)", flush=True)
            time.sleep(5 * (attempt + 1))
    raise last

def ask(model: str, system: str, prompt: str, output_format, step: str = ""):
    t0 = time.time()
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        parsed, usage = ask_openai(model, system, prompt, output_format)
    else:
        parsed, usage = ask_claude(model, system, prompt, output_format)
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
