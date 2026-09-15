"""시트의 _키워드 탭을 읽어 키워드마다 캡처 → 이미지 커밋 → 시트에 행 추가.

  python run.py             # 시트에 적힌 키워드 전부
  python run.py "배달의 민족"  # 한 키워드만

순서가 중요하다. 이미지를 먼저 깃허브에 올린 뒤에 시트가 그 주소를 참조해야 한다.
반대로 하면 구글이 없는 주소를 먼저 읽고 실패를 캐시해서 썸네일이 깨진 채로 남는다.

환경변수
  SHEET_ID    아카이브 시트 ID (필수)
  REPO        깃허브 owner/repo (필수, 액션이 자동 주입)
  GSHEET_SA   서비스계정 json 경로 (기본 service_account.json)
  BRANCH      기본 main
  SKIP_PUSH   1이면 git 커밋·푸시를 건너뛴다 (내 PC에서 시험할 때)
"""
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from PIL import Image

import capture
import sheet

KST = timezone(timedelta(hours=9))
ARCHIVE = Path("archive")
ARCHIVE_W = 1000      # 보관본 가로 폭. 원본 1282 -> 용량 약 40% 절감
ARCHIVE_Q = 82
# 보관 대상. 분할본(part)은 선반 전체 이미지에서 언제든 다시 자를 수 있어 뺀다.
KEEP = ["01_검색상단.jpg", "02_선반_접힘.jpg", "03_선반_전체.jpg", capture.TOP_FILE]

SHEET_ID = os.environ.get("SHEET_ID", "")
SA_PATH = os.environ.get("GSHEET_SA", "service_account.json")


def raw_url(rel_path, repo=None, branch=None):
    """archive/... 상대경로를 raw.githubusercontent 주소로 (한글은 퍼센트 인코딩)."""
    repo = os.environ.get("REPO", "") if repo is None else repo
    branch = os.environ.get("BRANCH", "main") if branch is None else branch
    enc = "/".join(quote(p, safe="") for p in Path(rel_path).parts)
    return f"https://raw.githubusercontent.com/{repo}/{quote(branch, safe='')}/{enc}"


def archive(result, keyword):
    """보관 대상만 줄여서 archive/<키워드>/<연도>/<시각>/ 에 넣는다.

    ponytail: 연도 폴더로 나눠두니 용량이 차면 지난 연도만 통째로 지우면 된다.
    키워드 3개를 넘기면 1년 안에 깃허브 권장 용량(1GB)에 닿는다.
    """
    dest = ARCHIVE / sheet.tab_name(keyword) / f"20{result['stamp'][:2]}" / result["stamp"]
    dest.mkdir(parents=True, exist_ok=True)
    for name in KEEP:
        src = result["dir"] / name
        if not src.exists():
            continue
        im = Image.open(src)
        if im.width > ARCHIVE_W:
            im = im.resize((ARCHIVE_W, round(im.height * ARCHIVE_W / im.width)),
                           Image.LANCZOS)
        im.convert("RGB").save(dest / name, quality=ARCHIVE_Q, subsampling=0)
    for name in ("items.json", "meta.txt"):
        shutil.copy(result["dir"] / name, dest / name)
    return dest


