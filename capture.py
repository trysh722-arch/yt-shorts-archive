"""유튜브 검색 → 채널 '최신 Shorts 동영상' 선반 더보기 펼쳐서 캡처.

썸네일용으로 검색 결과 맨 위 일반 'Shorts' 선반(5개)도 따로 찍는다.
채널 선반이 없는 검색어는 일반 'Shorts' 선반 5개×4세트를 이어 붙여 선반 전체 이미지로 대신한다.

사용법: python capture.py "배달의 민족" [출력폴더]
"""
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlsplit

from PIL import Image
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

KST = timezone(timedelta(hours=9))
SHELF_TEXT = "최신 Shorts 동영상"      # '배달의민족의 최신 Shorts 동영상' 부분일치
SHELF_MARK = '[data-hobis-shelf="1"]'
TOP_TEXT = "Shorts"                    # 검색 결과에 흩어진 일반 'Shorts' 선반 제목(정확히 일치)
TOP_MARK = "data-hobis-top"            # 일반 선반 표식: 값 = DOM 순서 1..N
TOP_SETS = 4                           # 채널 선반 없을 때 모을 일반 선반 수 (5개×4세트)
TOP_FILE = "04_Shorts상단.jpg"          # 시트 썸네일 = 맨 위 일반 Shorts 선반
CARD_SEL = "ytm-shorts-lockup-view-model, ytd-reel-item-renderer"
VIEWPORT = {"width": 1920, "height": 1080}
TILE_H = 1080                          # 긴 세로 캡처 분할 높이
JPEG_Q = 92                            # 저장 품질(용량 절감)

ITEMS_JS = """(shelf) => [...shelf.querySelectorAll(
  'ytm-shorts-lockup-view-model, ytd-reel-item-renderer')].map(e => {
  const a = e.querySelector('a[href*="/shorts/"], a[href*="/watch?"]');
  return {txt: e.innerText || '', href: a ? a.getAttribute('href') : ''};
})"""


class CaptureFailed(Exception):
    """시트에 쓸 짧은 reason과 로그에 남길 detail을 분리한다."""

    def __init__(self, reason, detail):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


