# gc_cleanup.py 為了徹底清理死連結，這段腳本每週日單獨運行。它使用 videos.list 批量接口（傳入 50 個 ID 僅扣 1 點 Quota），高效核對數據庫。如果發現影片已被刪除或設為私享，會將其從數據庫中抹除，並自動重新計算其餘存活影片的全量標籤矩陣，確保前端絕對沒有死引用。
import requests
from datetime import datetime, timezone

from config.settings import DB_FILE_PATH, OUTPUT_DATA_PATH, YOUTUBE_API_KEY
from utils.logger import setup_logger
from utils.io_helpers import load_json, save_json
from core.graph_matrix import GraphMatrixAnalyzer

logger = setup_logger("Garbage_Collector")


def chunk_list(lst, n):
    """將 list 切成每段 n 個的生成器。"""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def run_garbage_collection():
    logger.info("🧼 啟動每週定期異步死鏈清理機制 (GC)...")
    if not YOUTUBE_API_KEY:
        logger.error("缺少 API key，GC 取消。")
        return

    processed_db = load_json(DB_FILE_PATH, dict)
    # 与 main.py 保持同一份 schema，避免 io_helpers 回退 {} 後后續讀取 "videos" 重示鍍誤
    output_payload = load_json(
        OUTPUT_DATA_PATH,
        lambda: {"last_updated": "", "themes_matrix": {}, "videos": []},
    )
    
    if "videos" not in output_payload or not output_payload["videos"]:
        logger.info("數據庫為空，無需清理。")
        return

    all_videos = output_payload["videos"]
    video_ids = [v["video_id"] for v in all_videos]
    
    active_ids = set()
    
    # 50 個 ID 一組進行批量包裝，每組僅花費 1 點 Quota
    for chunk in chunk_list(video_ids, 50):
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "id,status",
            "id": ",".join(chunk),
            "key": YOUTUBE_API_KEY
        }
        try:
            res = requests.get(url, params=params, timeout=15)
            if res.status_code == 200:
                items = res.json().get("items", [])
                for item in items:
                    v_id = item["id"]
                    privacy = item["status"]["privacyStatus"]
                    # 只有公開影片才被允許保留在 React 前端
                    if privacy == "public":
                        active_ids.add(v_id)
        except Exception as e:
            logger.error(f"批量核對時發生崩潰: {e}")
            return # 安全起見，退出不損害數據庫

    # 找出被刪除或私享的死鏈影片
    dead_ids = set(video_ids) - active_ids
    
    if dead_ids:
        logger.warning(f"🚨 檢測到 {len(dead_ids)} 個失效死鏈影片: {dead_ids}，啟動數據剔除與矩陣重建。")
        
        # 1. 剔除影片池
        survived_videos = [v for v in all_videos if v["video_id"] not in dead_ids]
        
        # 2. 剔除增量狀態庫，允許未來萬一重新公開時能被再次抓取
        for d_id in dead_ids:
            if d_id in processed_db:
                del processed_db[d_id]
                
        # 3. 🛠️ 核心亮點：自動重新計算其餘存活影片的雙層關係矩陣，避免 React 前端死引用
        analyzer = GraphMatrixAnalyzer()
        new_themes_matrix = analyzer.generate_relations(survived_videos)
        
        output_payload["last_updated"] = datetime.now(timezone.utc).isoformat()
        output_payload["themes_matrix"] = new_themes_matrix
        output_payload["videos"] = survived_videos
        
        # 持久化推回 Git
        save_json(DB_FILE_PATH, processed_db)
        save_json(OUTPUT_DATA_PATH, output_payload)
        logger.info("✅ 數據清洗完畢，全量關係矩陣重組完成，前端數據已保持 100% 純淨。")
    else:
        logger.info("盤點完畢！所有歷史追蹤影片均健康存活 (200 OK)。")

if __name__ == "__main__":
    run_garbage_collection()