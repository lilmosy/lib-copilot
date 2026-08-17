"""입력 도서 보강 — 알라딘 Open API(TTB)로 `책소개`를 받아 BookInput을 채운다.

**왜 필요한가 (design.md §7.1 키워드 문제):**
제목·저자만으로는 초점을 못 읽는 책이 있다. 「부린 왕자」는 조어 제목이라 '부동산'이
어디에도 안 나오지만, 책소개 첫 줄이 "부동산을 '투자 대상'이 아니라…"로 시작한다.

**왜 국중이 아니라 알라딘인가 (2026-08-01 12케이스 실측):**
  국중 seoji : 책소개 3/12 · 목차 4/12 · 출판사 매칭 3건 오류 + 1건 미수록
  알라딘 TTB : 책소개 12/12 · 매칭 12/12
  (알라딘도 `toc`/`fullDescription`은 "별도 협의 후 제공"이라 빈다 — 요약 책소개로 충분)

**무엇에 쏘나 — 둘이다.**
  ① 분류 대상 책 한 권      `enrich()`   제목→검색→매칭→상세 (3단계, ISBN을 모르니까)
  ② 후보 서가 책 전부        `describe_many()`  ISBN→상세 (1단계, marc_isbn이 이미 있다)

②를 붙인 이유(2026-08-12, 교수님 제안): 지금까지 **분류 대상 책만 책소개를 갖고**
서가 책은 제목·주제명만 있었다. "이 자리에 꽂혀도 어울리나"를 물으면서 한쪽 속만
보여준 셈이다. 처음엔 앞 10권만 쐈는데 그 10권이 하필 옛날 책이라 3~5권밖에 못 받았다
(sogang_db._ORDER 주석 참고) → 지금은 **ISBN 있는 전부**에 쏜다. 캐시가 있어 첫 회차만 든다.

⚠️ **한국 책만, 그것도 최근 책만 덮인다**(2026-08-12 실측). 알라딘에 양서와
   2010년 이전 국내서가 없다. 서가 40권 중 실제로 받는 건 720.2가 29권, 883.01은 1권이다.
   본교 MARC의 `520 요약`은 2.1%뿐이라 대안이 못 된다. 양서 소스는 미정.

**평가 재현성:** 응답은 `data/aladin_cache.json`에 굳힌다. 캐시에 있으면 네트워크를 타지
않으므로, 알라딘 쪽 문구가 바뀌어도 골든셋 점수가 흔들리지 않는다.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request

from config import DATA_DIR
from schema import BookInput

CACHE_PATH = DATA_DIR / "aladin_cache.json"
_SEARCH = "http://www.aladin.co.kr/ttb/api/ItemSearch.aspx"
_LOOKUP = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"


def _key() -> str | None:
    return os.environ.get("ALADIN_TTB_KEY")


# 캐시는 두 칸이다. **찾는 열쇠가 다르기 때문**이다.
#   _by_title  분류 대상 신규 도서 — ISBN을 모르니 제목으로 찾는다(검색→매칭→상세, 3단계)
#   _by_shelf  그 책을 분류하며 들여다본 본교 서가 책들 — marc_isbn이 있으니 상세 조회 1단계
#              **분류 대상 책 → 청구기호 → ISBN** 세 겹이다. 어느 책을 분류하다 어느 서가를
#              봤는지가 그대로 보여야 팀원이 "이 서가는 어떤 동네인가"를 읽을 수 있다.
# 서가 책은 새 책이 들어오기 전까지 안 바뀌므로 한 번 받으면 계속 쓴다.
# 캐시 파일은 커밋한다 → 팀원이 클론하면 알라딘을 다시 안 부른다.
_T, _I = "_by_title", "_by_shelf"


def _load_cache() -> dict:
    """캐시를 연다. **구 형식(제목이 최상위 키)도 그대로 읽는다.**"""
    if not CACHE_PATH.exists():
        return {_T: {}, _I: {}}
    raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    if _T in raw or _I in raw:
        return {_T: raw.get(_T, {}), _I: raw.get(_I, {})}
    return {_T: raw, _I: {}}                       # 구 형식 → 제목 칸으로 옮긴다


def _save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def _get(url: str, **params) -> dict:
    # 알라딘 JS 출력은 BOM이 붙고 끝에 ';'가 오기도 한다.
    raw = urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=25).read()
    return json.loads(raw.decode("utf-8-sig").strip().rstrip(";"))


def _clean(s: str | None) -> str:
    """알라딘 문자열에서 HTML 엔티티를 푼다.

    JSON으로 오지만 값 안에 웹 표시용 인코딩이 섞여 있다(2026-08-18 발견):
        Histoire des &#x00E9;pices au Moyen ...   →   Histoire des épices au Moyen ...
    캐시 36개 필드 중 1개꼴로 드물지만, 하필 그게 **원제**였고 원제는 0차 승계의 열쇠다.
    깨진 문자열로 marc_246 접두검색을 하면 한 건도 안 걸린다.
    """
    return html.unescape((s or "").strip())


def _name_tokens(s: str) -> set[str]:
    """저자 문자열에서 이름 토큰만. '하야시 유타카 지음 ; 유서윤 옮김' → {하야시, 유타카, 유서윤}"""
    s = re.sub(r"\((지은이|옮긴이|엮은이)\)|지음|옮김|엮음|편저|저|역", " ", s or "")
    return {t for t in re.findall(r"[가-힣A-Za-z]{2,}", s)}


def _pick(items: list[dict], author: str) -> tuple[dict | None, str]:
    """저자가 겹치는 항목을 고른다. 없으면 첫 항목(매칭 근거를 함께 반환)."""
    want = _name_tokens(author)
    if want:
        for it in items:
            if want & _name_tokens(it.get("author", "")):
                return it, "author"
    return (items[0], "title_only") if items else (None, "none")


def lookup(title: str, author: str = "") -> dict | None:
    """제목으로 검색 → 저자로 매칭 확인 → ISBN13으로 상세 조회. 캐시 우선."""
    cache = _load_cache()
    ck = title.strip()
    if ck in cache[_T]:
        return cache[_T][ck]

    ttb = _key()
    if not ttb:
        return None                      # 키 없으면 조용히 건너뛴다(파이프라인은 계속 돈다)

    try:
        s = _get(_SEARCH, ttbkey=ttb, Query=title, QueryType="Title", MaxResults=20,
                 SearchTarget="Book", output="JS", Version="20131101")
        hit, how = _pick(s.get("item", []), author)
        if hit is None:
            return None
        d = _get(_LOOKUP, ttbkey=ttb, itemIdType="ISBN13", ItemId=hit.get("isbn13", ""),
                 output="JS", Version="20131101", OptResult="Toc,Story,fullDescription")
        it = (d.get("item") or [{}])[0]
        sub = it.get("subInfo", {})
        rec = {
            "title": _clean(it.get("title")),
            "publisher": _clean(it.get("publisher")),
            "pub_date": it.get("pubDate", ""),
            "isbn13": (it.get("isbn13") or "").strip(),
            # 요약 책소개. fullDescription/toc은 별도 협의 대상이라 대개 빈다.
            "description": _clean(it.get("fullDescription") or it.get("description")),
            "toc": _clean(sub.get("toc")),
            "sub_title": _clean(sub.get("subTitle")),
            "original_title": _clean(sub.get("originalTitle")),
            # 서점 분류. **정답이 아니라 '뒤집히기 쉬운 방향'의 신호**다 — docx의
            # "기관 간 분류 상이"가 실측됐다(부린왕자 알라딘=성공학 ↔ 작성자 179.9 → 교열 332.6324).
            "category": it.get("categoryName", ""),
            "matched_by": how,
        }
    except Exception:
        return None                      # 네트워크 실패로 평가가 죽지 않게

    cache[_T][ck] = rec
    _save_cache(cache)
    return rec


def describe_many(books: list, for_title: str = "(미상)") -> dict[str, str]:
    """본교 서가 책들 → {isbn: 책소개}. **캐시에 없는 것만 부른다.**

    `for_title`은 지금 분류 중인 책 제목 — 캐시에서 최상위 묶음이 된다.

    `lookup()`과 다른 점은 **검색 단계가 없다**는 것이다. 서가 책은 `marc_isbn`이
    이미 있어서 상세 조회 한 번으로 끝나고, 제목 검색의 오매칭 위험도 없다.

    캐시는 **분류 대상 책 → 청구기호 → ISBN** 세 겹으로 쌓는다. 팀원이 파일만 열어도
    "이 책을 분류하며 어느 서가들을 봤고, 그 서가가 어떤 동네인가"를 읽을 수 있어야 한다 —
    ISBN을 평평하게 늘어놓으면 숫자만 보이고 아무것도 안 읽힌다.
    ⚠️ 조회는 ISBN 하나로 하므로, 읽을 때 세 겹을 평평하게 눌러 색인을 만든다.
       서가가 겹치면 같은 책이 두 묶음에 들어가지만 캐시라 문제 없다(디스크만 조금 더).

    ⚠️ **못 찾은 것도 빈 책소개로 캐시한다.** 안 그러면 양서(알라딘 미보유)를
       회차마다 다시 물어본다 — 서가 절반 이상이 양서인 번호대가 있다.

    ⚠️ 캐시 저장은 **한 번에 한 번만**. 권마다 저장하면 12건 회차에서 파일을 500번 쓴다.
    """
    cache = _load_cache()
    ttb = _key()
    dirty = False
    book_box = cache[_I].setdefault(for_title, {})
    # 이미 어느 묶음에든 받아둔 ISBN은 다시 안 부른다(서가가 겹칠 때 중복 호출 방지).
    known = {i: v.get("책소개", "")
             for bk in cache[_I].values() for sh in bk.values() for i, v in sh.items()}
    for b in books:
        isbn = (getattr(b, "isbn", "") or "").strip()
        if not isbn:
            continue
        shelf = book_box.setdefault(getattr(b, "ddc_h", "") or "(미상)", {})
        if isbn in shelf:
            continue
        if isbn in known:
            shelf[isbn] = {"제목": getattr(b, "title", "")[:80], "책소개": known[isbn]}
            dirty = True
            continue
        desc = ""
        if ttb:
            try:
                d = _get(_LOOKUP, ttbkey=ttb, itemIdType="ISBN13", ItemId=isbn,
                         output="JS", Version="20131101", OptResult="fullDescription")
                it = (d.get("item") or [{}])[0]
                desc = (it.get("fullDescription") or it.get("description") or "").strip()
            except Exception:
                desc = ""                    # 네트워크 실패로 평가가 죽지 않게
        shelf[isbn] = {"제목": getattr(b, "title", "")[:80], "책소개": desc}
        dirty = True
    if dirty:
        _save_cache(cache)

    flat = {i: v.get("책소개", "")
            for bk in cache[_I].values() for sh in bk.values() for i, v in sh.items()}
    return {(getattr(b, "isbn", "") or "").strip(): flat.get((getattr(b, "isbn", "") or "").strip(), "")
            for b in books}


# (BookInput 필드, 알라딘 레코드 키).
# 입력에 비어 있을 때만 알라딘 값으로 채운다 → 손으로 적은 값이 항상 이긴다.
_ENRICH_FIELDS = (
    ("description", "description"),
    ("original_title", "original_title"),
    ("subtitle", "sub_title"),
    # ISBN은 **판단에 안 쓴다.** 회차 json에 남겨두는 것뿐이다 —
    # 나중에 종합서지를 제목 대신 ISBN으로 조회하게 될 때 열쇠가 된다(design.md §9.1).
    ("isbn", "isbn13"),
)


def enrich(book: BookInput) -> BookInput:
    """비어 있는 입력 칸을 알라딘에서 받아 채운다. 값이 있으면 손대지 않는다.

    책소개·원제·부제 세 칸을 채운다. 원제가 중요한 이유: 시나리오 JSON에는 사람이
    미리 적어뒀지만(`"original_title": "Richest man in Babylon"`), **실전에서 사서가
    원제를 직접 찾아 적지는 않는다.** 알라딘은 준다 → 0차 승계가 실전에서도 작동한다.

    ⚠️ `category`(알라딘 서점분류)는 받아서 캐시에만 남기고 **입력에는 넣지 않는다.**
       사서가 실제 업무에서 보는 자료가 아니고(서점 진열용 분류), 실측 2건 모두
       오답 방향을 가리켰다(가장 인간적인 도시=인문학↔720.2 · 부린왕자=성공학↔332.6324).
    """
    need = [(dst, src) for dst, src in _ENRICH_FIELDS if not getattr(book, dst, None)]
    if not need:
        return book
    rec = lookup(book.title, book.author or "")
    if not rec:
        return book
    upd = {dst: rec[src].strip() for dst, src in need if (rec.get(src) or "").strip()}
    return book.model_copy(update=upd) if upd else book