def _views_to_int(text):
    """'조회수 9.4만회' -> 94000. 못 읽으면 None."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(억|만|천)?\s*회", (text or "").replace(",", ""))
    if not m:
        return None
    n = float(m.group(1))
    return int(n * {"억": 100000000, "만": 10000, "천": 1000}.get(m.group(2), 1))


def _parse_items(records):
    """DOM에서 읽은 {txt, href} 목록을 네트워크 없이 파싱한다."""
    out = []
    for i, r in enumerate(records, start=1):
        lines = [x.strip() for x in (r.get("txt") or "").splitlines() if x.strip()]
        is_new = "새 동영상" in lines
        lines = [x for x in lines if x != "새 동영상"]
        views = next((x for x in lines if re.fullmatch(
            r"조회수\s*(?:[\d,.]+\s*(?:억|만|천)?\s*회|없음)", x)), "")
        title = next((x for x in lines if x != views), "")
        url = urlsplit(r.get("href") or "")
        video_id = (url.path.split("/shorts/", 1)[1].split("/", 1)[0]
                    if "/shorts/" in url.path else parse_qs(url.query).get("v", [""])[0])
        out.append({
            "순번": i,
            "제목": title,
            "조회수": _views_to_int(views),
            "조회수원문": views,
            "영상ID": video_id,
            "새동영상": "Y" if is_new else "",
        })
    return out


def _items(shelf):
    return _parse_items(shelf.evaluate(ITEMS_JS))


def _interstitial(page):
    """차단/동의 화면은 일반적인 검색 로딩 실패와 구분한다."""
    text = page.locator("body").inner_text(timeout=3000).lower()
    if ("/sorry/" in page.url or any(s in text for s in (
            "confirm you’re not a bot", "confirm you're not a bot",
            "confirm you are not a bot", "unusual traffic", "로봇이 아님", "봇이 아님",
            "로봇이 아닌지", "봇이 아닌지"))):
        return "봇체크 화면"
    if (urlsplit(page.url).netloc.startswith("consent.") or any(s in text for s in (
            "before you continue to youtube", "youtube로 이동하기 전에",
            "youtube를 계속 사용하기 전에"))):
        return "동의 화면"
    for label in ("모두 수락", "Accept all", "Reject all", "모두 거부"):
        if page.get_by_role("button", name=label, exact=True).first.is_visible():
            return "동의 화면"
    return None


def _dismiss_consent(page):
    """미국 서버에서 뜨는 쿠키 동의 화면 처리."""
    for label in ("모두 수락", "Accept all", "Reject all", "모두 거부"):
        btn = page.get_by_role("button", name=label, exact=True).first
        if btn.is_visible():
            btn.click(timeout=5000)
            page.wait_for_timeout(2000)
            return True
    return False


def _mark_shelf(page, log):
    """태그 이름에 기대지 않고 '…최신 Shorts 동영상' 선반을 표시해 둔다.

    유튜브가 세션마다 다른 레이아웃을 주기 때문에 grid-shelf-view-model 이 아닐 수 있다.
    제목 텍스트를 먼저 찾고, 거기서 위로 올라가며 쇼츠 카드를 품은 가장 가까운 조상을 고른다.
    """
    found = page.evaluate("""(want) => {
      document.querySelectorAll('[data-hobis-shelf]')
              .forEach(e => e.removeAttribute('data-hobis-shelf'));
      const candidates = [...document.querySelectorAll('h2, h3, span, yt-formatted-string')]
        .filter(e => (e.innerText || '').includes(want));
      // 중첩된 제목 span은 한 번만 세고, DOM 순서를 유지한다.
      const titles = candidates.filter(e => !candidates.some(p => p !== e && p.contains(e)));
      const names = titles.map(e => e.innerText.trim().replace(/\\s+/g, ' '));
      const result = {titles: names, title: null};
      const title = titles[0];
      if (!title) return result;
      let n = title;
      for (let i = 0; i < 10 && n; i++) {
        if (['BODY', 'MAIN', 'YTD-SEARCH', 'YTD-SECTION-LIST-RENDERER'].includes(n.tagName)) return result;
        const cards = n.querySelectorAll('ytm-shorts-lockup-view-model, ytd-reel-item-renderer');
        if (cards.length > 0) {
          if (cards.length > 150 || titles.some(t => t !== title && n.contains(t))) return result;
          // 일반 검색 결과까지 품는 조상은 선반이 아니다.
          if (n.querySelector('ytd-video-renderer, yt-lockup-view-model')) return result;
          n.setAttribute('data-hobis-shelf', '1');
          n.setAttribute('data-hobis-title', names[0]);
          result.title = names[0];
          return result;
        }
        n = n.parentElement;
      }
      return result;
    }""", SHELF_TEXT)
    entry = "일치 선반 제목(DOM 순서): " + json.dumps(found["titles"], ensure_ascii=False)
    if found["titles"] and entry not in log:
        log.append(entry)
    return found["title"]


TOP_JS = """([want, mark, limit]) => {
  document.querySelectorAll('[' + mark + ']').forEach(e => e.removeAttribute(mark));
  const heads = [...document.querySelectorAll('h2, h3, span, yt-formatted-string')]
    .filter(e => (e.innerText || '').trim() === want);
  const titles = heads.filter(e => !heads.some(p => p !== e && p.contains(e)));
  let k = 0;
  for (const t of titles) {
    let n = t.parentElement;
    for (let i = 0; i < 8 && n; i++, n = n.parentElement) {
      // 일반 검색 결과까지 품는 조상은 선반이 아니다 (상단 필터 칩 'Shorts'가 여기서 걸러진다).
      if (n.querySelector('ytd-video-renderer, yt-lockup-view-model')) break;
      if (n.querySelectorAll('ytm-shorts-lockup-view-model, ytd-reel-item-renderer').length) {
        n.setAttribute(mark, String(++k));
        break;
      }
    }
    if (k >= limit) break;
  }
  return k;
}"""


def _mark_top_shelves(page, limit):
    """제목이 정확히 'Shorts'인 일반 선반을 DOM 순서로 최대 limit개 표시하고 개수를 돌려준다."""
    return page.evaluate(TOP_JS, [TOP_TEXT, TOP_MARK, limit])


def _top_shelf(page, i):
    return page.locator(f"[{TOP_MARK}='{i}']").first


def _shot_top(page, out_dir, log):
    """맨 위 일반 'Shorts' 선반(5개)을 썸네일용으로 찍는다. 없으면 건너뛴다(러너 레이아웃 대비)."""
    if not _mark_top_shelves(page, 1):
        page.evaluate("window.scrollBy(0, 2000)")
        page.wait_for_timeout(1200)
    if not _mark_top_shelves(page, 1):
        log.append("상단 Shorts 선반 없음: 썸네일은 채널 선반 접힘본으로 대체")
        return False
    shelf = _top_shelf(page, 1)
    shelf.scroll_into_view_if_needed()
    page.wait_for_timeout(1200)                 # 썸네일 지연 로딩
    _shot_shelf(page, shelf, out_dir / TOP_FILE)
    log.append(f"{TOP_FILE} OK")
    return True


def _stitch(paths, out):
    """같은 폭의 이미지를 세로로 이어 붙여 저장한다. (폭, 높이) 반환."""
    imgs = [Image.open(p).convert("RGB") for p in paths]
    canvas = Image.new("RGB", (max(i.width for i in imgs), sum(i.height for i in imgs)), "white")
    y = 0
    for i in imgs:
        canvas.paste(i, (0, y))
        y += i.height
    canvas.save(out, quality=JPEG_Q, subsampling=0)
    return canvas.size


def _capture_top_sets(page, out_dir, log):
    """채널 선반이 없을 때: 일반 'Shorts' 선반 TOP_SETS개(5개×4세트)를 각각 찍어 세로 1장으로 만든다."""
    k = _mark_top_shelves(page, TOP_SETS)
    for _ in range(6):
        if k >= TOP_SETS:
            break
        page.evaluate("window.scrollBy(0, 2500)")
        page.wait_for_timeout(900)
        k = _mark_top_shelves(page, TOP_SETS)
    if not k:
        raise CaptureFailed("채널 Shorts 선반 없음", "일반 Shorts 선반도 없음")
    if k < TOP_SETS:
        log.append(f"WARN: 일반 Shorts 선반 {k}세트만 발견 (목표 {TOP_SETS})")
    records, parts = [], []
    for i in range(1, k + 1):
        shelf = _top_shelf(page, i)
        shelf.scroll_into_view_if_needed()
        page.wait_for_timeout(1000)
        part = out_dir / f"03_세트{i:02d}.jpg"
        _shot_shelf(page, shelf, part)
        parts.append(part)
        records += shelf.evaluate(ITEMS_JS)
    if not (out_dir / TOP_FILE).exists():      # 썸네일을 못 찍었으면 1세트로 대신한다
        Image.open(parts[0]).save(out_dir / TOP_FILE, quality=JPEG_Q, subsampling=0)
    size = _stitch(parts, out_dir / "03_선반_전체.jpg")
    log.append(f"일반 Shorts 선반 {k}세트 캡처 → 03_선반_전체.jpg {size}")
    return _parse_items(records)


def _find_shelf(page, log, max_scroll=45):
    """바닥까지 훑어 내리며 선반을 찾는다. 못 찾으면 진단값을 로그에 남긴다."""
    last_state = None
    bottom = False
    for i in range(max_scroll):
        if _mark_shelf(page, log):
            shelf = page.locator(SHELF_MARK).first
            shelf.scroll_into_view_if_needed()
            page.wait_for_timeout(800)
            log.append(f"선반 발견(스크롤 {i}회)")
            return shelf
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(900)
        state = page.evaluate("""() => [document.documentElement.scrollHeight,
          document.querySelectorAll('ytd-video-renderer, yt-lockup-view-model').length]""")
        if state == last_state:             # 높이와 결과 수가 안정된 뒤에도 로딩 여유를 준다
            page.wait_for_timeout(3000)     # 느린 러너에서 이어지는 로딩을 '바닥'으로 오판하지 않게
            stable = page.evaluate("""() => [document.documentElement.scrollHeight,
              document.querySelectorAll('ytd-video-renderer, yt-lockup-view-model').length]""")
            if stable == state:
                bottom = page.evaluate("""() => window.scrollY + window.innerHeight >=
                  document.documentElement.scrollHeight - 10""")
                break
        last_state = state
    # 마지막 기다림 중 도착한 제목도 검사한다.
    if _mark_shelf(page, log):
        return page.locator(SHELF_MARK).first
    diag = page.evaluate("""() => ({
      height: document.documentElement.scrollHeight,
      results: document.querySelectorAll('ytd-video-renderer, yt-lockup-view-model').length,
      shorts: document.querySelectorAll('ytm-shorts-lockup-view-model').length,
      hasText: document.body.innerText.includes('최신 Shorts 동영상'),
      shelves: document.querySelectorAll('grid-shelf-view-model').length,
    })""")
    diag["bottom"] = bottom
    log.append(f"진단: {diag}")
    if diag["hasText"]:
        log.append("일치 선반 제목: 본문 텍스트 존재, 컨테이너 연결 실패")
    if bottom and diag["results"] >= 100 and not diag["hasText"]:
        raise CaptureFailed("채널 Shorts 선반 없음", "이 검색어에는 채널 Shorts 선반이 없음")
    raise CaptureFailed("레이아웃/로딩 문제", f"선반 탐색 실패: {diag}")


def _refresh_shelf(page, log):
    """더보기/이미지 로딩 중 교체된 요소를 제목에서 다시 찾는다."""
    if _mark_shelf(page, log):
        return page.locator(SHELF_MARK).first
    try:
        return _find_shelf(page, log, max_scroll=3)
    except CaptureFailed as error:
        # 이미 발견했던 선반이 사라진 것은 정상적인 '없음'이 아니다.
        raise CaptureFailed("레이아웃/로딩 문제", f"선반 재탐색 실패: {error.detail}") from error


def _expanded(shelf, before):
    return (shelf.locator(CARD_SEL).count() > before or
            shelf.locator("button").filter(has_text=re.compile("간략히|Show less", re.I))
            .first.is_visible())


def _expand_shelf(page, shelf, log):
    before = shelf.locator(CARD_SEL).count()
    more = shelf.locator("button").filter(has_text=re.compile("더보기|Show more", re.I)).first
    if not more.is_visible():
        log.append("더보기 버튼 없음: 현재 표시된 카드 수 확인")
        return shelf, before, False
    more.click(timeout=10000)
    for _ in range(12):
        page.wait_for_timeout(500)
        if _mark_shelf(page, log):
            shelf = page.locator(SHELF_MARK).first
            if _expanded(shelf, before):
                log.append(f"더보기 확인: {before} → {shelf.locator(CARD_SEL).count()}개")
                return shelf, before, True
    raise CaptureFailed("레이아웃/로딩 문제", "더보기 후 선반 또는 펼침 상태를 확인하지 못함")


def _load_all_thumbs(page, shelf):
    """펼친 선반 썸네일이 전부 뜨도록 훑어 내렸다 올라온다."""
    box = shelf.bounding_box()
    if not box:
        raise CaptureFailed("레이아웃/로딩 문제", "선반 크기를 읽을 수 없음")
    shelf.evaluate("e => e.scrollIntoView({block: 'start', behavior: 'instant'})")
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
    """고정 헤더를 가리고 요소만 캡처한다. 긴 검색 페이지 전체는 렌더링하지 않는다."""
    style = page.add_style_tag(content="ytd-masthead,#masthead-container{visibility:hidden !important}")
    try:
        shelf.evaluate("e => e.scrollIntoView({block: 'start', behavior: 'instant'})")
        shelf.screenshot(path=path, type="jpeg", quality=JPEG_Q, timeout=15000)
    finally:
        style.evaluate("e => e.remove()")


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


def _capture_channel(page, out_dir, log):
    """채널 '…최신 Shorts 동영상' 선반: 접힘 캡처 → 더보기 → 전체 캡처. (제목, 항목, 펼침여부) 반환."""
    shelf = _find_shelf(page, log)
    shelf = _refresh_shelf(page, log)
    title = shelf.get_attribute("data-hobis-title")
    log.append("shelf_title=" + title)
    _shot_shelf(page, shelf, out_dir / "02_선반_접힘.jpg")
    log.append("02_선반_접힘.jpg OK")

    shelf = _refresh_shelf(page, log)
    shelf, before, clicked = _expand_shelf(page, shelf, log)
    _load_all_thumbs(page, shelf)
    shelf = _refresh_shelf(page, log)
    if shelf.get_attribute("data-hobis-title") != title:
        raise CaptureFailed("레이아웃/로딩 문제", "펼치기 전후 선택된 선반 제목이 달라짐")
    expanded = _expanded(shelf, before)
    if clicked and not expanded:
        raise CaptureFailed("레이아웃/로딩 문제", "이미지 로딩 후 펼침 상태가 사라짐")
    items = _items(shelf)
    log.append(f"펼침확인(카드증가/간략히버튼)={'YES' if expanded else 'NO'}")
    _shot_shelf(page, shelf, out_dir / "03_선반_전체.jpg")
    return title, items, expanded


def _attempt(query, out_dir, log, stamp, attempt):
    """한 번의 새 브라우저 세션. 실패 종류와 화면을 남기고 항상 닫는다."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--lang=ko-KR"])
        page = None
        try:
            ctx = browser.new_context(
                locale="ko-KR", timezone_id="Asia/Seoul", viewport=VIEWPORT,
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
            )
            ctx.add_cookies([{"name": "PREF", "value": "hl=ko&gl=KR",
                              "domain": ".youtube.com", "path": "/"}])
            page = ctx.new_page()
            url = ("https://www.youtube.com/results?search_query=" + quote_plus(query)
                   + "&persist_hl=1&hl=ko&persist_gl=1&gl=KR")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            block = _interstitial(page)
            if block:
                log.append(f"차단 감지: {block} · {page.url}")
                if block != "동의 화면" or not _dismiss_consent(page):
                    raise CaptureFailed("봇체크/동의화면", block)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(
                "ytd-item-section-renderer, ytd-video-renderer, yt-lockup-view-model, "
                "grid-shelf-view-model", state="attached", timeout=30000)
            page.wait_for_timeout(3000)
            block = _interstitial(page)
            if block:
                raise CaptureFailed("봇체크/동의화면", block)

            page.screenshot(path=out_dir / "01_검색상단.jpg", type="jpeg", quality=JPEG_Q)
            log.append("01_검색상단.jpg OK")

            has_top = _shot_top(page, out_dir, log)
            try:
                title, items, expanded = _capture_channel(page, out_dir, log)
                shelf_missing = False
            except CaptureFailed as error:
                if error.reason != "채널 Shorts 선반 없음":
                    raise
                log.append(f"채널 선반 없음 → 일반 '{TOP_TEXT}' 선반 5개×{TOP_SETS}세트로 대체")
                items = _capture_top_sets(page, out_dir, log)
                title, expanded, shelf_missing = TOP_TEXT, True, True
            thumb = TOP_FILE if has_top or shelf_missing else "02_선반_접힘.jpg"
            n = len(items)
            missing = sum(1 for it in items if not it["영상ID"] or not it["제목"])
            if not n or missing > n * 0.3:
                raise CaptureFailed("레이아웃/로딩 문제",
                                    f"쇼츠 카드가 비었거나 제목/영상ID 추출 실패 ({missing}/{n})")
            if missing:
                log.append(f"WARN: 제목/영상ID 빈 카드 {missing}/{n}개 (기록은 진행)")
            log.append(f"shorts_count={n}")
            full = out_dir / "03_선반_전체.jpg"
            with Image.open(full) as img:
                w, h = img.size
            log.append(f"03_선반_전체.jpg OK ({w}x{h})")
            log.append(f"분할 {len(_slice(full, out_dir, '03_선반'))}장")
        except Exception as error:
            block = None
            if page is not None:
                try:
                    block = _interstitial(page)
                    if block:
                        log.append(f"차단 감지: {block} · {page.url}")
                    page.screenshot(path=out_dir / f"99_실패화면_{attempt}.jpg",
                                    type="jpeg", quality=JPEG_Q, timeout=10000)
                except Exception as diagnostic_error:
                    log.append(f"실패화면 진단 불가: {diagnostic_error}")
            if block:
                raise CaptureFailed("봇체크/동의화면", f"{block}: {error}") from error
            if isinstance(error, PlaywrightTimeoutError):
                raise CaptureFailed("레이아웃/로딩 문제", str(error)) from error
            raise
        finally:
            browser.close()

    (out_dir / "items.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    return {"dir": out_dir, "stamp": stamp, "query": query, "title": title,
            "count": n, "expanded": expanded, "items": items,
            "thumb": thumb, "shelf_missing": shelf_missing}


