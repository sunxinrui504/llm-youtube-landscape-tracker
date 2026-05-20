# core/transcription.py
"""
轉錄 Pipeline（三級策略 + SEGMENT BREAK 硬邊界預處理）

優先級 1：YouTube 手動字幕（Manual Captions）—— 最快且免費，直接下載 .vtt 檔案
優先級 2：YouTube 自動字幕（Auto-generated Captions）—— 同樣快速免費
優先級 3（保底）：faster-whisper 本地 ASR
  ── 當字幕不存在或下載失敗時，用 yt-dlp 下載低位元率音訊，
  並調用 faster-whisper（完全本地免費）在 GitHub Actions 中運行轉錄

SEGMENT BREAK 預處理：
  在把字幕文本餵給 LLM 之前，對每段超過 30 秒間隔的連續字幕
  強制插入 [SEGMENT BREAK @MM:SS] 標記，給 LLM 提供硬邊界錨點，
  顯著提升多人口說場景下的說話人邊界識別精度。
"""

import os
import re
import tempfile
from typing import Dict, Any

import yt_dlp

from utils.logger import setup_logger

logger = setup_logger("Transcription")

# faster-whisper 是可選依賴；若未安裝則保底路線不可用
try:
    from faster_whisper import WhisperModel
    FASTER_WHISPER_AVAILABLE = True
except ImportError:
    FASTER_WHISPER_AVAILABLE = False
    logger.warning("faster-whisper 未安裝，保底音訊轉錄路線不可用。"
                   " 請執行 pip install faster-whisper")