def push_images(msg):
    """archive/ 를 커밋·푸시. 원격이 앞서면 rebase 후 한 번만 다시 푸시한다."""
    if os.environ.get("SKIP_PUSH") == "1":
        print("SKIP_PUSH=1 → 커밋 건너뜀")
        return False  # 원격 업로드를 확인하지 않았으므로 IMAGE()를 만들면 안 된다.
    branch = os.environ.get("BRANCH", "main")

    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120,
                              env={**os.environ, "LC_ALL": "C"})

    def checked(*a):
        result = git(*a)
        if result.returncode:
            raise RuntimeError(f"git {' '.join(a)}: {result.stderr.strip()[:300]}")
        return result

    try:
        checked("config", "user.name", "github-actions")
        checked("config", "user.email", "actions@github.com")
        checked("add", "--", "archive")
        diff = git("diff", "--cached", "--quiet", "--", "archive")
        if diff.returncode == 0:
            print("커밋할 이미지 없음 (기존 로컬 커밋은 푸시 확인)")
        elif diff.returncode == 1:
            checked("commit", "-m", msg, "--only", "--", "archive")
        else:
            raise RuntimeError(f"git diff 실패: {diff.stderr.strip()[:300]}")
        r = git("push", "origin", f"HEAD:{branch}")
        if r.returncode and any(s in r.stderr.lower() for s in ("non-fast-forward", "fetch first")):
            print("원격에 새 커밋이 있음 → rebase 후 푸시 1회 재시도")
            try:
                checked("pull", "--rebase", "origin", branch)
            except (OSError, subprocess.SubprocessError, RuntimeError):
                try:
                    abort = git("rebase", "--abort")
                    if abort.returncode:
                        print("rebase 정리 확인:", abort.stderr.strip()[:300])
                except (OSError, subprocess.SubprocessError) as cleanup_error:
                    print(f"rebase 정리 실패: {cleanup_error}")
                raise
            r = git("push", "origin", f"HEAD:{branch}")
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"이미지 업로드 실패: {error}")
        return False
    if r.returncode:
        print("푸시 실패:", r.stderr.strip()[:300])
        return False
    print("이미지 푸시 완료")
    return True


def main():
    if not SHEET_ID:
        sys.exit("SHEET_ID 환경변수가 없습니다.")
    if not os.environ.get("REPO"):
        sys.exit("REPO 환경변수가 없습니다. 예: REPO=아이디/저장소")

    sh = sheet.open_sheet(SHEET_ID, SA_PATH)
    targets = [(sys.argv[1], None)] if len(sys.argv) > 1 else sheet.keywords(sh)
    if not targets:
        sys.exit("_키워드 탭에 사용중인 키워드가 없습니다.")
    print(f"대상 키워드 {len(targets)}개: {[t[0] for t in targets]}")

    when = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    done, failed = [], []

    # 1단계 — 캡처하고 이미지만 모아둔다 (시트는 아직 안 건드림)
    for keyword, row in targets:
        try:
            sheet.tab_name(keyword)  # 예약 탭명 충돌을 캡처 전에 검사한다.
            result = capture.capture(keyword, "out")
            dest = archive(result, keyword)
        except capture.CaptureFailed as error:
            failed.append((keyword, row, error.reason))
            print(f"[{keyword}] {error}")
            continue
        except Exception as e:
            failed.append((keyword, row, f"기타 예외: {type(e).__name__}: {str(e)[:200]}"))
            traceback.print_exc()
            continue
        done.append((keyword, row, result, dest))

    # 2단계 — 이미지를 먼저 올린다
    pushed = push_images(f"capture {when}") if done else True

    # 3단계 — 그 다음에 시트에 행을 쌓는다
    for keyword, row, result, dest in done:
        thumb_file = dest / result["thumb"]
        tw, th = Image.open(thumb_file).size
        thumb_h = max(40, round(sheet.THUMB_W * th / tw))
        sheet.append_run(sh, keyword, when, result["title"], result["count"],
                         raw_url(thumb_file), thumb_h,
                         raw_url(dest / "03_선반_전체.jpg"),
                         raw_url(dest / "01_검색상단.jpg"), images_uploaded=pushed,
                         shelf_missing=result["shelf_missing"])
        sheet.append_items(sh, keyword, when, result["items"])
        if row:
            note = "OK" if result["expanded"] else "OK (일부만 펼쳐짐)"
            if result["shelf_missing"]:
                note = "OK (채널 선반 없음 · 일반 Shorts 4세트로 대체)"
            if not pushed:
                note = "이미지 업로드 실패"
            sheet.mark_keyword(sh, row, when, f"{note} · Shorts {result['count']}개")
        print(f"[{keyword}] 시트 기록 완료 (Shorts {result['count']}개)")

    for keyword, row, why in failed:
        if row:
            sheet.mark_keyword(sh, row, when, why)

    print(f"\n완료 {len(done)}/{len(targets)}")
    if not done and any(why != "채널 Shorts 선반 없음" for _, _, why in failed):
        sys.exit(1)


if __name__ == "__main__":
    main()
