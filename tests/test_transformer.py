# tests/test_transformer.py
"""
覆蓋 DataTransformer.transform 對兩種數據源的標準化能力：
    - yt_dlp：upload_date 'YYYYMMDD' → 'YYYY-MM-DD'，chapters 為 None 不爆 NoneType
    - api_v3 ：snippet.publishedAt ISO → 截前 10 位，duration ISO 8601 → 秒
    - 來源不認識：回傳 None（不拋例外）
"""
from core.transformer import DataTransformer


def test_transform_ytdlp_basic():
    """yt-dlp 標準路線：欄位齊全且 chapters 為 None 不會炸。"""
    raw = {
        "id": "abc12345678",
        "webpage_url": "https://www.youtube.com/watch?v=abc12345678",
        "title": "Demo",
        "uploader": "Sample Channel",
        "upload_date": "20260520",
        "duration": 600,
        "view_count": 1000,
        "like_count": 50,
        "comment_count": 5,
        "description": "test desc",
        "tags": ["llm"],
        "subtitles": {"en": [{}]},
        "automatic_captions": {},
        "chapters": None,  # 之前的 NoneType 崩潰場景，必須兜底
    }
    result = DataTransformer().transform(raw, source="yt_dlp")
    assert result is not None
    assert result["video_id"] == "abc12345678"
    assert result["published_at"] == "2026-05-20"
    assert result["duration_seconds"] == 600
    assert result["metrics"]["views"] == 1000
    assert result["chapters"] == []   # None 必須轉成 []
    assert result["processing_info"]["has_manual_sub"] is True


def test_transform_api_v3_basic():
    """API v3 路線：ISO duration → 秒，publishedAt 截前 10 位。"""
    raw = {
        "id": "xyz98765432",
        "snippet": {
            "title": "Api Demo",
            "channelTitle": "Api Channel",
            "publishedAt": "2026-05-20T08:30:00Z",
            "description": "",
            "tags": None,  # tags 為 None 不能炸
        },
        "contentDetails": {"duration": "PT1M30S"},
        "statistics": {"viewCount": "42"},
    }
    result = DataTransformer().transform(raw, source="api_v3")
    assert result is not None
    assert result["published_at"] == "2026-05-20"
    assert result["duration_seconds"] == 90
    assert result["metadata"]["tags"] == []
    assert result["url"].endswith("xyz98765432")


def test_transform_unknown_source_returns_none():
    """未知 source 應返回 None 而不是拋例外。"""
    assert DataTransformer().transform({}, source="mystery") is None
