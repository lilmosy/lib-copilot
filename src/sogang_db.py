"""본교 장서(서강 로욜라도서관) 저장소 접근의 유일한 창구. (구 db.py)

**불변식(AGENTS.md):** 저장소 접근 방식은 이 파일 밖으로 나가지 않는다.
밖으로는 `schema.py`의 CollectionBook만. → 저장소를 갈아끼워도 부르는 쪽 불변.

담는 것 = 사서 업무 절차:
  0차: 원제/기존서지 승계 → find_inheritance()  (있으면 결정론적 승계, LLM 불필요)
  2차: 서가 의미           → shelf_books()       (후보 번호대에 뭐가 꽂혔나 — 레코드 통째로)

**저장소: `data/sogang_db_final.db` (SQLite 88만 건, 2026-08-05 교체)**
구 `sogang_db.json`(1,682건)은 도서관 **상세정보 화면**을, 이쪽은 같은 페이지의
**MARC 화면**을 긁은 것이다. 같은 원본이고 MARC 쪽이 상위집합이다.
전량 적재하지 않는다 — 인덱스(`ix_852h`/`ix_title`)로 필요한 레코드만 꺼낸다.

**▼h는 `marc_852h`를 쓴다**(구 `ddc_h` 컬럼 아님). `ddc_h`는 크롤러가 화면의
청구기호를 파싱한 값이라, 별치 자료(`보관서고`·`귀중자료`)에서는 **검색한 번호대로
주저앉는다**(예: 「Truman」 ddc_h=973 ↔ 실제 852▼h=973.918092). 23,343건이 그렇다.
대신 `marc_852h`가 빈 2.4%(정기간행물·참고도서 등 별치)는 조회에서 빠진다 —
개가 서가의 성격을 읽는 게 목적이므로 오히려 맞다.

**홀드아웃(design.md §8):** 평가 시 "분류 대상 책 자신"이 본교 DB에 이미 있으면
정답째로 누출된다. `holdout=(title, author)`를 넘기면 그 책을 조회에서 **동적으로 제외**한다.
DB는 손대지 않는다(원자료 보존). 데모/실전에서는 holdout=None.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from functools import lru_cache

from config import COLLECTION_DB, SHELF_SAMPLE, holdout_ids as _holdout_ids
from schema import BookInput, CollectionBook


# ── MARC → 상세정보(사람 라벨) ────────────────────────────
# 프롬프트에 실제로 들어가는 12종. 구 상세정보 화면의 라벨을 그대로 쓴다
# (prompt.py의 서가 블록·_SKIP_DETAIL이 이 이름을 보고 동작한다).
#
# ⚠️ 빠진 것과 이유 (2026-08-05 실측, 서가 40권 21,379자 기준):
#   245 서명/저자사항(18.1%)·100 개인저자(8.2%)·260 발행사항(8.6%)
#       → `title`/`author`/`publisher`/`pub_year`로 첫 줄에 이미 나간다. 40권이면 40번 중복.
#   020 ISBN(10.4%)·300 형태사항(5.8%) → 숫자·쪽수. 서가 의미와 무관.
#   504 서지주기(8.0%) → 거의 전부 "Includes bibliographical references and indexes." 정보량 0.
#   나머지 86종 → 레코드번호·수정일시·입력기관 등 시스템 관리용.
_MARC_LABEL = {
    "250": "판사항",
    "246": "원서명",
    "440": "총서명", "490": "총서명",
    "650": "일반주제명",
    "651": "지명주제명",
    "600": "주제명(개인명)",
    "610": "주제명(단체명)",
    "500": "일반주기",     # "본서는 ...의 번역서임"이 여기 들어온다
    "505": "내용주기",     # 목차
    "520": "요약",
    # ── 아래는 **답이 들어있다**. detail에는 담되 프롬프트에서 막는다.
    #    (prompt._SKIP_DETAIL이 라벨로 거른다. 사후 분석용으로 값 자체는 남긴다)
    "082": "분류기호",     # 업체 DDC
    "852": "청구기호",     # ▼h 그 자체
    "056": "KDC",
    "092": "로컬분류",
}

# 서브필드 중 사람이 읽을 내용이 아닌 것 — 전거번호(w)·분류표판(2)·연결(6,8) 등
_NOISE_SUBFIELDS = set("012568w")

# 주제명은 LCSH 관례대로 ` -- `로 잇는다: "Whiskey -- Tennessee -- History"
_SUBJECT_TAGS = {"600", "610", "650", "651"}

# LDR 6번째 글자(자료유형). 7번째가 's'면 연속간행물로 덮어쓴다.
_LDR_TYPE = {"a": "단행본", "t": "필사자료", "e": "지도자료", "f": "지도자료",
             "c": "악보", "d": "악보", "i": "녹음자료", "j": "녹음자료",
             "g": "영상자료", "k": "평면화상", "m": "전자자료", "r": "입체자료"}

# 008의 언어 코드. ⚠️ 크롤 과정에서 008의 연속 공백이 줄어들어 고정위치 파싱이
# 불가능하다(길이가 25~32자로 제각각). 그래서 '알려진 언어 코드'를 뒤에서부터 찾는다.
_LANG = {"kor": "한국어", "eng": "영어", "jpn": "일본어", "chi": "중국어",
         "fre": "프랑스어", "fra": "프랑스어", "ger": "독일어", "deu": "독일어",
         "spa": "스페인어", "rus": "러시아어", "ita": "이탈리아어", "lat": "라틴어",
         "gre": "그리스어", "grc": "그리스어", "por": "포르투갈어", "ara": "아랍어",
         "heb": "히브리어", "vie": "베트남어", "tha": "태국어", "mon": "몽골어",
         "mul": "다국어", "und": "미상"}


@lru_cache(maxsize=1)
def _conn() -> sqlite3.Connection:
    """읽기 전용 커넥션 하나. 데이터를 메모리에 올리지 않는다(CLAUDE.md 규약 4)."""
    if not COLLECTION_DB.exists():
        raise FileNotFoundError(
            f"본교 장서 DB가 없습니다: {COLLECTION_DB}\n"
            "  드라이브에서 sogang_db_final.db를 받아 data/ 에 넣어주세요 (커밋 대상 아님)."
        )
    c = sqlite3.connect(f"file:{COLLECTION_DB}?mode=ro", uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


# ── MARC 파싱 ─────────────────────────────────────────────
def _subfields(field: str, sep: str) -> str:
    """'▼a Whiskey ▼z Tennessee ▼2 lcsh' → 'Whiskey -- Tennessee' (노이즈 서브필드 제외)."""
    out = []
    for part in field.split("▼"):
        part = part.strip()
        if len(part) < 2 or part[0] in _NOISE_SUBFIELDS:
            continue
        val = part[1:].strip(" .,;:/")
        if val:
            out.append(val)
    return sep.join(out)


def _detail(raw: str | None) -> dict[str, str]:
    """DB의 MARC dict(JSON) → 구 상세정보 화면과 같은 한글 라벨 dict."""
    try:
        marc = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(marc, dict):
        return {}

    out: dict[str, str] = {}

    # 자료유형 — LDR 6~7 (구 화면의 '자료유형' 100% 필드를 대신한다)
    ldr = (marc.get("LDR") or [""])[0]
    if len(ldr) >= 8:
        t = _LDR_TYPE.get(ldr[6])
        if t:
            out["자료유형"] = "연속간행물" if ldr[7] == "s" else t

    # 언어 — 008 뒤쪽에서 알려진 코드를 찾는다. 없으면 041 ▼a로 보완.
    f008 = (marc.get("008") or [""])[0]
    codes = [t for t in re.findall(r"[a-z]{3}", f008) if t in _LANG]
    if codes:
        out["언어"] = _LANG[codes[-1]]
    elif marc.get("041"):
        m = re.search(r"[a-z]{3}", _subfields(marc["041"][0], " "))
        if m and m.group(0) in _LANG:
            out["언어"] = _LANG[m.group(0)]

    for tag, fields in marc.items():
        label = _MARC_LABEL.get(tag)
        if not label or not fields:
            continue
        sep = " -- " if tag in _SUBJECT_TAGS else " "
        vals = [v for v in (_subfields(f, sep) for f in fields) if v]
        if not vals:
            continue
        out[label] = (out[label] + "; " if label in out else "") + "; ".join(vals)
    return out


def _to_book(r: sqlite3.Row) -> CollectionBook:
    return CollectionBook(
        id=r["id"] or "",
        title=r["title"] or "",
        author=r["author"] or "",
        publisher=r["publisher"] or "",
        pub_year=r["pub_year"] or "",
        call_number=r["call_number"] or "",
        ddc_h=r["marc_852h"] or "",
        searched_callno=r["searched_callno"] or "",
        isbn=(r["marc_isbn"] or "").strip() if "marc_isbn" in r.keys() else "",
        detail=_detail(r["detail"]),
    )


_COLS = ("id, title, author, publisher, pub_year, call_number, "
         "marc_852h, searched_callno, marc_isbn, detail")

# 서가 표본 40권을 무엇으로 뽑을까 — 셋을 실측해서 정했다(2026-08-05).
#
#   방식            detail 분량   주제정보 있는 책
#   삽입순             3,828자      38/40      ← 구 head(40)과 같은 성격
#   최신순             2,334자      14/40      ← ✗ 정보가 사라진다
#   주제有 > 최신순     6,736자      40/40      ← ✓ 채택   (332.6324 기준)
#
# 최신순을 먼저 시도했으나 **오히려 정보를 없앴다.** 650(LCSH 주제명)은 미국의회도서관
# 레코드를 복사할 때 딸려오는 것이라 2020년대 한국 신간에는 16%만 붙어 있다.
# 최신 40권을 뽑으면 제목만 남고 주제를 못 읽는다.
#
# 그래서 **주제 정보가 있는 책을 앞에 두고, 그 안에서 최신순**으로 뽑았다.
# ⚠️ 대가: 표본이 복사목록(주로 외서) 쪽으로 기운다. 서가의 **구성**을 묻는 데는 못 쓴다
#    — 그 답은 shelf_count()가 전량 기준으로 따로 준다.
#
# ── 2026-08-12: 반반으로 바꿨다. 알라딘 책소개를 붙이면서 전제가 무너졌다 ──
# 서가 책에 책소개를 붙였는데 40권 중 3~5권밖에 안 붙었다. 원인을 재보니:
#
#   720.2 상위 10권         주제명 10/10 · 책소개  3/10   (1998~2025년 책)
#   720.2 최신순 10권        주제명  0/10 · 책소개 10/10   (2026년 책)
#   028.9 최신순 10권        주제명  0/10 · 책소개 10/10
#
# **두 정보가 서로 다른 책에 붙어 있다.** 주제명은 옛날 책·외서에 몰려 있고(위 이유),
# 알라딘은 최신 한국 책만 덮는다. 주제명 우선으로 뽑으면 **알라딘이 가장 안 듣는 책만
# 골라서 쏘게 된다.** 반대로 최신순으로만 뽑으면 양서 위주 서가(407은 상위 10권 중
# 한국어가 2권뿐)에서 주제명이 통째로 사라져 제목만 남는다.
#
# → 절반씩 나눠 담는다. **주제명 묶음은 주제명으로, 최신 묶음은 책소개로 읽힌다.**
#   ① 주제명 있는 책 최신순으로 절반
#   ② 최신순으로 훑으며 **아직 안 담긴** 책으로 나머지를 채운다(id로 중복 제거)
#   ②를 나중에 도는 게 핵심 — ①에 이미 '최신+주제명' 책이 들어가 있으면
#   ②는 그 다음 최신 책을 집어 온다. 결과적으로 두 정보원이 겹치지 않고 나뉜다.
#
# `id`는 **동점 처리자**다. 같은 조건의 책이 수십 권이라(332.6324의 2025년 47권)
# 이게 없으면 40권 표본이 실행마다 달라져 점수 흔들림의 원인을 못 가린다.
# pub_year는 TEXT라 "1938-1967"·"미상" 같은 값이 4.1% 섞여 있다 → 4자리만 연도로 친다.
_YEAR = "CASE WHEN pub_year GLOB '[0-9][0-9][0-9][0-9]' THEN pub_year ELSE '0000' END"
_ORDER = f"ORDER BY (marc_650!='') DESC, {_YEAR} DESC, id"     # ① 주제명 묶음
_ORDER_RECENT = f"ORDER BY {_YEAR} DESC, id"                   # ② 최신 묶음


# ── 홀드아웃 ──────────────────────────────────────────────
def _is_self(rec_id: str, holdout: tuple[str, str] | None) -> bool:
    """이 레코드가 '분류 대상 책 자신'인가 — **레코드 ID로만 판별한다.**

    빼야 할 ID는 시나리오 JSON의 `holdout_ids`에 못 박혀 있다(config.holdout_ids).
    12건이 각각 정확히 1건이고, 눈으로 확인된 값이다.

    ⚠️ 예전에는 조회할 때마다 제목으로 추측했다("토큰이 거의 겹치면 자신"). 그래서
       **승계해야 할 다른 판본까지 빼버리는 사고**가 났다 — 바빌론에서 개정판 4권이
       통째로 빠져, 정작 승계 케이스의 평가가 무의미해질 뻔했다(2026-08-05).
       무엇을 빼는지는 눈으로 확인 가능해야 하고, 매칭 규칙을 손대도 안 흔들려야 한다.
    """
    if not holdout or not holdout[0]:
        return False
    return rec_id in _holdout_ids(holdout[0])


def _active(holdout: tuple[str, str] | None) -> bool:
    return bool(holdout and holdout[0])


def stats() -> dict:
    row = _conn().execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT marc_852h) k FROM books WHERE marc_852h!=''"
    ).fetchone()
    return {"총 장서": row["n"], "청구기호 종류": row["k"], "상세정보 보유": row["n"]}


# ── 0차: 원제/기존서지 승계 ───────────────────────────────
# 검색 기준 — 원제와 제목이 다르다. 괄호 안에 무엇이 들었는지가 다르기 때문이다(실측).
#
#   원표제의 괄호   관사 92.3%      「(The) Richest man in Babylon」 = 「Richest man…」
#                                 → **벗기고 본다**. 같은 원제가 두 표기로 흩어져 있을 뿐이다.
#   제목의 괄호     수식어 83.7%    「(카자흐스탄의) 바위그림」 ≠ 「(중앙아시아의) 바위그림」
#                  (관사 1.7%)     → **벗기지 않는다**. 벗기면 다른 책이 같은 자리에 모인다.
#
# 제목 쪽을 벗겼더니 실제로 틀렸다: 원본 「바위그림」이 없자 「(카자흐스탄의) 바위그림」(958.45)을
# 물어왔다. 「(중앙아시아의) 바위그림」은 958이다 — 수식어가 다르면 다른 책이다(2026-08-05).
# 대가: 「(개정증보) 차근차근 전통주」 같은 정당한 승계를 놓친다. 그때는 LLM이 판단하므로 회복된다.
_E_246 = ("CASE WHEN marc_246 LIKE '(%' AND instr(marc_246,')')>0 "
          "THEN ltrim(substr(marc_246, instr(marc_246,')')+1)) ELSE marc_246 END")  # 정렬표 ix_246n
_E_TITLE = "title"                                                                  # 정렬표 ix_title

# 승계 후보의 순서. **괄호 없는 것을 먼저** 본다(원제 검색용 안전장치).
# 원제 정렬표는 괄호를 벗겨 만들었으므로 「(The) X」와 「X」가 같은 자리에 모인다.
# 관사면 같은 책이라 문제없지만 7.7%는 관사가 아니므로, 괄호 없는 쪽을 먼저 집는다.
_INH_ORDER = f"ORDER BY (title LIKE '(%'), {_YEAR} DESC, id"

# 접두 뒤에 와도 되는 글자 = **제목이 거기서 끝나고 부제·판차가 시작된다는 표시**.
# 문장부호이거나 끝이어야 한다. 공백은 안 된다 — 공백까지 허용하면 「아메리카」가
# 「아메리카 아이스」를 물어온다(접두는 맞지만 다른 책이다).
#   「차근차근 전통주」 → 「차근차근 전통주」                끝    → 같은 책 ✅
#                      → 「차근차근 전통주 : 우리 술 빚기」   ':'  → 같은 책 ✅ (부제)
#                      → 「차근차근 전통주 / 개정판」        '/'  → 같은 책 ✅ (판차)
#                      → 「아메리카 아이스」                '아'  → 다른 책 ❌
_BOUNDARY = set(":/,.;=[(<|-–—")


def _first_prefix(expr: str, prefix: str,
                  holdout: tuple[str, str] | None) -> CollectionBook | None:
    """`expr`이 `prefix`로 **낱말 경계까지** 일치하는 첫 레코드. 그 책 자신은 건너뛴다."""
    for r in _conn().execute(
        f"SELECT {_COLS}, {expr} AS _key FROM books "
        f"WHERE {expr} >= ? AND {expr} < ? {_INH_ORDER} LIMIT 40",
        (prefix, prefix + "\uffff")
    ):
        tail = (r["_key"] or "")[len(prefix):].lstrip()
        if tail and tail[0] not in _BOUNDARY:
            continue
        if not _is_self(r["id"], holdout):
            return _to_book(r)
    return None


def find_inheritance(book: BookInput,
                     holdout: tuple[str, str] | None = None) -> CollectionBook | None:
    """원서/기존 판본이 본교에 있으면 그 레코드를 돌려준다(청구기호 승계 대상).

    **검색 순서 = 사서 업무 순서**(케이스 분석 docx의 human_trace 그대로):
      (1) 원제가 있으면 **원제부터** — 원표제가 그 원제로 시작하는 책
      (2) 없거나 못 찾으면 제목 — 제목이 입력 제목으로 시작하는 책

    원제를 먼저 보는 이유: **번역서 제목은 출판사가 마음대로 붙인다.**
      「바빌론 부자들의 지혜」·「바빌론 최고의 부자」·「바빌론 부자들의 돈 버는 지혜」
      셋 다 원제가 `Richest man in Babylon`인 같은 책이다. 제목은 흔들리고 원제는 안 흔들린다.
      증거의 강도도 다르다 — 원제 일치는 **같은 저작이 확실**하지만 제목 겹침은 추정이다.

    제목 판정이 "**입력 제목으로 시작**"인 이유: 도서관 제목은 「제목 : 부제 / 판차」 꼴이라
      개정판·증보판이 입력 제목을 앞에 그대로 달고 있다. 예전에는 '단어가 몇 개 겹치나'로
      느슨하게 봤는데 **틀린 승계**를 냈다 —
      「가장 인간적인 도시」→「가장 인간적인 것들의 역사」(572) ·
      「어느 완벽한 이중언어자」→「어느 완벽한 하루」(853.914). 승계가 틀리면 LLM까지 못 가고
      그대로 오답이 되므로 **애매하면 승계하지 않는다.**
      한계: 입력 쪽이 더 길면(「…전통주 개정3판」) 못 찾는다. 그때는 LLM이 판단한다.

    holdout으로 '그 책 자신'은 제외한다(레코드 ID로 판별, 평가 누출 방지).

    ⚠️ 두 정렬표(인덱스)는 **드라이브 배포본에 이미 들어 있다.** 없어도 동작은 같다
       (3ms → 4.7초로 느려질 뿐). 직접 만들려면 아래를 sqlite3에 그대로 붙인다:

           CREATE INDEX IF NOT EXISTS ix_852h  ON books(marc_852h);
           CREATE INDEX IF NOT EXISTS ix_title ON books(title);
           -- 원표제는 **앞 괄호를 벗긴 값**으로 정렬한다. 도서관이 관사(The/A/An)를
           -- 정렬에서 빼려고 괄호로 묶는데 처리가 목록자마다 갈려, 같은 책이
           -- '(' 칸과 'R' 칸으로 흩어진다. 제목에는 적용하지 않는다 —
           -- 제목의 괄호는 83.7%가 수식어라 다른 책이 한자리에 모여버린다.
           CREATE INDEX IF NOT EXISTS ix_246n ON books(
             CASE WHEN marc_246 LIKE '(%' AND instr(marc_246,')')>0
                  THEN ltrim(substr(marc_246, instr(marc_246,')')+1)) ELSE marc_246 END);
    """
    orig = (book.original_title or "").strip()
    if len(orig) >= 5:                       # 너무 짧은 원제는 흔한 낱말과 부딪힌다
        hit = _first_prefix(_E_246, orig, holdout)
        if hit is not None:
            return hit

    title = (book.title or "").strip()
    if len(title) >= 3:
        return _first_prefix(_E_TITLE, title, holdout)
    return None


# ── 1차: 청구기호 검색 ────────────────────────────────────
def shelf_books(ddc_h: str, limit: int = SHELF_SAMPLE,
                holdout: tuple[str, str] | None = None) -> list[CollectionBook]:
    """이 번호대에 꽂힌 본교 책을 **레코드 통째로**(상세정보 포함) 돌려준다.

    제목만으로는 서가 의미가 흐리다("건축만담"만 봐선 뭘 다루는지 모른다).
    상세정보의 `일반주제명`·`지명주제명`·`요약`·`내용주기` 등이 주제를 말해준다.
    무엇을 쓸지는 2차(LLM)가 고르게 두고, 여기서는 거르지 않는다.

    뽑는 방식은 위 `_ORDER`/`_ORDER_RECENT` 주석 참고 — **절반은 주제명 있는 책,
    절반은 최신 책**이다. 두 정보(주제명·알라딘 책소개)가 서로 다른 책에 붙어 있어서다.
    """
    c = _conn()
    half = (limit + 1) // 2          # 홀수면 주제명 묶음에 한 권 더 준다

    def _ids(order: str) -> list[str]:
        rows = c.execute(f"SELECT id FROM books WHERE marc_852h=? {order}", (ddc_h,))
        return [r["id"] for r in rows if not _is_self(r["id"], holdout)]

    # ① 주제명 있는 책부터 절반. ② 최신순으로 훑으며 **안 담긴 책**으로 나머지를 채운다.
    ids = _ids(_ORDER)[:half]
    seen = set(ids)
    for i in _ids(_ORDER_RECENT):
        if len(ids) >= limit:
            break
        if i not in seen:
            ids.append(i)
            seen.add(i)
    if not ids:
        return []

    rows = c.execute(
        f"SELECT {_COLS} FROM books WHERE id IN ({','.join('?' * len(ids))})", ids
    ).fetchall()
    order = {i: n for n, i in enumerate(ids)}
    rows.sort(key=lambda r: order[r["id"]])
    return [_to_book(r) for r in rows]


def shelf_count(ddc_h: str, holdout: tuple[str, str] | None = None) -> int:
    """이 번호대의 본교 소장 건수. **전량 크롤이라 이제 정확하다**(구 JSON은 번호당 50건 상한)."""
    c = _conn()
    if not _active(holdout):
        return c.execute("SELECT COUNT(*) FROM books WHERE marc_852h=?", (ddc_h,)).fetchone()[0]
    return sum(1 for r in c.execute("SELECT id FROM books WHERE marc_852h=?", (ddc_h,))
               if not _is_self(r["id"], holdout))


def has_number(ddc_h: str, holdout: tuple[str, str] | None = None) -> bool:
    """본교에 이 번호대 책이 있나(없으면 서가 의미를 못 읽는다)."""
    if not _active(holdout):
        return _conn().execute(
            "SELECT 1 FROM books WHERE marc_852h=? LIMIT 1", (ddc_h,)).fetchone() is not None
    return shelf_count(ddc_h, holdout) > 0


# ── 키워드 검색 (2026-08-18) ──────────────────────────────
def search_by_keywords(kws: list[str], min_hit: int = 2,
                       holdout: tuple[str, str] | None = None,
                       limit: int = 40) -> list[tuple[str, int]]:
    """검색어들 → `[(청구기호, 걸린 권수)]`, 많은 순.

    **책을 찾는 게 아니라 번호대를 찾는다.** 검색 결과를 `GROUP BY marc_852h`로 접으면
    책 목록이 청구기호 목록이 된다 — 그게 우리가 말하는 클러스터링이고, 별도 알고리즘이
    필요 없는 이유다.

    `min_hit` = 검색어 5개 중 **몇 개 이상 걸린 책만** 셀까. 이게 이 함수의 핵심이다.
    OR(1개만 걸려도 통과)로 두면 흔한 낱말이 큰 서가를 통째로 끌어온다 —
    `#7 게임철학`에서 '철학' 하나가 193(1435권)·100(797)·194(787)을 데려와
    정답 794.801(6권)을 파묻었다. 2로 조이면 그게 빠진다(config.KEYWORD_MIN_HIT 주석).

    `LIKE '%kw%'`(부분일치)를 쓴다. `tokens.py`의 토큰 겹침이 아니다 — 650이 영어라
    부분일치가 유리하다(`bilingual` 하나로 `Bilingualism`·`Bilingual education`이 다 걸린다).

    ⚠️ **홀드아웃을 반드시 통과시킨다.** 안 걸면 그 책 자신이 검색에 걸려서
       자기 청구기호를 근거로 자기 번호를 찾는다 — `794.801`은 총 6권인데 그중 1권이
       「게임으로 철학하기」 그 책이다.

    ⚠️ 인덱스가 없어 88만 건 풀스캔이다(검색어 4개에 7~9초). 한 권 분류에 LLM이
       30~60초 쓰니 견딜 만하다. 실전 배치 때 FTS5를 얹으면 ms로 떨어진다.
    """
    kws = [k.strip() for k in kws if k and k.strip()]
    if not kws:
        return []

    score = " + ".join(
        "(CASE WHEN title LIKE ? OR marc_650 LIKE ? THEN 1 ELSE 0 END)" for _ in kws)
    params: list = []
    for k in kws:
        params += [f"%{k}%", f"%{k}%"]

    where = [f"marc_852h != ''", f"({score}) >= {int(min_hit)}"]
    for rec_id in _holdout_ids(holdout[0]) if _active(holdout) else ():
        where.append("id != ?")
        params.append(rec_id)

    rows = _conn().execute(
        f"SELECT marc_852h, COUNT(*) n FROM books WHERE {' AND '.join(where)} "
        f"GROUP BY marc_852h ORDER BY n DESC, marc_852h LIMIT {int(limit)}",
        params,
    ).fetchall()
    return [(r["marc_852h"], r["n"]) for r in rows]


def subject_top(ddc_h: str, n: int = 3) -> list[tuple[str, int]]:
    """이 번호대 **전체**의 650(일반주제명) 빈출어 상위 n개.

    키워드 후보는 서가를 10권만 보여주므로, 못 보여준 나머지 서가의 성격을 이 한 줄이
    대신 말해준다. 실측으로 갈림길이 세 단어에 드러난다:
        306.446  Multilingualism(19) · Sociolinguistics(18)   → 사회언어학
        400.42   Bilingualism(18) · Education,Bilingual(7)    → 이중언어 교육

    ⚠️ **표본 40권이 아니라 번호대 전량을 센다.** 표본만 세면 숫자가 작아 우연에 흔들린다 —
       102(철학 649권)를 표본으로 세면 2위가 'Grammar, Comparative'(3권)로 나온다.
       전량으로 세면 Philosophy(140) · Philosophical counseling(6)로 성격이 분명하다.
       `shelf_count`가 전량인 것과 같은 이유다.

    홀드아웃은 걸지 않는다 — 한 권 빠진다고 빈출어 순위가 바뀌지 않고, 여기서 나가는 건
    주제명이지 청구기호가 아니라 누출 경로가 아니다.
    """
    cnt: Counter[str] = Counter()
    for (raw,) in _conn().execute(
            "SELECT marc_650 FROM books WHERE marc_852h=? AND marc_650!=''", (ddc_h,)):
        for term in (raw or "").split(";"):
            term = term.strip(" .")
            if term:
                cnt[term] += 1
    return cnt.most_common(n)
