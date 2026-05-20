# tests/test_extract_video_id.py
"""
覆蓋 IngestionDispatcher._extract_video_id 的正則校驗行為：
    - 標準 watch URL → 取 v 參數
    - youtu.be 短鏈
    - /shorts/ URL
    - 帶 query 的 watch URL
    - 頻道 URL（24 字 UC 開頭）→ 拒絕，回傳空字串
    - 空字串 → 回傳空字串
"""
import pytest

from core.ingestion import IngestionDispatcher

extract = IngestionDispatcher._extract_video_id


@pytest.mark.parametrize("url, expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",          "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10",     "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ",                          "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ",           "dQw4w9WgXcQ"),
])
def test_extract_video_id_valid(url, expected):
    assert extract(url) == expected


@pytest.mark.parametrize("url", [
    "",
    None,
    "https://www.youtube.com/@AndrejKarpathy",
    "https://www.youtube.com/channel/UCXUPKJO5MZQN11PqgIvyuvQ",  # 24 字頻道 ID 不可冒充
    "https://www.youtube.com/watch?v=tooShort",
])
def test_extract_video_id_rejected(url):
    assert extract(url) == ""
