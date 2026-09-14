"""유튜브 검색 → 채널 '최신 Shorts 동영상' 선반 더보기 펼쳐서 캡처.

사용법: python capture.py "배달의 민족" [출력폴더]
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

KST = timezone(timedelta(hours=9))
SHELF_TEXT = "최신 Shorts 동영상"      # '배달의민족의 최신 Shorts 동영상' 부분일치
SHELF_SEL = "grid-shelf-view-model"   # 2026-09 기준 유튜브 선반 태그
VIEWPORT = {"width": 1920, "height": 1080}
TILE_H = 1080                          # 긴 세로 캡처 분할 높이
JPEG_Q = 92                            # 저장 품질(용량 절감)
PAD = 16                               # 선반 좌우 여백

ITEMS_JS = """(shelf) => [...shelf.querySelectorAll('ytm-shorts-lockup-view-model')].map(e => {
  const a = e.querySelector('a[href]');
  return {txt: e.innerText || '', href: a ? a.getAttribute('href') : ''};
})"""


def _views_to_int(text):
    """'조회수 9.4만회' -> 94000. 못 읽으면 None."""
    m = re.search(r"([\d.]+)\s*(억|만|천)?회", text.replace(",", ""))
    if not m:
        return None
    n = float(m.group(1))
    return int(n * {"억": 100000000, "만": 10000, "천": 1000}.get(m.group(2), 1))


def _items(shelf):
    """선반 안 쇼츠 목록을 [{순번,제목,조회수,조회수원문,영상ID,새동영상}] 로."""
    out = []
    for i, r in enumerate(shelf.evaluate(ITEMS_JS), start=1):
        lines = [x.strip() for x in r["txt"].splitlines() if x.strip()]
        is_new = bool(lines) and lines[0] == "새 동영상"
        if is_new:
            lines = lines[1:]
        views = next((x for x in lines if "조회수" in x), "")
        title = next((x for x in lines if "조회수" not in x), "")
        out.append({
            "순번": i,
            "제목": title,
            "조회수": _views_to_int(views),
            "조회수원문": views,
            "영상ID": (r["href"] or "").rsplit("/", 1)[-1],
            "새동영상": "Y" if is_new else "",
        })
    return out


def _dismiss_consent(page):
    """미국 서버에서 뜨는 쿠키 동의 화면 처리."""
    for label in ("모두 수락", "Accept all", "Reject all", "모두 거부"):
        btn = page.get_by_role("button", name=label)
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(2000)
            return True
    return False


def _find_shelf(page, max_scroll=40):
    shelf = page.locator(SHELF_SEL).filter(has_text=SHELF_TEXT)
    for _ in range(max_scroll):
        if shelf.count():
            shelf.first.scroll_into_view_if_needed()
            page.wait_for_timeout(800)
            return shelf.first
        page.evaluate("window.scrollBy(0, 2500)")
        page.wait_for_timeout(800)
    return None


def _load_all_thumbs(page, shelf):
    """펼친 선반 썸네일이 전부 뜨도록 훑어 내렸다 올라온다."""
    box = shelf.bounding_box()
    if not box:
        return
    steps = int(box["height"] / 600) + 2
    for _ in range(steps):
        page.evaluate("window.scrollBy(0, 600)")
        page.wait_for_timeout(350)
    page.wait_for_timeout(1500)
    for _ in range(steps):
        page.evaluate("window.scrollBy(0, -600)")
        page.wait_for_timeout(120)
    page.wait_for_timeout(800)


def _shot_shelf(page, shelf, path):
    """고정 헤더 가리고 좌우 여백 준 채로 선반 전체를 한 장으로."""
    page.add_style_tag(content="ytd-masthead,#masthead-container{visibility:hidden !important}")
    page.wait_for_timeout(300)
    box = shelf.bounding_box()
    scroll_y = page.evaluate("window.scrollY")
    page.screenshot(path=path, full_page=True, type="jpeg", quality=JPEG_Q, clip={
        "x": max(0, box["x"] - PAD),
        "y": box["y"] + scroll_y,
        "width": box["width"] + PAD * 2,
        "height": box["height"],
    })
    page.add_style_tag(content="ytd-masthead,#masthead-container{visibility:visible !important}")


def _slice(png_path, out_dir, stem):
    img = Image.open(png_path)
    w, h = img.size
    parts = []
    for i, top in enumerate(range(0, h, TILE_H), start=1):
        bottom = min(top + TILE_H, h)
        if bottom - top < 120:        # 자투리는 버림
            break
        p = out_dir / f"{stem}_part{i:02d}.jpg"
        img.crop((0, top, w, bottom)).convert("RGB").save(p, quality=JPEG_Q, subsampling=0)
        parts.append(p)
    return parts


def capture(query, out_root):
    stamp = datetime.now(KST).strftime("%y%m%d_%H%M")
    out_dir = Path(out_root) / f"{re.sub(r'[^0-9A-Za-z가-힣]+', '', query)}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = [f"query={query}", f"captured_at={datetime.now(KST).isoformat()}"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--lang=ko-KR"])
        ctx = browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport=VIEWPORT,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
        )
        ctx.add_cookies([{"name": "PREF", "value": "hl=ko&gl=KR",
                          "domain": ".youtube.com", "path": "/"}])
        page = ctx.new_page()

        url = ("https://www.youtube.com/results?search_query="
               + query.replace(" ", "+") + "&persist_hl=1&hl=ko&persist_gl=1&gl=KR")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if "consent" in page.url or _dismiss_consent(page):
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("ytd-item-section-renderer", timeout=30000)
        page.wait_for_timeout(3000)

        page.screenshot(path=out_dir / "01_검색상단.jpg", type="jpeg", quality=JPEG_Q)
        log.append("01_검색상단.jpg OK")

        shelf = _find_shelf(page)
        if shelf is None:
            log.append(f"ERROR: '{SHELF_TEXT}' 선반 못 찾음 (유튜브 구조 변경 의심)")
            page.screenshot(path=out_dir / "99_실패화면.jpg", type="jpeg", quality=JPEG_Q)
            (out_dir / "meta.txt").write_text("\n".join(log), encoding="utf-8")
            browser.close()
            print("\n".join(log))
            sys.exit(1)

        title = shelf.locator("h2").first.inner_text().strip()
        log.append("shelf_title=" + title)
        _shot_shelf(page, shelf, out_dir / "02_선반_접힘.jpg")
        log.append("02_선반_접힘.jpg OK")

        more = shelf.locator("button").filter(has_text=re.compile("더보기|Show more"))
        if more.count():
            more.first.click()
            page.wait_for_timeout(2500)
            log.append("더보기 클릭 OK")
        else:
            log.append("WARN: 더보기 버튼 없음")

        _load_all_thumbs(page, shelf)

        items = _items(shelf)
        n = len(items)
        collapse = shelf.locator("button").filter(has_text=re.compile("간략히|Show less")).count()
        log.append(f"shorts_count={n}")
        log.append(f"완전펼침확인(간략히버튼)={'YES' if collapse else 'NO'}")

        full = out_dir / "03_선반_전체.jpg"
        _shot_shelf(page, shelf, full)
        w, h = Image.open(full).size
        log.append(f"03_선반_전체.jpg OK ({w}x{h})")
        log.append(f"분할 {len(_slice(full, out_dir, '03_선반'))}장")

        browser.close()

    (out_dir / "meta.txt").write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))
    print(f"\n→ {out_dir}")
    return out_dir


if __name__ == "__main__":
    capture(sys.argv[1] if len(sys.argv) > 1 else "배달의 민족",
            sys.argv[2] if len(sys.argv) > 2 else "out")
