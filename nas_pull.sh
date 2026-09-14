#!/bin/sh
# 시놀로지 DSM → 제어판 → 작업 스케줄러 → 사용자 정의 스크립트 에 붙여넣기.
# 실행 시각 권장: 매일 08:20, 20:20 (깃허브 액션이 끝난 뒤)
# 사용자: root (또는 DEST 폴더 쓰기 권한 있는 계정)

# ─── 설정 3줄만 바꾸면 됨 ───────────────────────────
REPO="깃허브아이디/저장소이름"
TOKEN=""                                   # 비공개 저장소면 PAT 넣기, 공개면 빈칸
DEST="/volume1/폴더명/유튜브캡처"
# ───────────────────────────────────────────────────

mkdir -p "$DEST" || exit 1
LOG="$DEST/_sync.log"
TMP="/tmp/ytcap_$$.tar.gz"

if [ -n "$TOKEN" ]; then
  curl -sfL -o "$TMP" -H "Authorization: Bearer $TOKEN" \
       "https://api.github.com/repos/$REPO/tarball/snapshots"
else
  curl -sfL -o "$TMP" \
       "https://codeload.github.com/$REPO/tar.gz/refs/heads/snapshots"
fi

if [ $? -ne 0 ]; then
  echo "$(date '+%F %T') 다운로드 실패" >> "$LOG"
  rm -f "$TMP"
  exit 1
fi

# 압축 안 풀리면 기존 파일은 그대로 둔다 (덮어쓰기 사고 방지)
if tar xzf "$TMP" -C "$DEST" --strip-components=1; then
  N=$(ls -1d "$DEST"/*/ 2>/dev/null | wc -l)
  echo "$(date '+%F %T') OK  누적 폴더 ${N}개" >> "$LOG"
else
  echo "$(date '+%F %T') 압축해제 실패" >> "$LOG"
  rm -f "$TMP"
  exit 1
fi

rm -f "$TMP"
