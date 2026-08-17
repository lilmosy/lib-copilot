"""청구기호(분류기호) 기준 크롤러 — 로욜라도서관.

제목→책→클릭 3단계 없이, 청구기호 하나로 그 번호대 전체를 바로 리스트업한다.
검색 축: st=FRNT (청구기호). devlog 2026-07-15 실증 참조.

핵심 우회: 사이트가 __verified_refresh 봇 검증을 건다.
  1) 첫 요청 → 302 + Set-Cookie(verified, Max-Age=300s=5분)
  2) 같은 URL에 &__verified_refresh=1 붙여 재요청(쿠키 포함) → 200 + 결과
requests.Session()이 쿠키를 자동 반송. verified는 5분 만료 → 주기적 재발급.

출력 3종 (JSON 필드 = CSV 컬럼, 1:1로 맞춤. 라벨만 영/한 차이):
  1) ../data/collection.json      — retrieve.py가 읽는 스키마 (다음 단계 바로 사용)
  2) ../data/collection.csv       — 같은 필드를 한글 컬럼으로 (사람이 보기용)
  3) ../data/crawl_coverage.json  — 번호대별 커버리지(총 몇 건 중 몇 건 수집, 잘림 여부)

※ 상세URL은 저장 안 함 — id에서 결정론적으로 조립됨: BASE/search/detail/{id}

실행: python crawler/callno_crawler.py
"""

from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── 설정 (여기만 바꾸면 됨) ────────────────────────────────
# 12케이스 검색 과정에 등장한 청구기호 전체 (중복 제거). 케이스 분석 문서 기준.
CALL_NUMBERS = [
    "174.4", "332.024", "332.6",                              # 1 바빌론
    "029.4", "028", "028.9",                                  # 2 이 책만큼은
    "641.3383", "641.338309",                                 # 3 향신료
    "663.1", "641.210951",                                    # 4 전통주
    "720.105", "720.2", "720.1", "720.563", "711.4",          # 5 인간적인 도시
    "363.19262", "363.192", "363.19", "363",                  # 6 오렌지주스
    "102", "794.8", "794.801",                                # 7 게임철학
    "781.63", "780.2", "796", "780.4202", "792.02",           # 8 피아노맨
    "381.45002", "658.827",                                   # 9 서점
    "306.446", "401.93", "407", "400.42", "400.7", "400.72", "370.117",  # 10 이중언어자
    "650.1", "179.9", "332.6324", "811.36",                   # 11 부린왕자
    "291.13", "028.1", "883", "883.01",                       # 12 일리아스 (028·028.9는 위 중복)
]
import os
# 번호대당 페이지 상한 (10건/페이지). 기본 5(=50건). 전체 재크롤은 LIBCOPILOT_MAX_PAGES로 상향.
MAX_PAGES = int(os.environ.get("LIBCOPILOT_MAX_PAGES", "5"))
DELAY_SEC = 0.8         # 요청 간 대기(초) — 서버 예의
COOKIE_TTL = 270        # verified 쿠키 재발급 주기(초, 5분보다 여유)

BASE = "https://library.sogang.ac.kr"
SEARCH = BASE + "/search/toc/result"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_JSON = DATA_DIR / "collection.json"
OUT_CSV = DATA_DIR / "collection.csv"
OUT_COVERAGE = DATA_DIR / "crawl_coverage.json"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
}

CALLNO_RE = re.compile(r"\[([^\]]+)\]")           # 소장처 안 [청구기호]
BIBID_RE = re.compile(r"/search/detail/(\w+)")
DDC_RE = re.compile(r"\d+(?:\.\d+)?")            # 청구기호에서 분류기호(숫자.숫자)
# 총 건수: 사이드바 '단행본(750)' 또는 본문 '750 건'
TOTAL_FACET_RE = re.compile(r"단행본\s*\(\s*([\d,]+)\s*\)")
TOTAL_GEON_RE = re.compile(r"([\d,]+)\s*건")


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()


