# config/settings.py
import os

# 追蹤的熱門 LLM YouTube 頻道列表 (示例)
TARGET_CHANNELS = [
    "https://www.youtube.com/@AndrejKarpathy",
    "https://www.youtube.com/@MatthewBerman",
    "https://www.youtube.com/@YannicKilcher"
]

# 憑證配置（優先從環境變量讀取，適配 GitHub Actions）
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# 增量更新數據庫路徑
DB_FILE_PATH = "processed_videos.json"
OUTPUT_DATA_PATH = "data.json"

# 採集限流與降級配置
MAX_RETRIES = 3
YTDLP_MAX_WORKERS = 2
COOL_DOWN_MIN = 2
COOL_DOWN_MAX = 5