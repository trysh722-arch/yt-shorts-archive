"""python test_offline.py — 자격증명/네트워크/브라우저 없는 순수 함수 검사."""
import sys
import tempfile
from pathlib import Path

from PIL import Image

sys.dont_write_bytecode = True

import capture
import run
import sheet


def main():
    for text, expected in [
        ("조회수 1.2억회", 120000000), ("조회수 9.4만회", 94000),
        ("조회수 2.5천회", 2500), ("조회수 123회", 123),
        ("조회수 1,234회", 1234), ("0회", 0),
        (None, None), ("", None), ("조회수 없음", None),
    ]:
        assert capture._views_to_int(text) == expected, text

    items = capture._parse_items([
        {"txt": "새 동영상\n 도시락 소개 \n조회수 9.4만회", "href": "/shorts/abc-123_X?feature=share"},
        {"txt": "조회수 올리는 방법\n조회수 1,234회\n새 동영상",
         "href": "https://www.youtube.com/shorts/xyz789#details"},
        {"txt": "세 번째\n조회수 없음", "href": "/watch?v=watch123&feature=share"},
        {"txt": None, "href": None},
    ])
    assert items[0] == {"순번": 1, "제목": "도시락 소개", "조회수": 94000,
                        "조회수원문": "조회수 9.4만회", "영상ID": "abc-123_X", "새동영상": "Y"}
    assert items[1] == {"순번": 2, "제목": "조회수 올리는 방법", "조회수": 1234,
                        "조회수원문": "조회수 1,234회", "영상ID": "xyz789", "새동영상": "Y"}
    assert items[2] == {"순번": 3, "제목": "세 번째", "조회수": None,
                        "조회수원문": "조회수 없음", "영상ID": "watch123", "새동영상": ""}
    assert items[3] == {"순번": 4, "제목": "", "조회수": None,
                        "조회수원문": "", "영상ID": "", "새동영상": ""}
    assert capture._parse_items([]) == []

    assert sheet.tab_name(" 배민 ") == "배민"
    assert sheet.tab_name("배민", "_목록") == "배민_목록"
    assert sheet.tab_name("a:b/c?d*e[f]g\\h") == "a b c d e f g h"
    assert sheet.tab_name("가" * 120) == "가" * 95
    assert sheet.tab_name("가" * 120, "_목록") == "가" * 92 + "_목록"
    for reserved in ("", "  ", "_키워드", "배민_목록", "배민_목록 ", ".", ".."):
        try:
            sheet.tab_name(reserved)
        except ValueError:
            pass
        else:
            raise AssertionError(f"예약 탭명 허용: {reserved!r}")

    url = run.raw_url(Path("archive") / "배민 & +#%" / "사진 1.jpg",
                      repo="owner/repo", branch="feature/한글")
    assert url == ("https://raw.githubusercontent.com/owner/repo/feature%2F%ED%95%9C%EA%B8%80/"
                   "archive/%EB%B0%B0%EB%AF%BC%20%26%20%2B%23%25/"
                   "%EC%82%AC%EC%A7%84%201.jpg"), url

    for rng, expected in [("'배민'!A7:F7", 7), ("'탭!A99'!A12:H51", 12),
                          ("배민!AA40:AH40", 40), ("'탭'!$A$8:$F$8", 8),
                          ("A3", 3), ("A0:F0", None), ("잘못된 범위", None)]:
        assert sheet._row_of({"updates": {"updatedRange": rng}}) == expected, rng
    for response in (None, {}, {"updates": {}}, {"updates": {"updatedRange": None}}):
        assert sheet._row_of(response) is None
    assert sheet.remark(True, "") == "선반 이미지 없음"
    assert sheet.remark(True, "선반 이미지 없음") == "선반 이미지 없음"
    assert sheet.remark(False, "선반 이미지 없음") == "선반이미지 추가"
    assert sheet.remark(False, " 선반 이미지 없음 ") == "선반이미지 추가"
    assert sheet.remark(False, "") == ""
    assert sheet.remark(False, "선반이미지 추가") == ""
    assert sheet.RUN_HEADER[-1] == "비고" and len(sheet.RUN_HEADER) == 7

    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, h in enumerate((30, 50)):
            p = Path(tmp) / f"{i}.jpg"
            Image.new("RGB", (100, h), "red").save(p)
            parts.append(p)
        out = Path(tmp) / "all.jpg"
        assert capture._stitch(parts, out) == (100, 80)
        assert Image.open(out).size == (100, 80)

    print("오프라인 테스트 통과 (조회수·항목 파싱·탭 이름·URL·행 번호·비고·이어붙이기)")


if __name__ == "__main__":
    main()
