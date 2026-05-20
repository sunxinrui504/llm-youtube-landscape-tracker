# core/ingestion.py 核心採集
import time
import random
import re
import requests
from typing import Dict, Any, Optional, List
import yt_dlp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import MAX_RETRIES, YOUTUBE_API_KEY, COOL_DOWN_MIN, COOL_DOWN_MAX
from utils.logger import setup_logger

logger = setup_logger("Ingestion")

class YtdlpBrokenException(Exception):
    """自定義異常：當 yt-dlp 連續失敗達到臨界值時拋出"""
    pass

class IngestionDispatcher:
    def __init__(self):
        self.api_client = None
        if YOUTUBE_API_KEY:
            self.api_client = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        else:
            logger.warning("未配置 YOUTUBE_API_KEY，降級斷路器激活時將無法調用 API v3！")

    def is_shorts(self, duration: Optional[int], webpage_url: Optional[str]) -> bool:
        """
        雙重攔截機制：精準過濾 Shorts 短影片
        1. 檢查網址路由特徵 2. 檢查時長硬防禦
        """
        if webpage_url and "/shorts/" in webpage_url:
            return True
        if duration and duration <= 60:
            return True
        return False

    def fetch_metadata_via_ytdlp(self, video_url: str) -> Dict[str, Any]:
        """
        核心引擎：使用 yt-dlp 抓取影片元數據與字幕狀態
        """
        ydl_opts = {
            'extract_flat': False,
            'skip_download': True,  # 僅抓取元數據，不下載影片
            'writesubtitles': True,
            'writeautomaticsub': True,
        }
        
        retries = 0
        while retries < MAX_RETRIES:
            try:
                # 隨機冷卻控流，防止 429 Anti-scraping 限流
                sleep_time = random.uniform(COOL_DOWN_MIN, COOL_DOWN_MAX)
                time.sleep(sleep_time)
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=False)
                    return info
            except Exception as e:
                retries += 1
                logger.warning(f"yt-dlp 抓取失敗 ({retries}/{MAX_RETRIES})，網址: {video_url}。錯誤原因: {e}")
                
        raise YtdlpBrokenException(f"yt-dlp 連續失敗 {MAX_RETRIES} 次，觸發降級斷路器。")

    def fetch_metadata_via_api(self, video_id: str) -> Dict[str, Any]:
        """
        輔助/備用手段：當斷路器觸發時，調用 YouTube Data API v3 補齊元數據
        """
        if not self.api_client:
            raise RuntimeError("API 備用路由已觸發，但未配置有效 YOUTUBE_API_KEY。")
        
        try:
            # 獲取影片基礎數據與狀態
            video_response = self.api_client.videos().list(
                part="snippet,contentDetails,statistics,status",
                id=video_id
            ).execute()
            
            if not video_response.get("items"):
                return {"status_error": "Video not found or private"}
                
            return video_response["items"][0]
        except HttpError as e:
            logger.error(f"YouTube API v3 請求失敗: {e}")
            return {"status_error": str(e)}

    def get_channel_videos_ytdlp(self, channel_url: str) -> List[str]:
        """
        快速獲取頻道最新影片列表的 URL 集合
        """
        ydl_opts = {
            'extract_flat': 'in_playlist',
            'playlistend': 10,  # 每次動態增量監控最新的 10 支影片即可，確保效率
        }
        video_urls = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                playlist_info = ydl.extract_info(channel_url, download=False)
                if 'entries' in playlist_info:
                    for entry in playlist_info['entries']:
                        if entry:
                            video_urls.append(f"https://www.youtube.com/watch?v={entry['id']}")
        except Exception as e:
            logger.error(f"獲取頻道影片列表失敗 {channel_url}: {e}")
        return video_urls
    
    def convert_channel_url_to_playlist_id(self, channel_url: str) -> str:
        """
        將頻道網址轉換為隱藏的 Uploads Playlist ID (UU 碼)
        防禦核心：調用 playlistItems 僅消耗 1 點 Quota
        """
        # 支援多種網址格式: /channel/UC..., /@username 等
        # 此處展示核心轉換邏輯：拿到頻道唯一的 UC 碼後，將前兩個字母替換為 UU
        if "UC" in channel_url:
            match = re.search(r"(UC[a-zA-Z0-9_-]{22})", channel_url)
            if match:
                channel_id = match.group(1)
                return "UU" + channel_id[2:]
        
        # 如果是 @ 暱稱網址，實際生產環境中需先請求一次 api 獲取 channel_id (耗費1點)
        # 為了展示防禦防線，我們假設配置中已規範或通過此處轉換
        return channel_url 

    def get_latest_videos_via_api(self, channel_url: str, max_results: int = 10) -> List[str]:
        """【Quota 防禦核心】透過 1 點 Quota 的播放清單接口獲取最新影片列表"""
        if not self.api_client:
            logger.warning("未配置 API Key，無法啟用 Quota 防禦掃描。")
            return []

        playlist_id = self.convert_channel_url_to_playlist_id(channel_url)
        # 若轉換失敗（@暱稱格式），playlist_id 仍是 channel_url，無法使用，直接返回空
        if not playlist_id.startswith("UU"):
            logger.warning(f"無法從 {channel_url} 提取 UU 播放清單 ID，跳過 API 掃描。")
            return []

        try:
            response = self.api_client.playlistItems().list(
                part="snippet",
                playlistId=playlist_id,
                maxResults=max_results
            ).execute()
            video_urls = []
            for item in response.get("items", []):
                v_id = item["snippet"]["resourceId"]["videoId"]
                video_urls.append(f"https://www.youtube.com/watch?v={v_id}")
            return video_urls
        except HttpError as e:
            logger.error(f"無法透過 API 獲取影片列表: {e}")
        return []