"""아카이브 구글시트 읽기/쓰기. 서비스계정(gspread) 사용.

시트 구조
  _키워드      : A=키워드 B=사용(Y/N) C=마지막실행 D=마지막결과   ← 사용자가 여기에 키워드 추가
  <키워드>      : 회차마다 1행 (캡처시각·선반제목·Shorts수·썸네일·이미지링크·비고)
  <키워드>_목록 : 회차마다 Shorts 개수만큼 (제목·조회수·영상ID)
"""
import re

import gspread

KEYWORD_TAB = "_키워드"
KEYWORD_HEADER = ["키워드", "사용", "마지막실행", "마지막결과"]
RUN_HEADER = ["캡처시각", "선반제목", "Shorts수", "썸네일(상단 Shorts 5개)", "선반 전체 이미지",
              "검색 상단 이미지", "비고"]
NOTE_MISSING = "선반 이미지 없음"     # 채널 선반이 없어 일반 Shorts 4세트로 대체한 회차
NOTE_ADDED = "선반이미지 추가"        # 직전 회차가 '없음'이었는데 이번에 채널 선반이 생긴 회차
THUMB_W = 320          # 시트에 박히는 썸네일 가로 픽셀
ITEM_HEADER = ["캡처시각", "순번", "제목", "조회수", "조회수원문", "영상ID", "새동영상", "링크"]

# 구글시트 탭 이름에 못 쓰는 문자
_BAD_TAB = re.compile(r"[:\\/?*\[\]]")


def tab_name(keyword, suffix=""):
    """'_목록'은 항목 탭 전용. 해당 접미사의 키워드는 다른 키워드의 탭과 충돌해 거부한다.

    금지문자 치환/긴 이름 절단으로 같아지는 키워드는 사용자가 서로 다르게 이름 붙여야 한다.
    """
    base = _BAD_TAB.sub(" ", keyword).strip()
    if not base or base in (KEYWORD_TAB, ".", "..") or base.endswith("_목록"):
        raise ValueError("키워드는 비어 있거나 예약 이름(_키워드, *_목록, ., ..)일 수 없습니다")
    if len(suffix) >= 95:
        raise ValueError("탭 접미사가 너무 깁니다")
    return base[:95 - len(suffix)] + suffix  # 긴 키워드에서도 '_목록'을 보존한다.


def open_sheet(sheet_id, sa_path="service_account.json"):
    return gspread.service_account(filename=sa_path).open_by_key(sheet_id)


def _ensure_tab(sh, name, header):
    """탭 없으면 만들고 헤더를 넣는다. 있으면 헤더가 달라졌을 때만(열 추가) 1행을 고친다."""
    try:
        ws = sh.worksheet(name)
        if ws.row_values(1) != header:
            ws.update(range_name="A1", values=[header])
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=max(len(header), 8))
        ws.update(range_name="A1", values=[header])
        ws.freeze(rows=1)
        if header is RUN_HEADER:      # 썸네일 열을 넓혀둔다
            ws.spreadsheet.batch_update({"requests": [{"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": 3, "endIndex": 4},
                "properties": {"pixelSize": THUMB_W + 20}, "fields": "pixelSize"}}]})
    return ws


def keywords(sh):
    """_키워드 탭에서 사용중인 키워드 목록. 탭이 없으면 만들어서 안내행을 넣는다."""
    try:
        ws = sh.worksheet(KEYWORD_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=KEYWORD_TAB, rows=200, cols=4)
        ws.update(range_name="A1", values=[KEYWORD_HEADER,
                                           ["배달의 민족", "Y", "", ""]])
        ws.freeze(rows=1)
        return [("배달의 민족", 2)]

    rows = ws.get_all_values()
    out = []
    for i, r in enumerate(rows[1:], start=2):          # 1행은 헤더
        kw = (r[0] if r else "").strip()
        use = (r[1] if len(r) > 1 else "").strip().upper()
        if kw and use != "N":                           # 빈칸이면 사용으로 본다
            out.append((kw, i))
    return out


def mark_keyword(sh, row, when, result):
    """_키워드 탭의 해당 행에 마지막 실행 시각·결과를 남긴다."""
    sh.worksheet(KEYWORD_TAB).update(range_name=f"C{row}:D{row}",
                                     values=[[when, result]])


def _row_of(append_response):
    """append_row 응답의 updatedRange('탭'!A7:F7)에서 행 번호만 뽑는다."""
    try:
        rng = append_response["updates"]["updatedRange"]
        match = re.fullmatch(r"\$?[A-Z]+\$?([1-9]\d*)(?::\$?[A-Z]+\$?[1-9]\d*)?", rng.rsplit("!", 1)[-1])
        return int(match.group(1)) if match else None
    except (KeyError, TypeError, AttributeError):
        return None


def remark(shelf_missing, prev_note):
    """비고 열 값. 없음 회차는 '없음', 직전이 '없음'이었다가 선반이 생기면 '추가', 그 외 빈칸."""
    if shelf_missing:
        return NOTE_MISSING
    return NOTE_ADDED if (prev_note or "").strip() == NOTE_MISSING else ""


def _last_note(ws):
    """마지막 회차 행의 비고(G열). 데이터 행이 없으면 빈칸."""
    rows = ws.get_all_values()
    last = rows[-1] if len(rows) > 1 else []
    return last[len(RUN_HEADER) - 1] if len(last) >= len(RUN_HEADER) else ""


def append_run(sh, keyword, when, title, count, thumb_url, thumb_h, full_url, top_url,
               images_uploaded=True, shelf_missing=False):
    """<키워드> 탭에 회차 1행 추가. 썸네일이 안 잘리게 행 높이도 맞춘다."""
    ws = _ensure_tab(sh, tab_name(keyword), RUN_HEADER)
    note = remark(shelf_missing, _last_note(ws))
    resp = ws.append_row(
        [when, title, count,
         f'=IMAGE("{thumb_url}", 4, {thumb_h}, {THUMB_W})' if images_uploaded else "이미지 업로드 실패",
         f'=HYPERLINK("{full_url}","선반 전체")' if images_uploaded else "이미지 업로드 실패",
         f'=HYPERLINK("{top_url}","검색 상단")' if images_uploaded else "이미지 업로드 실패",
         note],
        value_input_option="USER_ENTERED")
    row = _row_of(resp)
    if row:
        ws.spreadsheet.batch_update({"requests": [{"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "ROWS",
                      "startIndex": row - 1, "endIndex": row},
            "properties": {"pixelSize": thumb_h + 10}, "fields": "pixelSize"}}]})
    return ws


def append_items(sh, keyword, when, items):
    """<키워드>_목록 탭에 전부 추가. 40행도 append_rows 한 요청이며 분할할 필요가 없다."""
    if not items:
        return None
    ws = _ensure_tab(sh, tab_name(keyword, "_목록"), ITEM_HEADER)
    rows = [[when, it["순번"], it["제목"], it["조회수"], it["조회수원문"],
             it["영상ID"], it["새동영상"],
             f'https://www.youtube.com/shorts/{it["영상ID"]}'] for it in items]
    ws.append_rows(rows, value_input_option="RAW")  # 제목이 '='로 시작해도 텍스트로 보존한다.
    return ws
