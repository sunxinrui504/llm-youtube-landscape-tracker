# config/settings.py
import os

# ──────────────────────────────────────────────
# 追蹤的熱門 LLM YouTube 頻道清單
# 每個頻道格式：{"url": "...", "channel_id": "UC..."}  (channel_id 可空，首次運行後自動補全)
# ──────────────────────────────────────────────
TARGET_CHANNELS = [
    {"url": "https://www.youtube.com/@AndrejKarpathy",   "channel_id": "UCbXgNpp0jedKWcQiULLbDTA"},
    {"url": "https://www.youtube.com/@MatthewBerman",    "channel_id": "UCVR_6lPIpBsSAGi2M5WkxUg"},
    {"url": "https://www.youtube.com/@YannicKilcher",    "channel_id": "UCZHmQk67mSJgfCCTn7xBfew"},
    {"url": "https://www.youtube.com/@TwoMinutePapers",  "channel_id": "UCbmNph6atAoGfqLoCL_duAg"},
    {"url": "https://www.youtube.com/@aiexplained-official", "channel_id": "UCNJ1Ymd5yFuUPtn21xtRbbw"},
    {"url": "https://www.youtube.com/@samwitteveenai",   "channel_id": "UCyIe-61Y8C4_o-zZCtO4ETQ"},
    {"url": "https://www.youtube.com/@WolframResearch",  "channel_id": "UCJekgf6k62CQHdENznr-p2A"},
]

# ──────────────────────────────────────────────
# 憑證配置（優先從環境變量讀取，適配 GitHub Actions）
# ──────────────────────────────────────────────
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")

# ──────────────────────────────────────────────
# 持久化文件路徑
# ──────────────────────────────────────────────
DB_FILE_PATH     = "processed_videos.json"
OUTPUT_DATA_PATH = "data.json"
QUOTA_STATE_PATH = "quota_state.json"   # Quota 計數器持久化

# ──────────────────────────────────────────────
# 採集限流與降級配置
# ──────────────────────────────────────────────
MAX_RETRIES        = 3       # 單影片 yt-dlp 最大重試次數
YTDLP_MAX_WORKERS  = 2       # 最大並發線程數
COOL_DOWN_MIN      = 2       # 請求間隨機冷卻最小秒數
COOL_DOWN_MAX      = 5       # 請求間隨機冷卻最大秒數
GLOBAL_FAIL_THRESH = 5       # 連續 N 支影片失敗後觸發全局降級

# ──────────────────────────────────────────────
# YouTube API Quota 防禦配置
# ──────────────────────────────────────────────
QUOTA_DAILY_LIMIT  = 10000   # YouTube API 每日額度上限
QUOTA_WARN_AT      = 8000    # 達到此點數時寫入告警日誌並暫停 API 調用
