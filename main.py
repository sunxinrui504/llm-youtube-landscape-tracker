# main.py  —  LLM YouTube Landscape Tracker 主編排器
import json
import os
from datetime import datetime
from typing import Dict, Any

from config.settings import TARGET_CHANNELS, DB_FILE_PATH, OUTPUT_DATA_PATH
from utils.logger import setup_logger
from core.quota_guard import QuotaGuard
from core.ingestion import IngestionDispatcher
from core.transformer import DataTransformer
from core.transcription import TranscriptionPipeline
from core.map_reduce_engine import MapReduceTextEngine
from core.graph_matrix import GraphMatrixAnalyzer

logger = setup_logger("Main_Orchestrator")


def load_json(path: str, default_factory) -> Any:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"讀取 {path} 失敗: {e}")
    return default_factory()


def save_json(path: str, data: Any):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"寫入 {path} 失敗: {e}")


def main():
    logger.info("=" * 54)
    logger.info("  LLM YouTube Landscape Tracker Pipeline 啟動")
    logger.info("=" * 54)

    # ── 初始化模組 ───────────────────────────────────────────────
    quota       = QuotaGuard()
    dispatcher  = IngestionDispatcher(quota_guard=quota)
    transformer = DataTransformer()
    transcriber = TranscriptionPipeline()
    mr_engine   = MapReduceTextEngine()
    analyzer    = GraphMatrixAnalyzer()

    # ── 載入狀態 ─────────────────────────────────────────────────
    processed_db: Dict[str, Any] = load_json(DB_FILE_PATH, dict)
    output_payload: Dict[str, Any] = load_json(
        OUTPUT_DATA_PATH,
        lambda: {"last_updated": "", "themes_matrix": {}, "videos": []}
    )
    current_videos_pool = output_payload.get("videos", [])
    new_count = 0

    # ── 掃描每個頻道 ─────────────────────────────────────────────
    for channel_info in TARGET_CHANNELS:
        channel_url = channel_info["url"]
        logger.info(f"掃描頻道: {channel_url}")

        video_urls = dispatcher.get_channel_videos(channel_info, max_results=10)
        if not video_urls:
            logger.warning(f"無法獲取頻道影片列表，跳過: {channel_url}")
            continue

        for url in video_urls:
            video_id = IngestionDispatcher._extract_video_id(url)

            # 增量檢查
            if video_id in processed_db:
                logger.info(f"[跳過] 已處理: {video_id}")
                continue

            logger.info(f"[新影片] 開始處理: {video_id}")

            # 1. 採集元數據（兩層異常樹已封裝在 dispatcher.fetch_metadata）
            raw_data, source_engine = dispatcher.fetch_metadata(url)

            if not raw_data or "status_error" in raw_data:
                logger.error(f"元數據獲取失敗，跳過: {video_id}")
                processed_db[video_id] = {
                    "title": "",
                    "processed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "fetch_failed",
                }
                continue

            # 2. 數據標準化
            standard_meta = transformer.transform(raw_data, source=source_engine)
            if not standard_meta:
                continue

            # Shorts 攔截
            if dispatcher.is_shorts(
                standard_meta.get("duration_seconds"),
                standard_meta.get("url")
            ):
                logger.info(f"[Shorts 攔截] {video_id}")
                processed_db[video_id] = {
                    "title": standard_meta["title"],
                    "processed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "filtered_shorts",
                }
                continue

            # 3. 字幕 / 音訊轉錄
            text_result = transcriber.get_text_stream(url, video_id, standard_meta)
            text_stream = text_result["text"]
            standard_meta["processing_info"]["transcription_source"] = text_result["source"]

            # 4. Map-Reduce AI 分析
            ai_insights = mr_engine.run_pipeline(text_stream, standard_meta)

            standard_meta.update({
                "speaker_type":    ai_insights.get("speaker_type", "Solo"),
                "speakers":        ai_insights.get("speakers",     ["Unknown"]),
                "ai_topics":       ai_insights.get("ai_topics",    ["LLM"]),
                "summary":         ai_insights.get("summary",      ""),
                "dialogue_script": ai_insights.get("dialogue_script", []),
            })
            standard_meta["processing_info"]["processed_at"] = (
                datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            )

            # 寫入影片池
            current_videos_pool.append(standard_meta)
            processed_db[video_id] = {
                "title":        standard_meta["title"],
                "processed_at": standard_meta["processing_info"]["processed_at"],
                "status":       "ok",
            }
            new_count += 1
            logger.info(f"[完成] {standard_meta['title'][:60]}")

    # ── 重建主題矩陣 ─────────────────────────────────────────────
    if new_count > 0 or not output_payload.get("themes_matrix"):
        logger.info(f"重建全域主題矩陣（新增 {new_count} 支影片）...")
        themes_matrix = analyzer.generate_relations(current_videos_pool)
        output_payload = {
            "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "themes_matrix": themes_matrix,
            "videos": current_videos_pool,
        }
        save_json(DB_FILE_PATH,     processed_db)
        save_json(OUTPUT_DATA_PATH, output_payload)
        logger.info(f"Pipeline 完成，共處理 {new_count} 支新影片。Quota 今日已用 {quota.used} 點。")
    else:
        logger.info("沒有新影片，全域矩陣保持最新狀態。")


if __name__ == "__main__":
    main()
