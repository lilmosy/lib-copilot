# CLAUDE.md — lib_copilot

> 세션 시작 시 자동 로드된다. **내용을 여기 복붙하지 말고** 아래 문서로 안내만 한다.

## 한 줄

도서관 청구기호(852 ▼h)를 매기는 사서-AI 협업 에이전트.
**쉬운 책은 자동으로 끝내고, 판단이 갈리는 책만 사람에게 넘긴다.**

## ⚠️ 배선이 둘이다 (2026-08-18~)

```
lib_copilot/
├── docs · prompts는 아니고 —— 아래가 **공유**다 (한 벌만 있다)
│   docs/  crawler/  README.md  CLAUDE.md  .gitignore  .env.example  requirements.txt
│   20260818_AB종합.md   ab_report.html   run_all.sh
│
├── lib_copilot_a/   버전 A — 병합.   082+종합서지+키워드를 한 자리에 놓고 한 번 판단
└── lib_copilot_b/   버전 B — 조건부. 082+종합서지로 먼저 판단하고, 미달일 때만 키워드
```

**a와 b가 다른 파일은 `src/pipeline.py` 하나뿐이다.** 프롬프트·검색·병합·스키마·평가는
글자까지 같다 — 그래야 "이 차이 때문이다"라고 말할 수 있다.
**a/b 안의 공유 파일(`src/*`, `prompts/*`, `evaluate.py` 등)을 고치면 양쪽 다 고쳐야 한다.**
문서·설정은 상위에 한 벌뿐이라 그 걱정이 없다.

B의 `data/sogang_db_final.db`·`union_db.json`은 A를 가리키는 심볼릭 링크다(실물 하나).

비교가 끝나면 진 쪽을 지운다. 쟁점은 **후보를 늘리는 게 이득인가**와
**B의 트리거(LLM 확신도)가 확신하며 틀린 건을 못 잡는 문제**다 — design.md §3, devlog 2026-08-18.

> **⏳ 다음 구조 개편**: 지금은 `src/`가 a·b에 두 벌이라 손으로 맞춰야 한다.
> `pipeline_a.py`/`pipeline_b.py`만 남기고 나머지를 상위 `src/` 하나로 합치면
> "글자까지 같다"가 규칙이 아니라 **구조**가 된다. 3개 상한 재실행이 끝난 뒤에 한다
> — 지금 바꾸면 재실행 결과에 변수가 둘이 된다.

## 먼저 읽을 것 (진짜 내용은 여기 있다)

- **[docs/design.md](docs/design.md)** — 설계 정본. 문제의식(§1) · 사서 업무(§2) · 시스템(§3) ·
  데이터(§4) · 스키마(§5) · 평가(§7) · **규약(§8)** · 한계(§9) · **확장 미구현(§11)**
- **[docs/devlog.md](docs/devlog.md)** — 시간순 기록. "왜 이렇게 됐나"는 여기
- **[README.md](README.md)** — 팀원 진입점. 받아서 돌리는 법

## 구조

**실행은 `lib_copilot_a/` 또는 `lib_copilot_b/` 안에서 한다.** 상위에서 하지 않는다.

```
cd lib_copilot_a          (또는 lib_copilot_b)

  evaluate.py  골든셋 12건 채점   python evaluate.py --label=xxx --runs=3
  run.py       책 한 권 돌려보기   python run.py <book.json>       ← 터미널 출력만
  app.py       데모 화면          streamlit run app.py
  src/         라이브러리 (직접 실행하지 않는다)
  prompts/     시스템 프롬프트 — **코드가 아니라 파일이다**
                 systemprompt_1st.txt      082 정합성
                 systemprompt_keyword.txt  검색어 만들기
                 systemprompt_2nd.txt      최종 판단   ← B는 이걸 두 번 쓴다
  data/        골든셋 · 책소개 캐시 · DB(커밋 안 함)
  output/      회차 json (커밋 안 함 — 프롬프트 전문이 들어 있어 1MB씩)

상위에서:
  ./run_all.sh              4조합 × 3회차를 순서대로 (약 40분)
  20260818_AB종합.md         회차 종합 보고서
  ab_report.html            같은 내용의 웹 보고서 (가로 탭)
```

흐름(A): **보강 → 0차 승계 → 082 후보 → LLM① 082 정합성 → 게이트 → 종합서지
→ LLM② 키워드 추출 → 키워드 검색 → LLM③ 최종 판단 → 코드 판정**

## ⚠️ 반드시 지킬 것

1. **외부에서 받아온 산출물은 커밋하지 않는다** — 드라이브로 배포한다.
   `data/sogang_db_final.db`(1.3GB) · `data/union_db.json` · `data/aladin_cache.json`.
   `git add data/`를 통째로 하지 말 것. **골든셋(`data/scenarios/`)만 커밋 대상**이다.
   책소개 캐시는 출판사 저작물이라 저장소가 Public이 되면서 뺐다(2026-08-18).
2. **평가 경로를 바꿨으면 `--label`을 바꿔 새 회차로 남긴다.** 회차 산출물은 덮어쓰지 않는다.
3. **LLM 호출은 `classify.py` 안에서만 한다.** 지금 셋이다 —
   `check_prior()` · `extract_keywords()` · `classify()`. 다른 파일에서 부르지 않는다.
4. **결정 결과를 장서 DB에 되쓰지 않는다.** 누출을 매번 새로 만드는 짓이다. 앱은 읽기 전용.
5. **홀드아웃을 모든 조회에 통과시킨다** — 승계·서가·**키워드 검색** 전부.
   안 걸면 그 책 자신이 검색에 걸려 자기 번호를 근거로 자기 번호를 찾는다
   (`794.801`은 6권인데 그중 1권이 「게임으로 철학하기」다).
6. **스키마에 뺐던 필드를 다시 넣지 말 것** — `confidence`/`ambiguity`/`signals`.
   원칙: **시스템이 이미 아는 것은 LLM에게 묻지 않는다.**
7. **키워드 추출 호출에 서가를 보여주지 말 것.** 082 서가를 보여주면 그 어휘로 물들어,
   082가 오답인 케이스에서 검색이 오답 쪽으로 끌려간다 — design.md §3.3.

## ⚠️ 결과를 읽을 때

**LLM 판단이라 같은 조건에서도 실행마다 흔들린다.** `--runs=3`이 기본이다.
**항상 맞음 / 항상 틀림 / 흔들림**으로 나눠 봐야 한다. 고칠 가치가 있는 건 "항상 틀림"이고,
"흔들림"을 쫓으면 노이즈를 쫓는 것이다.

평가 산출물은 **json만** 만든다(md는 안 만든다). 여러 조합을 다 돌린 뒤 한 장으로 종합한다.

## 작업 후

시행착오와 결정은 `docs/devlog.md`에, 설계가 바뀌었으면 `docs/design.md`도 같이 고친다.

---

> `AGENTS.md` 는 이 파일을 가리키는 심볼릭 링크다. 에이전트마다 읽는 파일 이름이 달라서
> (Claude Code는 `CLAUDE.md`, Codex 등은 `AGENTS.md`) 두 이름으로 같은 내용을 준다.
> **내용을 복사해 두 벌로 만들지 말 것** — 한쪽만 고치면 에이전트마다 다른 지시를 받는다.
