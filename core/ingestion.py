# core/ingestion.py
"""
採集調度器（Ingestion Dispatcher）

兩層異常樹設計：
  YtdlpVideoFailed   — 單支影片抓取失敗（重試 MAX_RETRIES 次後降級至 API）
  YtdlpGlobalBroken  — 連續 GLOBAL_FAIL_THRESH 支影片都觸發了 VideoFailed
                        → 整個 Pipeline 切換到 API 全局模式

採集優先級：
  1. yt-dlp 抓取頻道影片列表 + 單片元數據      （主路線）
  2. API playlistItems.list 獲取列表（1點）    （備用列表）
  3. API videos.list 獲取單片元數據（1點）     （VideoFailed 降級）
"""

import time
import random
import re
from typing import Dict, Any, Optional, List

import yt_dlp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import (
    MAX_RETRIES, YOUTUBE_API_KEY,
    COOL_DOWN_MIN, COOL_DOWN_MAX, GLOBAL_FAIL_THRESH,
)
from core.quota_guard import QuotaGuard
from utils.logger import setup_logger

logger = setup_logger("Ingestion")


# ── 自定義異常 ───────────────────────────────────────────────────────────────

class YtdlpVideoFailed(Exception):
    """單支影片經過 MAX_RETRIES 次重試後仍失敗"""


class YtdlpGlobalBroken(Exception):
    """連續 GLOBAL_FAIL_THRESH 支影片失敗，yt-dlp 可能被全局封鎖"""


# ── 調度器 ───────────────────────────────────────────────────────────────────