def _label_map(li) -> dict:
    m = {}
    for dt in li.select("dl > dt.title"):
        nxt = dt.find_next_sibling()
        if nxt and nxt.name == "dd":
            m[dt.get_text(strip=True)] = nxt
    return m


def _extract_ddc(call_number: str) -> str | None:
    """'891.733 C515CM E' → '891.733'. 로케이션 접두어(LA/P 등)는 건너뛴다."""
    m = DDC_RE.search(call_number or "")
    return m.group(0) if m else None


def _parse_total(soup) -> int | None:
    """검색 결과 총 건수. 이게 있어야 '수집한 수 < 총수 = 잘림'을 판정한다."""
    text = soup.get_text(" ")
    if m := TOTAL_FACET_RE.search(text):
        return int(m.group(1).replace(",", ""))
    if m := TOTAL_GEON_RE.search(text):
        return int(m.group(1).replace(",", ""))
    return None


def parse_item(li) -> dict:
    m = _label_map(li)
    title, bibid, detail_url = "", "", ""
    if (td := m.get("서명")) and (a := td.find("a", href=True)):
        title = _clean(a.get_text())
        href = a["href"].split("?")[0]
        detail_url = BASE + href
        if mb := BIBID_RE.search(href):
            bibid = mb.group(1)

    def g(label):
        return _clean(m[label].get_text()) if label in m else ""

    # 소장처 정보에서 [청구기호]만 뽑는다 (소장위치/대출상태는 우리 스키마에 안 씀).
    call_numbers = []
    if hold := m.get("소장처 정보"):
        for loc in hold.select("p.location"):
            if cn := CALLNO_RE.search(_clean(loc.get_text(" "))):
                call_numbers.append(_clean(cn.group(1)))

    return {
        "bibid": bibid, "title": title, "author": g("저자"),
        "publisher": g("출판사"), "pubyear": g("출판년"),
        "call_numbers": list(dict.fromkeys(call_numbers)),
    }


