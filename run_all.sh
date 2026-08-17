#!/bin/bash
# 4조합 × 3회차. 알라딘 캐시 파일을 공유하므로 **순차로** 돈다.
set -u
cd "$(dirname "$0")"
log() { echo "[$(date +%H:%M:%S)] $*"; }

# ── a·b 동기화 검사 ────────────────────────────────────
# A와 B는 src/pipeline.py 하나만 달라야 한다. 한쪽만 고치면 비교가 조용히 오염되므로
# 회차를 돌리기 전에 자동으로 잡는다. (CLAUDE.md 규약을 코드로 옮긴 것)
# `-q`로 "어느 파일이 다른가"만 받는다 — 내용까지 받으면 pipeline.py 본문이 쏟아진다.
DIFF=$(diff -rq -x '__pycache__' -x 'data' -x 'output' -x 'runs' -x '.DS_Store' \
       lib_copilot_a lib_copilot_b 2>&1 | grep -v 'pipeline\.py')
if [ -n "$DIFF" ]; then
  echo "❌ a와 b가 pipeline.py 말고도 다릅니다 — 비교가 오염됩니다:"
  echo "$DIFF"
  exit 1
fi
log "✅ a·b 동기화 확인 (pipeline.py만 다름)"

log "=== A_on (병합 · 앞 서가 넣음) ==="
( cd lib_copilot_a && LIBCOPILOT_SEEN_SHELF=on  python3 -u evaluate.py --label=A_on  --runs=3 )
log "=== A_off (병합 · 앞 서가 생략) ==="
( cd lib_copilot_a && LIBCOPILOT_SEEN_SHELF=off python3 -u evaluate.py --label=A_off --runs=3 )
log "=== B_on (조건부 · 앞 서가 넣음) ==="
( cd lib_copilot_b && LIBCOPILOT_SEEN_SHELF=on  python3 -u evaluate.py --label=B_on  --runs=3 )
log "=== B_off (조건부 · 앞 서가 생략) ==="
( cd lib_copilot_b && LIBCOPILOT_SEEN_SHELF=off python3 -u evaluate.py --label=B_off --runs=3 )
log "=== 전부 끝 ==="
ls -1 lib_copilot_a/output/*.json lib_copilot_b/output/*.json
