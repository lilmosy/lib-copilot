"""후보 모으기 — LLM 없이 검색만 한다. 세 채널.

**본교 조회가 언제나 마지막 근거다.** 종합서지도 키워드도 "어느 번호를 볼지"만 정해주고,
서가의 의미는 항상 본교(`sogang_db`)에서 읽는다.

  ① 082(업체번호)     `retrieve_prior()`    — 본교에서 그 번호대 의미 확인
  ② 종합서지 득표      `retrieve_union()`    — 타대학이 **이 책**에 매긴 번호 (직접 증거)
                                              082 번호는 제외(1단계에서 이미 판단 + 승계 가능성)
  ③ 키워드 검색       `retrieve_keyword()`  — 이 주제어가 걸린 자리 (간접 증거)
                                              ①②에 이미 있는 번호는 제외

세 채널이 같은 번호를 낼 수 있다. 그때 후보를 새로 만들지 않고 `sources`에 라벨만 더한다
(`merge_candidates`) — 같은 서가 40권을 프롬프트에 두 번 찍는 낭비를 막고,
"증거가 겹쳤다"는 사실은 라벨로 남는다.

**계약 동일:** 어느 채널이든 같은 `CandidateNumber`를 만든다. classify/pipeline은 불변.
"""

from __future__ import annotations

import aladin
import sogang_db
import union_db
from config import (KEYWORD_FALLBACK_BELOW, KEYWORD_MIN_HIT, KEYWORD_SHELF,
                    MAX_KEYWORD_CANDIDATES, MAX_UNION_CANDIDATES,
                    SHELF_DESC_TOP, SHELF_SAMPLE, SUBJECT_TOP)
from schema import BookInput, CandidateNumber


def _make(ddc: str, *, sources: set[str], votes: int = 0, hits: int = 0,
          limit: int = SHELF_SAMPLE, subject: bool = False,
          holdout: tuple[str, str] | None = None) -> CandidateNumber:
    return CandidateNumber(
        ddc_h=ddc,
        is_082_prior="082" in sources,
        sources=set(sources),
        union_votes=votes,
        keyword_hits=hits,
        shelf_count=sogang_db.shelf_count(ddc, holdout=holdout),
        shelf_books=sogang_db.shelf_books(ddc, limit=limit, holdout=holdout),
        subject_top=sogang_db.subject_top(ddc, SUBJECT_TOP) if subject else [],
    )


# ── ① 082 ────────────────────────────────────────────────
def retrieve_prior(book: BookInput,
                   holdout: tuple[str, str] | None = None
                   ) -> tuple[CandidateNumber | None, list[str]]:
    """082 후보 하나. 종합서지는 아직 보지 않는다.

    게이트(THRESHOLD_1)가 여기서 끝낼 수 있으므로, 종합서지·키워드 조회와 그 알라딘
    호출을 뒤로 미루는 게 이 분리의 목적이다 — 하 케이스에서 통째로 안 돈다.
    """
    prior = (book.ddc_082 or "").strip()
    if not prior:
        return None, ["082 없음 → 종합서지·키워드로 후보를 찾습니다."]

    c = _make(prior, sources={"082"}, holdout=holdout)
    where = "본교에 해당 번호대 있음" if c.shelf_count else "본교에 해당 번호대 없음"
    return c, [f"082({prior}) 있음 → 본교에서 의미 확인 ({where})."]


# ── ② 종합서지 ────────────────────────────────────────────
def retrieve_union(book: BookInput, prior_h: str = "",
                   holdout: tuple[str, str] | None = None
                   ) -> tuple[list[CandidateNumber], list[str]]:
    """타대학 득표 상위 N. 082 번호와 본교 0권 번호는 뺀다."""
    votes, meta = union_db.voting(book.title)
    msgs: list[str] = []

    if not meta["matched_title"]:
        return [], ["종합서지에서 이 책을 못 찾음 → 082/키워드만으로 판단."]

    cands = [_make(ddc, sources={"종합서지"}, votes=votes[ddc], holdout=holdout)
             for ddc in votes if ddc != prior_h]

    # ⚠️ **본교에 0권인 번호는 뺀다**(2026-08-12). 서가를 못 읽는 번호는 LLM에게 판단할
    #    재료가 없다 — 프롬프트에 "(본교 미보유)" 한 줄만 들어가고 질문 자체가 성립 안 한다.
    #    12건 실측: 5개 케이스에 6개가 있었고 하나도 정답이 아니었다.
    dropped = [c.ddc_h for c in cands if c.shelf_count == 0]
    cands = [c for c in cands if c.shelf_count > 0]

    # ── 득표 상위 N위까지. **동점은 다 넣는다** ──
    # 딱 N개로 자르면 동점끼리 순서가 흔들릴 때 정답이 밀린다
    # (#11 부린왕자: 179.9(3)·811.36(1)·332.6324(1) — 정답이 3위인데 뒤 둘이 동점).
    cands.sort(key=lambda c: c.union_votes, reverse=True)
    before = {c.ddc_h for c in cands}
    if len(cands) > MAX_UNION_CANDIDATES:
        cut = cands[MAX_UNION_CANDIDATES - 1].union_votes
        cands = [c for c in cands if c.union_votes >= cut]
    rank_dropped = sorted(before - {c.ddc_h for c in cands})

    dist = ", ".join(f"{c.ddc_h}({c.union_votes})" for c in cands[:6]) or "(DDC 득표 없음)"
    msgs.append(
        f"종합서지 매칭: 「{meta['matched_title'][:30]}」 "
        f"· DDC {meta['ddc_libraries']}개 대학(서강 {meta['home_excluded']}개 제외) "
        f"· KDC {meta['kdc_libraries']}개(미집계).")
    excl = f" ※082({prior_h})는 1단계에서 판단하므로 득표에서 제외." if prior_h else ""
    msgs.append(f"DDC voting(순서 힌트): {dist}{excl}")
    if dropped:
        msgs.append(f"후보에서 뺀 번호: {', '.join(dropped)} — 본교에 0권이라 서가 의미를 "
                    f"읽을 수 없습니다.")
    if rank_dropped:
        msgs.append(f"득표 {MAX_UNION_CANDIDATES}위 밖이라 뺀 번호: {', '.join(rank_dropped)} "
                    f"(동점은 함께 남깁니다).")
    return cands, msgs


