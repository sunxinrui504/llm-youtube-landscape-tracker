# core/map_reduce_engine.py 滑動窗口重疊 + 語意微調」切片，並驅動 Llama3-8b（Map 階段）與 Llama3-70b（Reduce 階段）
import re
from typing import List, Dict, Any
import requests
from config.settings import GROQ_API_KEY # 假設在 settings 中已配置

class MapReduceTextEngine:
    def __init__(self):
        self.api_key = GROQ_API_KEY
        self.api_url = "https://api.groq.com/openai/v1/chat/completions"

    def _call_groq(self, model: str, system_prompt: str, user_content: str) -> str:
        """統一調用免費高併發 Groq 雲端端點"""
        if not self.api_key:
            return "{" + "\"summary\": \"Missing API Key\"" + "}"
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.1 # 低隨機性，確保結構化輸出
        }
        try:
            res = requests.post(self.api_url, json=payload, headers=headers, timeout=30)
            if res.status_code == 200:
                return res.json()["choices"][0]["message"]["content"]
        except Exception as e:
            return f"Error: {str(e)}"
        return ""

    def chunk_text_with_overlap(self, text: str, chunk_size: int = 3000, overlap: int = 400) -> List[str]:
        """
        核心防禦：滑動窗口重疊法 (策略 A) + 標點符號微調 (策略 B)
        將長文字切片，保留重疊區緩衝上下文，防止邊界語意斷層
        """
        words = text.split()
        chunks = []
        start = 0
        
        while start < len(words):
            end = min(start + chunk_size, len(words))
            
            # 策略 B 微調：如果在結束點附近有明顯的句尾（如帶有句號的單詞），將邊界向後微調
            if end < len(words):
                for look_ahead in range(0, 50): # 向後看 50 個詞
                    if end + look_ahead < len(words):
                        word = words[end + look_ahead]
                        if any(p in word for p in [".", "。", "！", "!"]):
                            end += (look_ahead + 1)
                            break
            
            chunk_content = " ".join(words[start:end])
            chunks.append(chunk_content)
            
            # 滑動窗口：下一次的起點向前回退 overlap 的長度
            start += (chunk_size - overlap)
            if start >= len(words) or (end == len(words)):
                break
                
        return chunks

    def run_pipeline(self, full_text: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        """驅動完整的 Map-Reduce 數據流"""
        if not full_text.strip():
            return {"summary": "無內建字幕且無音訊可轉錄", "ai_topics": ["Unknown"], "dialogue_script": []}

        # 1. 切片階段 (複合防禦機制)
        chunks = self.chunk_text_with_overlap(full_text, chunk_size=3000, overlap=400)
        
        map_system_prompt = """
        You are an advanced STT Transcript Segment Analyzer. 
        Your job is to identify speakers and extract micro-topics for this 10-minute segment.
        
        [CRITICAL DEFENSE FOR MULTI-SPEAKER CONTEXT]:
        1. Register all distinct entities. If a speaker's name is inferred from context (e.g., "Thanks, David"), use it.
        2. If the context is ambiguous, strictly tag them as [Unverified Speaker 1], [Unverified Speaker 2]. DO NOT blend their lines into one person.
        3. Extract 3-5 precise technical keywords from this segment.
        
        Output format must be valid JSON matching this structure:
        {
          "partial_script": [{"speaker": "...", "text": "..."}],
          "partial_keywords": ["keyword1", "keyword2"]
        }
        """
        
        partial_results = []
        # Map 階段：調用速度快且免費的 Llama3-8b 處理各個切片
        for i, chunk in enumerate(chunks):
            user_content = f"Segment {i+1} Text:\n{chunk}"
            response_text = self._call_groq("llama3-8b-8192", map_system_prompt, user_content)
            partial_results.append(response_text)

        # 2. Reduce 階段：合流小摘要與標籤，餵給大模型 Llama3-70b 進行全局洗鍊
        reduce_system_prompt = f"""
        You are the Master Editor. You are given a series of structural segment analyses from a technology video titled "{meta.get('title')}".
        Your task is to merge these overlapping segments into a coherent, clean global profile.
        
        [CRITICAL INSTRUCTION]:
        1. Resolve [Unverified Speaker N] identities by tracking back technical viewpoints across segment timelines.
        2. Deduplicate the overlapping tags and synthesize a unified executive summary.
        
        Output strict JSON:
        {{
          "speaker_type": "Solo" or "Co-Host" or "Panel Discussion",
          "speakers": ["Speaker Name A", "Speaker Name B"],
          "ai_topics": ["Unified Keyword 1", "Unified Keyword 2"],
          "summary": "Comprehensive 3-sentence executive summary."
        }}
        """
        
        combined_map_payload = "\n===\n".join(partial_results)
        final_response_text = self._call_groq("llama3-70b-8192", reduce_system_prompt, combined_map_payload)
        
        # 解析並防禦性還原 JSON
        import json
        try:
            # 清洗 markdown 標記包裝
            clean_json_str = re.sub(r"```json\s*|\s*```", "", final_response_text).strip()
            final_data = json.loads(clean_json_str)
            
            # 同時保留並重組腳本
            global_script = []
            for res_str in partial_results:
                try:
                    c_json = json.loads(re.sub(r"```json\s*|\s*```", "", res_str).strip())
                    global_script.extend(c_json.get("partial_script", []))
                except: continue
            
            # 簡單的時間戳去重（此處可根據實際生產環境對連續重覆文本做單行清洗）
            final_data["dialogue_script"] = global_script[:100] # 前端只渲染核心精華劇本
            return final_data
        except Exception as e:
            # 降級防禦
            return {
                "speaker_type": "Panel Discussion",
                "speakers": ["Inferred Speaker"],
                "ai_topics": ["LLM", "Technology"],
                "summary": "Failed to parse final reduce JSON, falling back to safe placeholder metadata.",
                "dialogue_script": []
            }