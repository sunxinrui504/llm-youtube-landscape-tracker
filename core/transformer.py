# core/transformer.py 數據標準化轉換
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from utils.logger import setup_logger

logger = setup_logger("Transformer")


def _today_utc() -> str:
    """返回 UTC 當日日期（YYYY-MM-DD），取代已弃用的 datetime.utcnow()。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

class DataTransformer:
    @staticmethod
    def parse_iso_duration(duration_str: str) -> int:
        """將 YouTube API v3 返回的 ISO 8601 時長（如 PT1M20S）轉換為秒數"""
        import isodate
        try:
            return int(isodate.parse_duration(duration_str).total_seconds())
        except Exception:
            return 0

    def transform(self, raw_data: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
        """
        核心洗數據邏輯：將來自 yt-dlp 或 API v3 的異質結構清洗為前端 React 統一消費的 JSON 對象
        """
        try:
            if source == "yt_dlp":
                # 提取字幕可用性狀態（yt-dlp 在無字幕時可能回傳 None，故統一兜底為 dict）
                subtitles = raw_data.get("subtitles") or {}
                automatic_captions = raw_data.get("automatic_captions") or {}
                
                has_manual = "en" in subtitles or any(k.startswith("en-") for k in subtitles)
                has_auto = "en" in automatic_captions or any(k.startswith("en-") for k in automatic_captions)
                
                # 標準化發布時間 (yt-dlp 給出的是 'YYYYMMDD')
                upload_date_raw = raw_data.get("upload_date") or ""
                if len(upload_date_raw) == 8:
                    published_at = f"{upload_date_raw[:4]}-{upload_date_raw[4:6]}-{upload_date_raw[6:]}"
                else:
                    published_at = _today_utc()

                return {
                    "video_id": raw_data.get("id"),
                    "url": raw_data.get("webpage_url"),
                    "title": raw_data.get("title"),
                    "channel": raw_data.get("uploader"),
                    "published_at": published_at,
                    "duration_seconds": int(raw_data.get("duration") or 0),
                    "metrics": {
                        "views": int(raw_data.get("view_count") or 0),
                        "likes": int(raw_data.get("like_count") or 0),
                        "comments": int(raw_data.get("comment_count") or 0)
                    },
                    "metadata": {
                        "description": raw_data.get("description") or "",
                        "tags": raw_data.get("tags") or []
                    },
                    "processing_info": {
                        "source_engine": "yt_dlp",
                        "has_manual_sub": has_manual,
                        "has_auto_sub": has_auto
                    },
                    "chapters": [
                        {"title": ch.get("title"), "start_time": int(ch.get("start_time") or 0)}
                        for ch in (raw_data.get("chapters") or [])
                    ]
                }

            elif source == "api_v3":
                snippet = raw_data.get("snippet") or {}
                content_details = raw_data.get("contentDetails") or {}
                statistics = raw_data.get("statistics") or {}
                
                # 標準化發布時間 (API v3 給出的是 ISO 格式 '2026-05-20T00:00:00Z')
                published_at_raw = snippet.get("publishedAt") or ""
                published_at = published_at_raw[:10] if published_at_raw else _today_utc()
                
                duration_seconds = self.parse_iso_duration(content_details.get("duration") or "")

                return {
                    "video_id": raw_data.get("id"),
                    "url": f"https://www.youtube.com/watch?v={raw_data.get('id')}",
                    "title": snippet.get("title"),
                    "channel": snippet.get("channelTitle"),
                    "published_at": published_at,
                    "duration_seconds": duration_seconds,
                    "metrics": {
                        "views": int(statistics.get("viewCount") or 0),
                        "likes": int(statistics.get("likeCount") or 0),
                        "comments": int(statistics.get("commentCount") or 0)
                    },
                    "metadata": {
                        "description": snippet.get("description") or "",
                        "tags": snippet.get("tags") or []
                    },
                    "processing_info": {
                        "source_engine": "api_v3",
                        "has_manual_sub": False, # API 無法獲取字幕列表細節，默認走保底或後續處理
                        "has_auto_sub": False
                    },
                    "chapters": []  # API v3 默認不提供 Timeline 章節數據
                }

        except Exception as e:
            logger.error(f"異質元數據轉換失敗，Source: {source}。錯誤細節: {e}")
            return None