"""프롬프트 작업창 — 고치고 바로 돌려보는 곳. **시행착오용이지 기록용이 아니다.**

    streamlit run app_for_experiment.py

`app.py`(사서·발표용)와 `evaluate.py`(12건 채점)와 다른 셋:
  · **아무것도 저장하지 않는다.** output/ 에 안 쌓인다 — 탐색 기록이 진짜 회차를 덮으면
    "무엇이 좋아졌나"를 못 센다. 괜찮다 싶으면 `.txt`에 반영하고 evaluate.py로 12건 돌린다.
  · **시스템 프롬프트를 화면에서 고친다.** 파일도 안 건드리므로 팀원끼리 충돌하지 않는다.
  · **직전 실행과 비교해서 보여준다.** 한 번 돌린 결과만 보면 좋아진 건지 알 수 없다.

⚠️ 한 건으로 결론내지 말 것. LLM이라 같은 조건에서도 점수가 회차마다 0.10씩 흔들린다.
   여기서 방향을 잡고, 판단은 `evaluate.py`로 12건 두 회차를 돌려서 한다.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import classify  # noqa: E402
from config import SCENARIO_DIR, THRESHOLD_1, THRESHOLD_2  # noqa: E402
from pipeline import classify_book  # noqa: E402
from schema import BookInput  # noqa: E402

st.set_page_config(page_title="lib_copilot — 프롬프트 작업창", page_icon="🔧", layout="wide")

MODELS = ["gpt-5.6-luna", "gpt-5.6-terra", "claude-opus-4-8", "claude-sonnet-5"]
PROMPT_1 = ROOT / "prompts" / "systemprompt_1st.txt"
PROMPT_2 = ROOT / "prompts" / "systemprompt_2nd.txt"


def _run(case_path: Path, model: str, sys1: str, sys2: str) -> dict:
    """프롬프트·모델을 갈아끼우고 한 건 돌린다.

    `classify` 모듈의 전역을 바꾼다 — 호출 시점에 읽히므로 이것으로 충분하고,
    파일을 안 건드리니 다른 사람 실행에 영향이 없다.
    """
    classify.SYSTEM_1ST = sys1
    classify.SYSTEM_2ND = sys2
    classify.MODEL_PRIOR = classify.MODEL_MAIN = model

    d = json.loads(case_path.read_text(encoding="utf-8"))
    raw = dict(d["input"])
    raw.pop("keywords", None)                 # evaluate.py와 같은 조건(수기 키워드 제거)
    book = BookInput(**raw)
    t0 = time.time()
    out = classify_book(book, holdout=(book.title, book.author or ""))
    exp = d.get("expected", {})
    return {"out": out, "sec": round(time.time() - t0, 1),
            "gold": exp.get("review_final") or exp.get("writer_final"),
            "writer": exp.get("writer_final"), "review": exp.get("review_final"),
            "model": model, "title": book.title, "ddc_082": book.ddc_082}


def _scores(r: dict) -> dict[str, float]:
    return {c.h: c.shelf_fit for c in r["out"].result.candidates}


# ══ 사이드바 — 무엇을 돌릴까 ══════════════════════════════
st.sidebar.title("🔧 프롬프트 작업창")
st.sidebar.caption("고치고 바로 돌려본다. **저장하지 않는다.**")
cases = sorted(SCENARIO_DIR.glob("*.json"))
case = st.sidebar.selectbox("케이스", cases, format_func=lambda p: p.stem)
model = st.sidebar.selectbox("모델", MODELS)
st.sidebar.divider()
st.sidebar.caption(f"임계값 THRESHOLD_1 `{THRESHOLD_1}` · THRESHOLD_2 `{THRESHOLD_2}`\n\n"
                   "임계값은 `config.py`에서만 바꿉니다 — 여기서 만지면 "
                   "회차 기록과 조건이 어긋납니다.")

left, right = st.columns([1, 1])

# ══ 왼쪽 — 무엇을 넣나 ═══════════════════════════════════
with left:
    st.subheader("⬜ 넣는 것")
    st.caption("시스템 프롬프트는 여기서 고쳐도 **파일은 안 바뀝니다.** "
               "괜찮으면 `prompts/*.txt`에 직접 반영하세요.")
    sys1 = st.text_area("1차 — 082 하나만 서가에 대본다  `systemprompt_1st.txt`",
                        value=PROMPT_1.read_text(encoding="utf-8"), height=200, key="s1")
    sys2 = st.text_area("2차 — 후보를 비교하고 점수를 매긴다  `systemprompt_2nd.txt`",
                        value=PROMPT_2.read_text(encoding="utf-8"), height=320, key="s2")
    go = st.button("▶ 돌리기", type="primary", use_container_width=True)

if go:
    with st.spinner(f"{model} 로 「{case.stem}」 판단 중…"):
        try:
            cur = _run(case, model,
                       sys1.replace("{SHELF_FIT_RUBRIC}", classify.SHELF_FIT_RUBRIC),
                       sys2.replace("{SHELF_FIT_RUBRIC}", classify.SHELF_FIT_RUBRIC))
        except Exception as e:                # 죽어도 화면은 남게
            st.error(f"{type(e).__name__}: {e}")
            cur = None
    if cur:
        st.session_state["prev"] = st.session_state.get("cur")
        st.session_state["cur"] = cur

cur = st.session_state.get("cur")
prev = st.session_state.get("prev")

# ══ 오른쪽 — 무엇이 나왔나 ════════════════════════════════
with right:
    if not cur:
        st.info("왼쪽에서 케이스·모델을 고르고 **▶ 돌리기**를 누르세요.")
        st.stop()

    out, gold = cur["out"], cur["gold"]
    res, retr = out.result, out.retrieve
    top = res.candidates[0] if res.candidates else None

    ok = "✅" if top and top.h == gold else "❌"
    tag = "자동확정" if not res.escalate else "사서에게 넘김"
    st.subheader(f"{ok} {tag} — `{top.h if top else '없음'}`"
                 + ("" if top and top.h == gold else f"  (정답 `{gold}`)"))
    st.caption(f"{cur['model']} · {cur['sec']}초"
               + (f" · ⚠️ 작성자 {cur['writer']} → 교열 {cur['review']} (사람도 갈린 건)"
                  if cur["writer"] != cur["review"] else ""))
    st.markdown(f"> ⚙️ **코드가 정함** — {res.escalate_reason}")

    # ── 직전 실행과 비교 — 이게 이 화면의 핵심이다 ──
    if prev:
        st.markdown("##### 직전 실행과 비교")
        a, b = _scores(prev), _scores(cur)
        rows = []
        for h in list(b) + [x for x in a if x not in b]:
            old, new = a.get(h), b.get(h)
            arrow = ("—" if old is None else "빠짐" if new is None else
                     "↑" if new > old else "↓" if new < old else "=")
            rows.append({"▼h": h + (" ★정답" if h == gold else ""),
                         "직전": "—" if old is None else f"{old:.2f}",
                         "지금": "—" if new is None else f"{new:.2f}", "": arrow})
        st.dataframe(rows, hide_index=True, use_container_width=True)
        p_top = prev["out"].result.candidates[0].h if prev["out"].result.candidates else None
        if p_top != (top.h if top else None):
            st.warning(f"**1순위가 바뀌었습니다** — `{p_top}` → `{top.h if top else '없음'}`")
        st.caption(f"직전: {prev['model']} · 「{prev['title']}」")

    # ── LLM이 낸 것 — **원물 그대로 편다** ──
    # ⚠️ `st.dataframe`을 쓰지 않는다. 칸 안의 긴 글(reasoning은 300자 안팎)을 잘라서
    #    보여주기 때문에, 정작 "왜 그 점수인지"가 안 보인다. 이 화면의 목적이 그건데.
    st.markdown("##### 🟩 LLM 출력 — 손대지 않은 그대로")

    if out.prior:
        gate = (f" → {THRESHOLD_1} 이상이라 **종합서지를 안 봤습니다**"
                if out.prior.shelf_fit >= THRESHOLD_1 else "")
        st.markdown(f"**1차 · 082({cur['ddc_082']})만 봄** — `shelf_fit` "
                    f"**{out.prior.shelf_fit}**{gate}")
        st.markdown(f"> `verdict` — {out.prior.verdict}")
    elif not out.inherited:
        st.caption("1차 호출 없음 — 082가 없거나 그 번호대 본교 책이 0권입니다.")

    st.markdown("**2차 · 후보 비교**")
    for i, c in enumerate(res.candidates, 1):
        mark = " ★정답" if c.h == gold else (" (082)" if c.h == cur["ddc_082"] else "")
        st.markdown(f"**{i}위 · `{c.h}`{mark}** — `shelf_fit` **{c.shelf_fit}**")
        st.markdown(f"　　`label` — {c.label}")
        st.markdown(f"　　`reasoning` — {c.reasoning}")
    if res.notes:
        st.markdown(f"**`notes`** (전체 판단 + 버린 번호의 기각 사유)")
        st.markdown(f"> {res.notes}")

    with st.expander("🟩 출력 원본 (파싱된 그대로)"):
        st.json({"1차": out.prior.model_dump() if out.prior else None,
                 "2차": {"candidates": [c.model_dump() for c in res.candidates],
                         "notes": res.notes}})

# ══ 아래 — 무엇이 들어갔나 (원물) ═════════════════════════
st.divider()
st.markdown("##### ⬜ 들어간 데이터")
cands = ([retr.prior_candidate] if retr.prior_candidate else []) + retr.union_candidates
lines = ["번호          타대학  본교전체  프롬프트  그중 책소개"]
for c in cands:
    mark = "(082)" if c.is_082_prior else ("★정답" if c.ddc_h == gold else "")
    lines.append(f"{c.ddc_h:<12} {c.union_votes:>5}곳 {c.shelf_count:>8}권 {len(c.shelf_books):>7}권"
                 f" {sum(1 for b in c.shelf_books if b.description):>9}권  {mark}")
st.code("\n".join(lines), language=None)
for m in retr.messages:
    st.caption(f"· {m}")

for t in out.trace:
    with st.expander(f"⬜ {t['step']} — 시스템 프롬프트 ({len(t['system']):,}자)"):
        st.code(t["system"], language=None)
    with st.expander(f"⬜ {t['step']} — 사용자 프롬프트 ({len(t['prompt']):,}자) "
                     "· 서가 책 목록이 여기 다 들어 있습니다"):
        st.code(t["prompt"], language=None)
