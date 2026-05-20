# config/settings.py
import os

# ──────────────────────────────────────────────
# 追蹤的熱門 LLM YouTube 頻道清單
# 每個頻道格式：{"url": "...", "channel_id": "UC..."}
#   - url：yt-dlp 拓取列表用的頻道主頁 URL（@handle 形式即可）
#   - channel_id：API v3 降級路線必需，為 'UC' 開頭的 24 字元字串，必填
# ──────────────────────────────────────────────
TARGET_CHANNELS = [
    {"url": "https://www.youtube.com/@AndrejKarpathy",   "channel_id": "UCXUPKJO5MZQN11PqgIvyuvQ"},
    {"url": "https://www.youtube.com/@3blue1brown",    "channel_id": "UCYO_jab_esuFRV4b17AJtAw"},
    {"url": "https://www.youtube.com/@YannicKilcher",    "channel_id": "UCZHmQk67mSJgfCCTn7xBfew"},
    {"url": "https://www.youtube.com/@TwoMinutePapers",  "channel_id": "UCbfYPyITQ-7l4upoX8nvctg"},
    {"url": "https://www.youtube.com/@aiexplained-official", "channel_id": "UCNJ1Ymd5yFuUPtn21xtRbbw"},
    {"url": "https://www.youtube.com/@samwitteveenai",   "channel_id": "UC55ODQSvARtgSyc8ThfiepQ"},
    {"url": "https://www.youtube.com/@IBMTechnology",   "channel_id": "UCKWaEZ-_VweaEx1j62do_vQ"},
]

# ──────────────────────────────────────────────
# 憑證配置（優先從環境變量讀取，適配 GitHub Actions）
# ──────────────────────────────────────────────
YOUTUBE_API_KEY  = os.getenv("YOUTUBE_API_KEY", "AIzaSyD2OXfF2zTNDrZV1SIXVo7b0y-OfrIX3RA")
MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY", "sk-LQp30ZEIRTQwDMeih7HTuXBTK3SCkbNhLEszIWqkoNMxGWFa")

# ──────────────────────────────
# Moonshot (Kimi) LLM 模型設定
#   Map 階段用 8k 模型（单 chunk 小上下文，快速）
#   Reduce 階段用 32k 模型（聚合多段 map 結果，需較大上下文）
# ──────────────────────────────
MOONSHOT_MAP_MODEL    = os.getenv("MOONSHOT_MAP_MODEL",    "moonshot-v1-8k")
MOONSHOT_REDUCE_MODEL = os.getenv("MOONSHOT_REDUCE_MODEL", "moonshot-v1-32k")

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
BACKOFF_BASE       = 2       # 指數退避基數（秒），實際等待 = base^attempt + jitter
BACKOFF_MAX        = 30      # 指數退避上限（秒），防止等太久
GLOBAL_FAIL_THRESH = 3       # 10 分鐘窗口內連續 N 支影片失敗後觸發全局降級
GLOBAL_FAIL_WINDOW = 600     # 全局降級時間窗口（秒）：超出此窗口的失敗不累計

# ──────────────────────────────────────────────
# YouTube API Quota 防禦配置
# ──────────────────────────────────────────────
QUOTA_DAILY_LIMIT  = 10000   # YouTube API 每日額度上限
QUOTA_WARN_AT      = 8000    # 達到此點數時寫入告警日誌並暫停 API 調用