def capture(query, out_root, tries=3):
    """실패 종류와 관계없이 최대 3개의 새 세션을 시도하고 구조화된 예외를 반환한다."""
    tries = max(1, min(3, tries))
    stamp = datetime.now(KST).strftime("%y%m%d_%H%M")
    out_dir = Path(out_root) / f"{re.sub(r'[^0-9A-Za-z가-힣]+', '', query)}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = [f"query={query}", f"captured_at={datetime.now(KST).isoformat()}"]

    failures = []
    try:
        for attempt in range(1, tries + 1):
            log.append(f"시도 {attempt}/{tries}: 새 브라우저 세션")
            try:
                return _attempt(query, out_dir, log, stamp, attempt)
            except CaptureFailed as error:
                failures.append(error)
            except Exception as error:
                failures.append(CaptureFailed("기타 예외", f"{type(error).__name__}: {error}"))
            log.append(f"시도 {attempt} 결과: {failures[-1]}")
            if attempt < tries:
                delay = random.uniform(1.5, 3.5)
                log.append(f"재시도 대기 {delay:.1f}초")
                time.sleep(delay)
        # 제목을 한 번이라도 봤다면 '없음'으로 덮지 않는다.
        saw_title = any(line.startswith("일치 선반 제목") for line in log)
        if saw_title:
            failure = next((e for e in reversed(failures) if e.reason != "채널 Shorts 선반 없음"),
                           CaptureFailed("레이아웃/로딩 문제", "세션별 제목 표시 불일치"))
        else:
            failure = next((e for e in failures if e.reason == "채널 Shorts 선반 없음"), failures[-1])
        log.append(f"최종 결과: {failure}")
        raise CaptureFailed(failure.reason, "\n".join(str(e) for e in failures))
    finally:
        (out_dir / "meta.txt").write_text("\n".join(log), encoding="utf-8", newline="\n")
        print("\n".join(log))
        print(f"\n→ {out_dir}")


if __name__ == "__main__":
    try:
        capture(sys.argv[1] if len(sys.argv) > 1 else "배달의 민족",
                sys.argv[2] if len(sys.argv) > 2 else "out")
    except CaptureFailed as error:
        print(error, file=sys.stderr)
        sys.exit(1)
