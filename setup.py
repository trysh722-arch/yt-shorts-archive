"""깃허브 저장소 생성 → push → 시크릿 등록 → 첫 실행까지 한 번에.

  python setup.py <깃허브토큰> <시트URL또는ID> [저장소이름]

토큰은 classic PAT 기준이며 scope 는 repo + workflow 두 개면 된다.
토큰은 화면·파일 어디에도 남기지 않는다.
"""
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import gspread
import requests
from nacl import encoding, public

API = "https://api.github.com"
SA_FILE = Path(r"C:\Users\METABUZZ33\Desktop\클로드\시트API\service_account.json")
WORKFLOW = "capture.yml"


def die(msg):
    print(f"\n✗ {msg}")
    sys.exit(1)


def step(n, msg):
    print(f"\n[{n}] {msg}")


def sheet_id_of(s):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", s)
    return m.group(1) if m else s.strip()


def gh(token, method, path, **kw):
    r = requests.request(method, API + path, timeout=30, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }, **kw)
    return r


def seal(public_key_b64, value):
    """깃허브 시크릿용 sealed box 암호화."""
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
    return base64.b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()


def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode:
        die(f"git {' '.join(args[:2])} 실패: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def main():
    if len(sys.argv) < 3:
        die("사용법: python setup.py <깃허브토큰> <시트URL> [저장소이름]")
    token = sys.argv[1].strip()
    sheet_id = sheet_id_of(sys.argv[2])
    repo_name = sys.argv[3] if len(sys.argv) > 3 else "yt-shorts-archive"

    # ── 1. 시트가 서비스계정에게 열려 있는지 (여기서 막히면 나머지가 무의미)
    step(1, "시트 접근 확인")
    if not SA_FILE.exists():
        die(f"서비스계정 파일이 없습니다: {SA_FILE}")
    try:
        sh = gspread.service_account(filename=str(SA_FILE)).open_by_key(sheet_id)
    except Exception as e:
        die("시트를 못 엽니다. cj-dashboard@madbox-354708.iam.gserviceaccount.com 에 "
            f"편집자로 공유했는지 확인해주세요.\n   ({str(e)[:160]})")
    print(f"    OK — '{sh.title}' (탭 {len(sh.worksheets())}개)")

    # ── 2. 토큰 확인
    step(2, "깃허브 토큰 확인")
    r = gh(token, "GET", "/user")
    if r.status_code != 200:
        die(f"토큰이 안 먹습니다 ({r.status_code}). scope 에 repo·workflow 가 있는지 확인해주세요.")
    owner = r.json()["login"]
    scopes = r.headers.get("x-oauth-scopes", "")
    print(f"    OK — {owner}  (scope: {scopes or 'fine-grained'})")

    # ── 3. 저장소 (없으면 만들고, 있으면 그대로 쓴다)
    step(3, f"공개 저장소 {owner}/{repo_name}")
    r = gh(token, "GET", f"/repos/{owner}/{repo_name}")
    if r.status_code == 200:
        print("    이미 있음 — 그대로 사용")
        if r.json().get("private"):
            print("    ⚠ 비공개 저장소입니다. 시트 썸네일이 안 보입니다. 공개로 바꿔주세요.")
    else:
        r = gh(token, "POST", "/user/repos",
               json={"name": repo_name, "private": False, "auto_init": False,
                     "description": "유튜브 최신 Shorts 선반 자동 캡처 아카이브"})
        if r.status_code not in (201,):
            die(f"저장소 생성 실패 ({r.status_code}): {str(r.json())[:200]}")
        print("    생성 완료")

    # ── 4. push
    step(4, "코드 push")
    git("remote", "remove", "origin", check=False)
    git("remote", "add", "origin", f"https://github.com/{owner}/{repo_name}.git")
    push_url = f"https://{owner}:{token}@github.com/{owner}/{repo_name}.git"
    r = git("push", push_url, "main:main", check=False)
    if r.returncode:
        die(f"push 실패: {(r.stderr or r.stdout).strip()[:300]}")
    print("    OK")

    # ── 5. 시크릿 2개
    step(5, "시크릿 등록")
    r = gh(token, "GET", f"/repos/{owner}/{repo_name}/actions/secrets/public-key")
    if r.status_code != 200:
        die(f"공개키를 못 받았습니다 ({r.status_code}). 토큰 scope 에 repo 가 필요합니다.")
    pk = r.json()
    for name, value in (("GOOGLE_SA_JSON", SA_FILE.read_text(encoding="utf-8")),
                        ("SHEET_ID", sheet_id)):
        r = gh(token, "PUT", f"/repos/{owner}/{repo_name}/actions/secrets/{name}",
               json={"encrypted_value": seal(pk["key"], value), "key_id": pk["key_id"]})
        if r.status_code not in (201, 204):
            die(f"{name} 등록 실패 ({r.status_code})")
        print(f"    {name} OK")

    # ── 6. 첫 실행
    step(6, "첫 실행 시작")
    before = gh(token, "GET", f"/repos/{owner}/{repo_name}/actions/runs?per_page=1")
    last_id = (before.json().get("workflow_runs") or [{}])[0].get("id")
    r = gh(token, "POST",
           f"/repos/{owner}/{repo_name}/actions/workflows/{WORKFLOW}/dispatches",
           json={"ref": "main"})
    if r.status_code != 204:
        die(f"실행 요청 실패 ({r.status_code}): {r.text[:200]}\n"
            f"   Actions 탭에서 직접 Run workflow 를 눌러도 됩니다.")
    print("    요청 완료 — 결과를 기다립니다 (보통 3~5분)")

    # ── 7. 결과 확인
    step(7, "결과 확인")
    run = None
    for _ in range(90):                       # 최대 15분
        time.sleep(10)
        runs = gh(token, "GET",
                  f"/repos/{owner}/{repo_name}/actions/runs?per_page=5").json()
        cand = [x for x in runs.get("workflow_runs", []) if x["id"] != last_id]
        if not cand:
            continue
        run = cand[0]
        if run["status"] == "completed":
            break
        print(f"    …{run['status']}")
    if not run:
        die("실행이 시작되지 않았습니다. Actions 탭을 확인해주세요.")

    url = run["html_url"]
    if run.get("conclusion") == "success":
        print(f"    성공 — {url}")
        print(f"\n✓ 끝났습니다. 시트를 열어보세요:\n"
              f"  https://docs.google.com/spreadsheets/d/{sheet_id}")
        print(f"  저장소: https://github.com/{owner}/{repo_name}")
        print("\n이후로는 _키워드 탭에 줄만 추가하면 오전 8시·오후 8시에 저절로 쌓입니다.")
    else:
        print(f"    실패({run.get('conclusion')}) — {url}")
        die("실행 로그를 확인해야 합니다. 위 주소를 알려주시면 제가 원인을 보겠습니다.")


if __name__ == "__main__":
    main()
