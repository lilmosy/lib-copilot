"""상세페이지 크롤러 — collection.json의 각 책을 한 번 더 클릭해 서지 상세를 채운다.

`callno_crawler.py`가 긁은 건 **목록페이지**뿐이라 `category=null`, `keywords=[]`로 비어 있다.
여기서는 각 책의 상세페이지에 들어가 `table.profiletable`의 th/td를 **통째로** 담는다.

상세URL은 이미 갖고 있다 — 목록페이지 제목 링크 `<a href="/search/detail/{id}">`에서
`callno_crawler`가 id를 뽑아 저장해뒀다. 그래서 조립만 하면 된다:
    BASE/search/detail/{id}

**필드를 미리 고르지 않는다.** 라벨이 23종 이상이고 책마다 다르며(3~98%), 뭐가 쓸모
있을지는 나중에 정해진다. 재크롤이 30분이라 그때 가서 다시 긁는 건 비싸다.
→ 나온 th/td를 전부 `detail`에 중첩해 담고, 해석은 파이프라인 쪽에서 한다.
   (AGENTS.md "안 지어냄" — 원자료 보존, 해석은 나중)

출력 3종:
  1) data/collection.json      — 각 레코드에 `detail` 키 추가 (기존 10필드는 그대로)
  2) data/collection.csv       — 기존 10컬럼 + 등장한 상세 라벨 **전부**를 컬럼으로
  3) data/detail_coverage.json — 라벨별 출현율 ("ISBN 75%, 목차 0%")
                                 = 어떤 필드를 믿고 쓸 수 있나 + 크롤 깨짐 감지

실행:
    python crawler/detail_crawler.py            # 전체 (약 30분)
    python crawler/detail_crawler.py --limit 20 # 맛보기
    python crawler/detail_crawler.py --resume   # detail 없는 것만 (중단 후 이어하기)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://library.sogang.ac.kr"
SEARCH = BASE + "/search/toc/result"
DETAIL = BASE + "/search/detail/"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
COLLECTION = DATA_DIR / "collection.json"
OUT_CSV = DATA_DIR / "collection.csv"
OUT_COVERAGE = DATA_DIR / "detail_coverage.json"

DELAY_SEC = 0.5      # 요청 간 대기 — 학교 서버 예의
COOKIE_TTL = 270     # verified 쿠키 재발급 주기(초). 5분 만료보다 여유
CHECKPOINT = 50      # 이 건수마다 중간 저장. 상세페이지 응답이 ~7초라 전체 3시간이 걸리는데,
                     # 끝에만 저장하면 도중에 죽을 때 전부 날아간다. --resume이 의미를 가지려면
                     # 중간 저장이 필수다.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
}

# 기존 10필드 (callno_crawler와 1:1로 맞춤). detail은 그 뒤에 붙는다.
_BASE_FIELDS = ["id", "title", "author", "publisher", "pub_year",
                "call_number", "ddc_h", "category", "keywords", "searched_callno"]
_CSV_LABELS = {
    "id": "서지ID", "title": "제목", "author": "저자", "publisher": "출판사",
    "pub_year": "출판년", "call_number": "청구기호", "ddc_h": "분류기호",
    "category": "분류항목", "keywords": "주제어", "searched_callno": "검색청구기호",
}


class Fetcher:
    """verified 봇검증 쿠키를 관리하며 상세페이지를 가져온다.

    callno_crawler와 같은 우회: 첫 요청으로 쿠키를 받고, 이후 `__verified_refresh=1`을
    붙여 재요청한다. Session이 쿠키를 자동 반송하고, 5분 만료라 주기적으로 재발급한다.
    """

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._verified_at = 0.0

    def _ensure_verified(self) -> None:
        if time.time() - self._verified_at < COOKIE_TTL:
            return
        self.s.get(SEARCH, params={"st": "FRNT", "si": "12", "q": " 000", "pn": "1"},
                   timeout=20, allow_redirects=False)
        self._verified_at = time.time()

    def detail(self, bib_id: str) -> BeautifulSoup:
        self._ensure_verified()
        r = self.s.get(DETAIL + bib_id, params={"__verified_refresh": "1"}, timeout=25)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")


def parse_detail(soup: BeautifulSoup) -> dict[str, str]:
    """`table.profiletable`의 th/td 쌍을 전부 dict로. 라벨을 고르지 않는다.

    같은 라벨이 여러 번 나오면(주제명 등) ' | '로 합친다.
    """
    tbl = soup.select_one("table.profiletable")
    if not tbl:
        return {}
    fields: dict[str, str] = {}
    for tr in tbl.select("tr"):
        th, td = tr.find("th"), tr.find("td")
        if not (th and td):
            continue
        k = re.sub(r"\s+", " ", th.get_text(strip=True))
        v = re.sub(r"\s+", " ", td.get_text(" ", strip=True))
        if not (k and v):
            continue
        fields[k] = f"{fields[k]} | {v}" if k in fields else v
    return fields


def crawl(records: list[dict], limit: int | None, resume: bool) -> tuple[int, int]:
    fetch = Fetcher()
    targets = [r for r in records if not (resume and r.get("detail"))]
    if limit:
        targets = targets[:limit]
    print(f"대상 {len(targets)}건 (전체 {len(records)}건)")

    done = failed = 0
    for i, rec in enumerate(targets, 1):
        try:
            rec["detail"] = parse_detail(fetch.detail(rec["id"]))
            done += 1
        except Exception as e:  # 한 건 실패로 전체를 멈추지 않는다
            rec["detail"] = {}
            failed += 1
            print(f"  ⚠️ {rec['id']} 실패: {type(e).__name__}")
        if i % CHECKPOINT == 0 or i == len(targets):
            save_json(records)          # ← 중간 저장. 여기서 죽어도 --resume으로 이어간다.
            print(f"  {i}/{len(targets)}  (성공 {done} · 실패 {failed}) 💾저장")
        time.sleep(DELAY_SEC)
    return done, failed


# ── 저장 ──────────────────────────────────────────────────
def save_json(records: list[dict]) -> None:
    COLLECTION.write_text(json.dumps(records, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def save_csv(records: list[dict]) -> None:
    """기존 10컬럼 + 등장한 상세 라벨을 **전부** 컬럼으로.

    컬럼 집합은 미리 못 정한다 → 실제로 나온 라벨의 합집합으로 만든다.
    대부분 빈칸이 되지만(내용주기 3% 등) 의도된 것. CSV는 재크롤 없이 JSON에서 재생성 가능.
    """
    seen = Counter()
    for r in records:
        seen.update(r.get("detail", {}).keys())
    detail_cols = [k for k, _ in seen.most_common()]   # 출현 많은 라벨부터

    header = [_CSV_LABELS[k] for k in _BASE_FIELDS] + detail_cols
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:  # 엑셀 한글 호환
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in records:
            row = {}
            for k in _BASE_FIELDS:
                v = r.get(k)
                row[_CSV_LABELS[k]] = "; ".join(v) if isinstance(v, list) else (v or "")
            d = r.get("detail", {})
            for c in detail_cols:
                row[c] = d.get(c, "")
            w.writerow(row)
    print(f"  CSV 컬럼 {len(header)}개 (기본 {len(_BASE_FIELDS)} + 상세 {len(detail_cols)})")


def save_coverage(records: list[dict]) -> None:
    """라벨별 출현율. 두 가지에 쓴다:
    ① 어떤 필드를 파이프라인에 쓸지 결정 (58%는 쓰고 3%는 안 씀)
    ② 크롤 깨짐 감지 (ISBN이 갑자기 20%로 떨어지면 HTML 구조가 바뀐 것)
    """
    with_detail = [r for r in records if r.get("detail")]
    n = len(with_detail)
    cnt = Counter()
    for r in with_detail:
        cnt.update(r["detail"].keys())
    cov = {
        "total_records": len(records),
        "with_detail": n,
        "without_detail": len(records) - n,
        "labels": {
            k: {"count": c, "rate": round(c / n, 3) if n else 0.0}
            for k, c in cnt.most_common()
        },
    }
    OUT_COVERAGE.write_text(json.dumps(cov, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"\n라벨 출현율 (n={n}):")
    for k, v in list(cov["labels"].items())[:15]:
        print(f"  {v['count']:5d}/{n}  {v['rate']:5.0%}  {k}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="이 건수만 (맛보기)")
    ap.add_argument("--resume", action="store_true", help="detail 없는 것만")
    args = ap.parse_args()

    records = json.loads(COLLECTION.read_text(encoding="utf-8"))
    done, failed = crawl(records, args.limit, args.resume)

    save_json(records)
    save_csv(records)
    save_coverage(records)
    print(f"\n완료: 성공 {done} · 실패 {failed}")
    print(f"  JSON     → {COLLECTION}")
    print(f"  CSV      → {OUT_CSV}")
    print(f"  커버리지 → {OUT_COVERAGE}")


if __name__ == "__main__":
    main()