class TranscriptionPipeline:
    def __init__(self):
        self._whisper_model = None   # 懶載入，避免啟動時佔用記憶體

    # ── 主入口 ───────────────────────────────────────────────────────────────

    def get_text_stream(
        self, video_url: str, video_id: str, standard_meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        回傳 {"text": str, "source": str}
        source 可能值：manual_caption / auto_caption / faster_whisper / unavailable
        """
        has_manual = standard_meta.get("processing_info", {}).get("has_manual_sub", False)
        has_auto   = standard_meta.get("processing_info", {}).get("has_auto_sub",   False)

        # 優先級 1 & 2：下載 YouTube 字幕（最快，僅下載幾 KB 的 .vtt，零 API 開銷）
        if has_manual or has_auto:
            result = self._download_subtitles(video_url, video_id, prefer_manual=has_manual)
            if result:
                source = "manual_caption" if has_manual else "auto_caption"
                preprocessed = self._inject_segment_breaks(result)
                return {"text": preprocessed, "source": source}

        # 優先級 3（保底）：faster-whisper 本地 ASR（當字幕不存在時）
        if FASTER_WHISPER_AVAILABLE:
            audio_path = self._download_audio(video_url, video_id)
            if audio_path:
                text = self._transcribe_with_faster_whisper(audio_path)
                if text:
                    preprocessed = self._inject_segment_breaks(text)
                    return {"text": preprocessed, "source": "faster_whisper"}

        logger.warning(f"影片 {video_id} 無法獲取任何文本。")
        return {"text": "", "source": "unavailable"}

    # ── 字幕下載 ─────────────────────────────────────────────────────────────

    def _download_subtitles(
        self, video_url: str, video_id: str, prefer_manual: bool
    ) -> str:
        temp_dir = tempfile.gettempdir()
        output_template = os.path.join(temp_dir, f"tracker_{video_id}.%(ext)s")

        ydl_opts = {
            "skip_download": True,
            "writesubtitles":    prefer_manual,
            "writeautomaticsub": not prefer_manual,
            "subtitleslangs":    ["en", "en-US"],
            "outtmpl":           output_template,
            "quiet":             True,
            "remote_components":  "ejs:github",
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
        except Exception as e:
            logger.error(f"字幕下載失敗 {video_id}: {e}")
            return ""

        # 尋找已下載的 .vtt 文件
        for suffix in [".en.vtt", ".en-US.vtt", ".vtt"]:
            vtt_path = os.path.join(temp_dir, f"tracker_{video_id}{suffix}")
            if os.path.exists(vtt_path):
                text = self._parse_vtt(vtt_path)
                try:
                    os.remove(vtt_path)
                except OSError:
                    pass
                return text

        logger.warning(f"找不到下載的 VTT 字幕文件，video_id={video_id}")
        return ""

    def _parse_vtt(self, vtt_path: str) -> str:
        """
        解析 WebVTT 字幕，輸出帶有 [MM:SS] 時間標記的純文字流。
        同時去除重複滾動行，保留句子邊界以供後續 SEGMENT BREAK 處理。
        """
        with open(vtt_path, "r", encoding="utf-8") as f:
            raw = f.read()

        lines       = raw.splitlines()
        chunks      = []
        current_sec = 0
        seen        = set()

        for line in lines:
            line = line.strip()

            # 提取時間戳（格式：HH:MM:SS.mmm 或 MM:SS.mmm）
            ts_match = re.match(
                r"(\d{1,2}):(\d{2}):(\d{2})[\.,]\d{3}\s*-->", line
            )
            if ts_match:
                h, m, s = int(ts_match.group(1)), int(ts_match.group(2)), int(ts_match.group(3))
                current_sec = h * 3600 + m * 60 + s
                continue

            # 過濾標頭與空行
            if not line or line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
                continue
            # 去除行內 HTML 標籤（<c>, <i>, <b> 等）
            line = re.sub(r"<[^>]+>", "", line)
            if not line:
                continue

            # 去重滾動行
            if line in seen:
                continue
            seen.add(line)

            m_fmt, s_fmt = divmod(current_sec, 60)
            chunks.append(f"[{m_fmt:02d}:{s_fmt:02d}] {line}")

        return "\n".join(chunks)

    # ── SEGMENT BREAK 硬邊界注入 ────────────────────────────────────────────

    @staticmethod
    def _inject_segment_breaks(text: str, gap_seconds: int = 30) -> str:
        """
        對帶有 [MM:SS] 標記的文字流，當相鄰兩行時間差超過 gap_seconds 時，
        強制插入 [SEGMENT BREAK @MM:SS]，為 LLM 提供說話人切換硬邊界。
        """
        lines = text.splitlines()
        result = []
        prev_sec = None

        ts_pattern = re.compile(r"^\[(\d{1,3}):(\d{2})\]")

        for line in lines:
            m = ts_pattern.match(line)
            if m:
                cur_sec = int(m.group(1)) * 60 + int(m.group(2))
                if prev_sec is not None and (cur_sec - prev_sec) >= gap_seconds:
                    mm, ss = divmod(cur_sec, 60)
                    result.append(f"\n[SEGMENT BREAK @{mm:02d}:{ss:02d}]\n")
                prev_sec = cur_sec
            result.append(line)

        return "\n".join(result)

    # ── 音訊下載 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _download_audio(video_url: str, video_id: str) -> str:
        """下載極低位元率音訊（64kbps m4a），節省頻寬與磁碟"""
        temp_dir      = tempfile.gettempdir()
        output_path   = os.path.join(temp_dir, f"tracker_{video_id}.m4a")
        output_tmpl   = os.path.join(temp_dir, f"tracker_{video_id}.%(ext)s")

        ydl_opts = {
            "format":        "worstaudio/worst",
            "outtmpl":       output_tmpl,
            "postprocessors": [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "m4a",
                "preferredquality": "64",
            }],
            "quiet": True,
            "remote_components": "ejs:github",
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
            if os.path.exists(output_path):
                return output_path
        except Exception as e:
            logger.error(f"音訊下載失敗 {video_id}: {e}")
        return ""

    # ── faster-whisper 本地 ASR ──────────────────────────────────────────────

    def _get_whisper_model(self) -> "WhisperModel":
        """懶載入 Whisper 模型（首次調用時初始化）"""
        if self._whisper_model is None:
            # 在 GitHub Actions 的無 GPU 環境使用 tiny 或 base 即可
            # 有 GPU 時可換成 large-v3
            self._whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
            logger.info("faster-whisper 模型已載入（base, CPU, int8 量化）")
        return self._whisper_model

    def _transcribe_with_faster_whisper(self, audio_path: str) -> str:
        """
        用 faster-whisper 本地轉錄音訊，輸出帶有 [MM:SS] 時間戳的文字流。
        轉錄完成後自動刪除音訊文件，防止 GitHub Actions 磁碟爆滿。
        """
        try:
            model    = self._get_whisper_model()
            segments, _ = model.transcribe(audio_path, beam_size=5, language="en")

            chunks = []
            for seg in segments:
                start_sec = int(seg.start)
                mm, ss    = divmod(start_sec, 60)
                chunks.append(f"[{mm:02d}:{ss:02d}] {seg.text.strip()}")

            return "\n".join(chunks)
        except Exception as e:
            logger.error(f"faster-whisper 轉錄失敗: {e}")
            return ""
        finally:
            if os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                    logger.info(f"已清理臨時音訊文件: {audio_path}")
                except OSError:
                    pass