class Fetcher:
    """verified 쿠키를 관리하며 검색 결과 페이지를 가져온다."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._verified_at = 0.0

    def _ensure_verified(self):
        if time.time() - self._verified_at < COOKIE_TTL:
            return
        self.s.get(SEARCH, params={"st": "FRNT", "si": "12", "q": " 000", "pn": "1"},
                   timeout=20, allow_redirects=False)
        self._verified_at = time.time()

    def page(self, call_number: str, pn: int) -> BeautifulSoup:
        self._ensure_verified()
        params = {"st": "FRNT", "si": "12", "q": f" {call_number}", "pn": str(pn),
                  "__verified_refresh": "1"}
        r = self.s.get(SEARCH, params=params, timeout=20)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")


def crawl() -> tuple[list[dict], list[dict]]:
    """반환: (collection 레코드들, 커버리지 로그)."""
    fetch = Fetcher()
    seen: set[str] = set()
    records: list[dict] = []
    coverage: list[dict] = []

    for cn_query in CALL_NUMBERS:
        print(f"\n[청구기호] {cn_query}")
        total_in_catalog = None
        collected_here = 0
        pages_done = 0
        for pn in range(1, MAX_PAGES + 1):
            soup = fetch.page(cn_query, pn)
            if pn == 1:
                total_in_catalog = _parse_total(soup)
            items = soup.select("ul.resultList > li.items")
            if not items:
                print(f"  p{pn}: 결과 없음")
                break
            pages_done = pn
            for li in items:
                rec = parse_item(li)
                if not rec["title"]:
                    continue
                key = rec["bibid"] or (rec["title"], rec["author"])
                if key in seen:
                    continue
                seen.add(key)
                match_cn = next(
                    (c for c in rec["call_numbers"] if _extract_ddc(c) == cn_query),
                    rec["call_numbers"][0] if rec["call_numbers"] else "",
                )
                ddc_h = _extract_ddc(match_cn) or cn_query
                records.append({
                    "id": rec["bibid"] or f"{cn_query}-{len(records)}",
                    "title": rec["title"],
                    "author": rec["author"],
                    "publisher": rec["publisher"],
                    "pub_year": rec["pubyear"],
                    "call_number": match_cn,        # 전체 청구기호 (▼h+▼i+▼m)
                    "ddc_h": ddc_h,                 # 분류기호만 (▼h)
                    "category": None,               # 목록페이지엔 없음 — 지어내지 않음
                    "keywords": [],                 # 목록페이지엔 없음
                    "searched_callno": cn_query,    # 출처: 어느 번호 검색에서 걸렸나
                })
                collected_here += 1
            print(f"  p{pn}: 누적 {len(records)}건 (이 번호대 {collected_here}건)")
            time.sleep(DELAY_SEC)

        truncated = bool(total_in_catalog and collected_here < total_in_catalog)
        coverage.append({
            "searched_callno": cn_query,
            "total_in_catalog": total_in_catalog,   # 카탈로그가 알려주는 총 건수
            "collected": collected_here,            # 우리가 실제로 담은 수
            "pages_crawled": pages_done,
            "max_pages": MAX_PAGES,
            "truncated": truncated,                 # True면 더 있는데 상한에서 잘림
            "missed": (total_in_catalog - collected_here) if truncated else 0,
        })
        if truncated:
            print(f"  ⚠️ 잘림: 총 {total_in_catalog}건 중 {collected_here}건만 수집 "
                  f"(-{total_in_catalog - collected_here})")
        # 중간 저장: 번호 하나 끝날 때마다 덤프(도중에 죽어도 진행분 보존, AGENTS.md 크롤 규약)
        save_json(records)
        save_coverage(coverage)

    return records, coverage


# ── 저장 ──────────────────────────────────────────────────
# JSON 필드 = CSV 컬럼 (1:1, 라벨만 영/한). 순서도 동일.
_FIELDS = ["id", "title", "author", "publisher", "pub_year",
           "call_number", "ddc_h", "category", "keywords", "searched_callno"]
_CSV_LABELS = {
    "id": "서지ID", "title": "제목", "author": "저자", "publisher": "출판사",
    "pub_year": "출판년", "call_number": "청구기호", "ddc_h": "분류기호",
    "category": "분류항목", "keywords": "주제어", "searched_callno": "검색청구기호",
}


def save_json(records: list[dict]):
    slim = [{k: r[k] for k in _FIELDS} for r in records]
    OUT_JSON.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")


def save_csv(records: list[dict]):
    """JSON과 같은 필드를 한글 컬럼으로. utf-8-sig: 엑셀 한글 호환."""
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[_CSV_LABELS[k] for k in _FIELDS])
        w.writeheader()
        for r in records:
            row = {}
            for k in _FIELDS:
                v = r[k]
                if isinstance(v, list):      # keywords → "a; b" (지금은 빈 리스트)
                    v = "; ".join(v)
                elif v is None:              # category=null → 빈 칸
                    v = ""
                row[_CSV_LABELS[k]] = v
            w.writerow(row)


def save_coverage(coverage: list[dict]):
    OUT_COVERAGE.write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    records, coverage = crawl()
    save_json(records)
    save_csv(records)
    save_coverage(coverage)

    print(f"\n완료: {len(records)}건")
    print(f"  JSON     → {OUT_JSON}")
    print(f"  CSV      → {OUT_CSV}")
    print(f"  커버리지 → {OUT_COVERAGE}")
    from collections import Counter
    print("분포:", dict(Counter(r["ddc_h"] for r in records)))
    trunc = [c["searched_callno"] for c in coverage if c["truncated"]]
    if trunc:
        print("잘린 번호대(더 있음):", trunc)


if __name__ == "__main__":
    main()