# ── ③ 키워드 ──────────────────────────────────────────────
def retrieve_keyword(kws: list[str], exclude: set[str],
                     holdout: tuple[str, str] | None = None
                     ) -> tuple[list[CandidateNumber], list[tuple[str, int]], int, list[str]]:
    """검색어 → 후보 번호대. `(후보, 전량결과, 실제문턱, 메시지)`.

    **전량 결과를 함께 돌려준다.** 자른 뒤만 남기면 두 가지를 잃는다:
      ① "몇 위까지 잘라야 사서가 본 책이 들어오나"를 재실행 없이 못 쓸어본다
      ② 꼬리(1~2권짜리 외딴 번호)가 재분류 큐의 씨앗인데 그게 사라진다 (design.md §11.1)

    문턱(`KEYWORD_MIN_HIT`)으로 조여 검색하고, 후보가 너무 적으면 1개 이상으로 완화한다.
    조어 제목이나 본교에 얇은 주제에서 0개가 될 수 있어서다.
    """
    msgs: list[str] = []
    min_hit = KEYWORD_MIN_HIT
    rows = sogang_db.search_by_keywords(kws, min_hit=min_hit, holdout=holdout)

    fresh = [(h, n) for h, n in rows if h not in exclude]
    if len(fresh) < KEYWORD_FALLBACK_BELOW and min_hit > 1:
        min_hit = 1
        rows = sogang_db.search_by_keywords(kws, min_hit=1, holdout=holdout)
        fresh = [(h, n) for h, n in rows if h not in exclude]
        msgs.append(f"키워드 {KEYWORD_MIN_HIT}개 이상으로는 후보가 부족해 1개 이상으로 완화.")

    picked = fresh[:MAX_KEYWORD_CANDIDATES]
    cands = [_make(h, sources={"키워드검색"}, hits=n, limit=KEYWORD_SHELF,
                   subject=True, holdout=holdout)
             for h, n in picked]

    msgs.insert(0, f"키워드 검색({', '.join(kws)}) · {min_hit}개 이상 매칭 "
                   f"· {len(rows)}개 번호대 중 {len(picked)}개 채택.")
    if picked:
        msgs.append("키워드 후보: " + ", ".join(f"{h}({n}권)" for h, n in picked))
    dup = [h for h, _ in rows[:MAX_KEYWORD_CANDIDATES + 3] if h in exclude]
    if dup:
        msgs.append(f"이미 후보인 번호라 새로 만들지 않음(출처 라벨만 추가): {', '.join(dup)}")
    return cands, rows, min_hit, msgs


# ── 병합 ──────────────────────────────────────────────────
def merge_candidates(base: list[CandidateNumber],
                     rows: list[tuple[str, int]]) -> None:
    """키워드 검색 결과 중 **이미 후보인 번호**의 출처 라벨을 갱신한다(제자리 수정).

    후보를 새로 만들지 않는다 — 같은 서가를 두 번 찍으면 프롬프트만 커지고 LLM도 혼란스럽다.
    대신 "종합서지에도 있고 키워드에도 걸렸다"는 **증거 중첩**을 라벨과 hits로 남긴다.
    """
    by_h = {c.ddc_h: c for c in base}
    for h, n in rows:
        c = by_h.get(h)
        if c is not None:
            c.sources.add("키워드검색")
            c.keyword_hits = n


def add_descriptions(cands: list[CandidateNumber], for_title: str) -> int:
    """후보 서가 책 중 ISBN 있는 책에 알라딘 책소개를 붙인다.

    후보를 다 모은 **뒤에 한 번에** 부른다 — 같은 책이 두 후보에 걸쳐 있을 때 중복 호출을
    피하려는 것이다. 못 받는 책은 빈 채로 둔다(양서·ISBN 없는 책이 많다).
    """
    if SHELF_DESC_TOP <= 0:
        return 0
    targets = [b for c in cands for b in c.shelf_books[:SHELF_DESC_TOP] if b.isbn]
    if not targets:
        return 0
    got = aladin.describe_many(targets, for_title)
    n = 0
    for b in targets:
        b.description = got.get(b.isbn, "") or ""
        n += bool(b.description)
    return n