class IngestionDispatcher:
    def __init__(self, quota_guard: QuotaGuard):
        self.quota = quota_guard
        self._consecutive_failures = 0   # 全局連續失敗計數器
        self._global_mode = "ytdlp"       # "ytdlp" | "api_only"

        self.api_client = None
        if YOUTUBE_API_KEY:
            self.api_client = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        else:
            logger.warning("未配置 YOUTUBE_API_KEY，API 降級路線將不可用！")

    # ── Shorts 攔截 ──────────────────────────────────────────────────────────

    @staticmethod
    def is_shorts(duration_seconds: Optional[int], url: Optional[str]) -> bool:
        """雙重 Shorts 清洗攔截器"""
        if url and "/shorts/" in url:
            return True
        if duration_seconds and duration_seconds <= 60:
            return True
        return False

    # ── 頻道影片列表獲取 ─────────────────────────────────────────────────────

    def get_channel_videos(self, channel_info: Dict[str, str], max_results: int = 10) -> List[str]:
        """
        獲取頻道最新影片列表。
        先嘗試 yt-dlp（無限制，免費），失敗或全局 API 模式時切換到 playlistItems API。
        """
        channel_url = channel_info["url"]
        channel_id  = channel_info.get("channel_id", "")

        if self._global_mode == "ytdlp":
            urls = self._get_channel_videos_ytdlp(channel_url, max_results)
            if urls:
                return urls
            logger.warning(f"yt-dlp 無法獲取 {channel_url} 的列表，嘗試 API 備援。")

        # API 備援：playlistItems（1點/次）
        if channel_id:
            return self._get_channel_videos_api(channel_id, max_results)

        logger.error(f"無 channel_id 且 yt-dlp 失敗，跳過頻道: {channel_url}")
        return []

    def _get_channel_videos_ytdlp(self, channel_url: str, max_results: int) -> List[str]:
        ydl_opts = {
            "extract_flat": "in_playlist",
            "playlistend": max_results,
            "quiet": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
                if info and "entries" in info:
                    return [
                        f"https://www.youtube.com/watch?v={e['id']}"
                        for e in info["entries"] if e
                    ]
        except Exception as e:
            logger.warning(f"yt-dlp 頻道列表抓取失敗 {channel_url}: {e}")
        return []

    def _get_channel_videos_api(self, channel_id: str, max_results: int) -> List[str]:
        """用 playlistItems（UU碼）獲取列表，消耗 1 點 Quota"""
        if not self.api_client or not self.quota.can_call("playlistItems.list"):
            return []
        uploads_playlist_id = "UU" + channel_id[2:]
        try:
            resp = self.api_client.playlistItems().list(
                part="snippet",
                playlistId=uploads_playlist_id,
                maxResults=max_results,
            ).execute()
            self.quota.charge("playlistItems.list")
            return [
                f"https://www.youtube.com/watch?v={item['snippet']['resourceId']['videoId']}"
                for item in resp.get("items", [])
            ]
        except HttpError as e:
            logger.error(f"playlistItems API 失敗 (channel_id={channel_id}): {e}")
        return []

    # ── 單片元數據抓取 ───────────────────────────────────────────────────────

    def fetch_metadata(self, video_url: str) -> Dict[str, Any]:
        """
        主調度入口：根據全局模式決定路線。
        - ytdlp 模式：yt-dlp 抓取，VideoFailed 時降級 API，
                      連續失敗 GLOBAL_FAIL_THRESH 次後切換全局 API 模式。
        - api_only 模式：直接走 API。
        回傳 (raw_data, source_engine) 元組。
        """
        video_id = self._extract_video_id(video_url)

        if self._global_mode == "api_only":
            logger.info(f"[全局 API 模式] 直接走 API 獲取 {video_id}")
            return self._fetch_via_api(video_id), "api_v3"

        # yt-dlp 路線，帶重試機制
        try:
            raw = self._fetch_via_ytdlp_with_retry(video_url)
            self._consecutive_failures = 0   # 成功後重置計數器
            return raw, "yt_dlp"
        except YtdlpVideoFailed as e:
            self._consecutive_failures += 1
            logger.warning(
                f"YtdlpVideoFailed: {video_id}（連續失敗 {self._consecutive_failures} 支）。"
                f" 降級至 API 獲取此片元數據。原因: {e}"
            )

            # 檢查是否需要觸發全局降級
            if self._consecutive_failures >= GLOBAL_FAIL_THRESH:
                self._global_mode = "api_only"
                logger.error(
                    f"[YtdlpGlobalBroken] 連續 {self._consecutive_failures} 支影片失敗，"
                    " yt-dlp 疑似被全局封鎖。Pipeline 切換至 API 全局模式。"
                )

            return self._fetch_via_api(video_id), "api_v3"

    def _fetch_via_ytdlp_with_retry(self, video_url: str) -> Dict[str, Any]:
        """帶重試的 yt-dlp 單片抓取，連續失敗 MAX_RETRIES 次後拋出 YtdlpVideoFailed"""
        ydl_opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "quiet": True,
        }
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            sleep_sec = random.uniform(COOL_DOWN_MIN, COOL_DOWN_MAX)
            time.sleep(sleep_sec)
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=False)
                    if info:
                        return info
            except Exception as e:
                last_error = e
                logger.warning(
                    f"yt-dlp 抓取失敗 ({attempt}/{MAX_RETRIES}) URL={video_url}: {e}"
                )
        raise YtdlpVideoFailed(
            f"yt-dlp 連續失敗 {MAX_RETRIES} 次，URL={video_url}。最後錯誤: {last_error}"
        )

    def _fetch_via_api(self, video_id: str) -> Dict[str, Any]:
        """用 videos.list 獲取單片元數據，消耗 1 點 Quota"""
        if not self.api_client or not self.quota.can_call("videos.list"):
            return {"status_error": "API 不可用（未配置 key 或 Quota 已暫停）"}
        try:
            resp = self.api_client.videos().list(
                part="snippet,contentDetails,statistics,status",
                id=video_id,
            ).execute()
            self.quota.charge("videos.list")
            items = resp.get("items", [])
            if not items:
                return {"status_error": "video_not_found_or_private"}
            return items[0]
        except HttpError as e:
            logger.error(f"videos.list API 失敗 (id={video_id}): {e}")
            return {"status_error": str(e)}

    # ── 工具函數 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_video_id(url: str) -> str:
        if "v=" in url:
            return url.split("v=")[-1].split("&")[0]
        return url.rstrip("/").split("/")[-1]
