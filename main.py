# 最终版 main.py
import json
import os
from datetime import datetime
from typing import Dict, Any

from config.settings import TARGET_CHANNELS, DB_FILE_PATH, OUTPUT_DATA_PATH
from utils.logger import setup_logger
from core.ingestion import IngestionDispatcher, YtdlpBrokenException
from core.transformer import DataTransformer
from core.transcription import TranscriptionPipeline
from core.map_reduce_engine import MapReduceTextEngine
from core.graph_matrix import GraphMatrixAnalyzer

logger = setup_logger("Main_Orchestrator")

def load_json_file(file_path: str, default_factory) -> Any:
    """加载JSON文件并处理异常"""
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取文件失败 {file_path}: {e}")
    return default_factory()

def save_json_file(file_path: str, data: Any):
    """保存JSON文件并处理异常"""
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"写入文件失败 {file_path}: {e}")

def main():
    logger.info("==============================================")
    logger.info("🚀 LLM YouTube Landscape Tracker Pipeline 启动")
    logger.info("==============================================")

    # 实例化核心模块
    dispatcher = IngestionDispatcher()
    transformer = DataTransformer()
    transcription_pipe = TranscriptionPipeline()
    mr_engine = MapReduceTextEngine()  # 新增MapReduce引擎
    analyzer = GraphMatrixAnalyzer()

    # 加载状态数据库
    processed_db: Dict[str, Any] = load_json_file(DB_FILE_PATH, dict)
    output_payload: Dict[str, Any] = load_json_file(OUTPUT_DATA_PATH, lambda: {"last_updated": "", "themes_matrix": {}, "videos": []})
    
    current_videos_pool = output_payload.get("videos", [])
    new_processed_count = 0

    # 处理目标频道
    for channel_url in TARGET_CHANNELS:
        logger.info(f"正在扫描频道: {channel_url}")
        
        # 优化视频获取逻辑（先API后降级）
        video_urls = dispatcher.get_latest_videos_via_api(channel_url, max_results=5)
        if not video_urls:
            video_urls = dispatcher.get_channel_videos_ytdlp(channel_url)
        
        for url in video_urls:
            video_id = url.split("v=")[-1] if "v=" in url else url.split("/")[-1]
            
            # 增量状态检查
            if video_id in processed_db:
                logger.info(f"➔ [1秒跳过] 影片 ID: {video_id} 已在数据库中，跳过重复处理")
                continue

            logger.info(f"➔ 发现新影片！启动处理管线, ID: {video_id}")
            
            # 1. 采集元数据（带断路器机制）
            raw_data = None
            source_engine = "yt_dlp"
            try:
                raw_data = dispatcher.fetch_metadata_via_ytdlp(url)
            except YtdlpBrokenException as e:
                logger.warning(f"断路器激活！切换至API v3。ID: {video_id}. 原因: {e}")
                raw_data = dispatcher.fetch_metadata_via_api(video_id)
                source_engine = "api_v3"
            except Exception as e:
                logger.error(f"非预期采集崩溃，跳过影片: {video_id}. 错误: {e}")
                continue

            if not raw_data or "status_error" in raw_data:
                logger.error(f"无法获取有效元数据，跳过。ID: {video_id}")
                continue

            # 2. 数据转换标准化
            standard_meta = transformer.transform(raw_data, source=source_engine)
            if not standard_meta:
                continue

            # Shorts视频拦截
            if dispatcher.is_shorts(standard_meta.get("duration_seconds"), standard_meta.get("url")):
                logger.info(f"➔ [Shorts 拦截] 检测到短视频，不写入技术矩阵库。ID: {video_id}")
                processed_db[video_id] = {
                    "title": standard_meta["title"],
                    "processed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "filtered_shorts"
                }
                continue

            # 3. 字幕处理
            media_payload = transcription_pipe.download_subtitles_or_audio(url, video_id, standard_meta)
            text_stream = ""
            
            if media_payload["type"] == "text_stream":
                text_stream = media_payload["data"]
                standard_meta["processing_info"]["transcription_source"] = "youtube_captions"
            elif media_payload["type"] == "audio_file":
                text_stream = transcription_pipe.transcribe_audio_via_groq(media_payload["data"])
                standard_meta["processing_info"]["transcription_source"] = "groq_whisper_cloud"

            # 4. 使用MapReduce引擎处理文本
            ai_insights = mr_engine.run_pipeline(text_stream, standard_meta)
            
            # 整合处理结果
            standard_meta.update({
                "speaker_type": ai_insights.get("speaker_type", "Panel Discussion"),
                "speakers": ai_insights.get("speakers", ["Unknown"]),
                "ai_topics": ai_insights.get("ai_topics", ["General"]),
                "summary": ai_insights.get("summary", ""),
                "dialogue_script": ai_insights.get("dialogue_script", []),
                "processing_info": {
                    **standard_meta["processing_info"],
                    "processed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                }
            })

            # 添加到视频池并更新数据库
            current_videos_pool.append(standard_meta)
            processed_db[video_id] = {
                "title": standard_meta["title"],
                "processed_at": standard_meta["processing_info"]["processed_at"],
                "status": "successfully_processed"
            }
            new_processed_count += 1

    # 5. 重新计算主题矩阵
    if new_processed_count > 0 or not output_payload.get("themes_matrix"):
        logger.info("发现数据更新，正在重建跨创作者主题矩阵与关联链...")
        themes_matrix = analyzer.generate_relations(current_videos_pool)
        
        # 组装输出数据
        output_payload = {
            "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "themes_matrix": themes_matrix,
            "videos": current_videos_pool
        }
        
        # 持久化保存
        save_json_file(DB_FILE_PATH, processed_db)
        save_json_file(OUTPUT_DATA_PATH, output_payload)
        logger.info(f"✅ 管线运行成功！处理新影片: {new_processed_count} 支。")
    else:
        logger.info("没有检测到新影片，全域矩阵保持最新状态。")

if __name__ == "__main__":
    main()
