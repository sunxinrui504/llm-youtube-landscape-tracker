# core/transcription.py 核心轉錄與 AI 處理
import os
import re
import tempfile
from typing import Dict, Any, Optional, List
import yt_dlp
import requests

from config.settings import GROQ_API_KEY
from utils.logger import setup_logger

logger = setup_logger("Transcription")

class TranscriptionPipeline:
    def __init__(self):
        self.groq_key = GROQ_API_KEY
        if not self.groq_key:
            logger.warning("未檢測到 GROQ_API_KEY！保底音訊轉錄與 LLM 劇本分析將無法執行。")

    def clean_vtt_text(self, vtt_path: str) -> str:
        """清洗 WebVTT 字幕文件，去除時間戳和重疊重複的字幕行，拼裝成帶有粗略時間標記的純文字流"""
        if not os.path.exists(vtt_path):
            return ""
        
        with open(vtt_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        cleaned_chunks = []
        current_time = "00:00"
        seen_sentences = set()

        for line in lines:
            line = line.strip()
            # 匹配並提取時間戳 (例如 00:01:23.456 --> 00:01:25.120)
            time_match = re.match(r'(\d{2}):(\d{2}):(\d{2})[\.,]\d{3}', line)
            if time_match:
                current_time = f"{time_match.group(2)}:{time_match.group(3)}"
                continue
            
            # 過濾 VTT 標頭與空行
            if not line or "WEBVTT" in line or "Kind:" in line or "Language:" in line:
                continue
                
            # 去除 HTML 標籤（如 <c> 等內建樣式標籤）
            line = re.sub(r'<[^>]*>', '', line)
            
            # 去除字幕組件滾動產生的重複去重
            if line not in seen_sentences and len(line) > 1:
                cleaned_chunks.append(f"[{current_time}] {line}")
                seen_sentences.add(line)

        return "\n".join(cleaned_chunks)

    def download_subtitles_or_audio(self, video_url: str, video_id: str, info_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        三級策略實裝：
        優先級 1 & 2: 真實抓取並下載文字字幕（Manual/Auto VTT）
        優先級 3 (保底): 下載極低位元率 .m4a 音訊以便調用 Groq 雲端
        """
        has_manual = info_dict.get("processing_info", {}).get("has_manual_sub", False)
        has_auto = info_dict.get("processing_info", {}).get("has_auto_sub", False)
        
        temp_dir = tempfile.gettempdir()
        output_template = os.path.join(temp_dir, f"tracker_{video_id}.%(ext)s")

        # 優先級 1 & 2：下載字幕
        if has_manual or has_auto:
            ydl_opts = {
                'skip_download': True,
                'writesubtitles': has_manual,
                'writeautomaticsub': not has_manual,
                'subtitleslangs': ['en'],
                'outtmpl': output_template,
                'quiet': True
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([video_url])
                
                # 尋找下載下來的 .vtt 檔案
                vtt_file = os.path.join(temp_dir, f"tracker_{video_id}.en.vtt")
                if os.path.exists(vtt_file):
                    text_stream = self.clean_vtt_text(vtt_file)
                    os.remove(vtt_file) # 隨手清理臨時文件
                    return {"type": "text_stream", "data": text_stream}
            except Exception as e:
                logger.error(f"字幕下載失敗，降級至音頻抓取: {e}")

        # 優先級 3 (保底)：下載低音質音頻
        logger.info(f"影片 {video_id} 無可用字幕，啟動優先級 3 保底方案：下載音訊檔。")
        ydl_opts = {
            'format': 'm4a/bestaudio',
            'outtmpl': output_template,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                'preferredquality': '64', # 最低音質即可，極大節省 GitHub Actions 的流量和帶寬
            }],
            'quiet': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
            
        audio_file = os.path.join(temp_dir, f"tracker_{video_id}.m4a")
        return {"type": "audio_file", "data": audio_file}

    def transcribe_audio_via_groq(self, audio_path: str) -> str:
        """將音訊投遞給 Groq 免費高架構 Whisper API，5秒內輸出帶有時間戳的文本"""
        if not os.path.exists(audio_path):
            return ""
        
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.groq_key}"}
        
        try:
            with open(audio_path, "rb") as f:
                files = {
                    "file": (os.path.basename(audio_path), f, "audio/m4a"),
                    "model": (None, "whisper-large-v3"),
                    "response_format": (None, "verbose_json") # 獲取富文本結構，包含時間戳
                }
                response = requests.post(url, headers=headers, files=files, timeout=60)
                response.raise_for_status()
                
                # 格式化拼裝成與 VTT 相同的 [MM:SS] 文字流
                result = response.json()
                segments = result.get("segments", [])
                text_stream_chunks = []
                for seg in segments:
                    start_sec = int(seg.get("start", 0))
                    m, s = divmod(start_sec, 60)
                    timestamp = f"{m:02d}:{s:02d}"
                    text_stream_chunks.append(f"[{timestamp}] {seg.get('text', '').strip()}")
                
                return "\n".join(text_stream_chunks)
        except Exception as e:
            logger.error(f"Groq Whisper API 轉錄崩潰: {e}")
            return ""
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path) # 確保刪除大音訊文件，防止 Actions 磁碟爆滿

    def run_llm_script_restoration(self, text_stream: str, video_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        統一語意劇本還原工程：
        將混亂的文本流投餵給大模型（例如 Groq 託管的 Llama-3-70b 或類似支持 JSON 輸出的高效模型）
        """
        if not self.groq_key or not text_stream:
            return self._get_fallback_ai_structure()

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.groq_key}",
            "Content-Type": "application/json"
        }

        # 核心講者識別（Speaker Labeling）與結構化提煉 Prompt
        system_prompt = """你是一個頂級的 AI 技術播客與學術劇本編輯。
以下是一段從 YouTube LLM 頻道下載的原始對話文本（帶有時間戳標記），裡面混雜了發言，你需要完成角色還原與主題提煉。

請嚴格執行以下任務，並**完全以 JSON 格式返回數據**，不要包含任何 Markdown 標籤或常規文本：

1. 識別講者身份：
   - 透過說話邏輯（主持人引導提問短小；嘉賓輸出底層技術細節、代碼長篇大論）。
   - 捕捉「稱呼錨定」線索（例如："Thanks for coming, Andrej", "Hey Matthew, what do you think"）。
   - 如果確定了真實姓名，請使用真實姓名；如果完全無法推斷，請統一使用 "Speaker A", "Speaker B"。
2. 還原結構化劇本 (`dialogue_script`): 格式為數組，包含 speaker, timestamp, text。
3. 判定講者類型 (`speaker_type`): 必須是 "Solo"（單人演講）、"Interview"（專訪式對談）或 "Group"（多人討論）之一。
4. 提取技術核心主題 (`ai_topics`): 提取 2-4 個具體的 LLM 技術關鍵詞（如 "RAG", "Agentic Workflow", "Context Window Optimization"）。
5. 總結精華摘要 (`summary`): 2-3句高度濃縮的架構實質結論。

必須輸出的 JSON 格式規範如下：
{
  "speaker_type": "Solo / Interview / Group",
  "speakers": ["講者姓名或Speaker A"],
  "ai_topics": ["主題1", "主題2"],
  "summary": "技術摘要總結",
  "dialogue_script": [
    {"speaker": "名字", "timestamp": "MM:SS", "text": "話語内容"}
  ]
}"""

        user_content = f"""影片標題: {video_metadata.get('title')}
頻道名稱: {video_metadata.get('channel')}
影片簡介: {video_metadata.get('metadata', {}).get('description')[:500]}

原始轉錄文本：
{text_stream[:8000]} # 截斷防止超出 Actions 虛擬機或 API 單次 tokens 限制
"""

        payload = {
            "model": "llama3-70b-8192", # 使用具備強大結構化推理能力的大型開源模型
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "response_format": {"type": "json_object"}, # 強制開啟 JSON 模式，防止格式解析碎裂
            "temperature": 0.2
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=45)
            response.raise_for_status()
            import json
            return json.loads(response.json()["choices"][0]["message"]["content"])
        except Exception as e:
            logger.error(f"LLM 統一語意還原調用失敗: {e}")
            return self._get_fallback_ai_structure()

    def _get_fallback_ai_structure(self) -> Dict[str, Any]:
        """容錯保底機制：若大模型崩潰，返回標準骨架防止前端 React Map 報錯"""
        return {
            "speaker_type": "Solo",
            "speakers": ["Speaker A"],
            "ai_topics": ["LLM"],
            "summary": "轉錄摘要暫不可用",
            "dialogue_script": []
        }