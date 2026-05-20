import json
import re
import time
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from config.settings import MOONSHOT_API_KEY, MOONSHOT_MAP_MODEL, MOONSHOT_REDUCE_MODEL
from utils.logger import setup_logger

logger = setup_logger('MapReduceEngine')
MOONSHOT_URL = 'https://api.moonshot.cn/v1/chat/completions'

# Moonshot body 體積上限寬裕，保留安全網截斷
MAX_USER_CONTENT_CHARS = 28000
# Reduce 階段每段 map 結果最多保留的字元數，避免拼接超限
MAX_MAP_SNIPPET_CHARS  = 4000

# ── Moonshot 速率參數（根據實際帳號配額） ──
MOONSHOT_CONCURRENCY = 3     # 帳號並行上限（3 個請求同時在空中）
MOONSHOT_RPM         = 20    # 每分鐘請求上限
# 安全 429 重試（正常不應觸發，僅作安全網）
LLM_MAX_RETRIES = 2
LLM_RETRY_BASE  = 3

MAP_SCHEMA = (
    '{"partial_script":[{"speaker":"x","timestamp":"MM:SS","text":"x"}],'
    ' "partial_keywords":["k"]}'
)
REDUCE_SCHEMA = (
    '{"speaker_type":"Solo","speakers":["N"],'
    ' "ai_topics":["T"],"summary":"x"}'
)


class MapReduceTextEngine:
    def __init__(self):
        self.api_key = MOONSHOT_API_KEY

    def _call_llm(self, model: str, system_prompt: str, user_content: str) -> str:
        """Moonshot API 單次調用，帶簡潔 429 安全網"""
        if not self.api_key:
            logger.error('MOONSHOT_API_KEY not configured.')
            return ''
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        if len(user_content) > MAX_USER_CONTENT_CHARS:
            logger.warning(
                f'user_content {len(user_content)} chars 超過 {MAX_USER_CONTENT_CHARS}，截斷後送出'
            )
            user_content = user_content[:MAX_USER_CONTENT_CHARS]
        payload = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user',   'content': user_content},
            ],
            'temperature': 0.1,
            'response_format': {'type': 'json_object'},
        }

        for attempt in range(1, LLM_MAX_RETRIES + 1):
            try:
                res = requests.post(MOONSHOT_URL, json=payload, headers=headers, timeout=60)
                if res.status_code == 429:
                    retry_after = res.headers.get('Retry-After')
                    wait = float(retry_after) if retry_after else LLM_RETRY_BASE * (2 ** (attempt - 1))
                    logger.warning(f'Moonshot 429（第 {attempt} 次），等待 {wait:.0f}s...')
                    time.sleep(wait)
                    continue
                res.raise_for_status()
                return res.json()['choices'][0]['message']['content']
            except requests.exceptions.HTTPError as e:
                logger.error(f'Moonshot HTTP 錯誤 model={model}: {e}')
                return ''
            except Exception as e:
                logger.error(f'Moonshot 調用失敗 model={model}: {e}')
                return ''

        logger.error(f'Moonshot 重試耗盡，放棄此次調用。')
        return ''

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 1200, overlap: int = 200) -> List[str]:
        segments = re.split(r'\[SEGMENT BREAK @\d{1,3}:\d{2}\]', text)
        chunks: List[str] = []
        buffer = ''
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            combined = (buffer + '\n' + seg).strip() if buffer else seg
            if len(combined.split()) <= chunk_size:
                buffer = combined
            else:
                if buffer:
                    chunks.append(buffer)
                words = seg.split()
                start = 0
                while start < len(words):
                    end = min(start + chunk_size, len(words))
                    chunks.append(' '.join(words[start:end]))
                    start += chunk_size - overlap
                buffer = ''
        if buffer:
            chunks.append(buffer)
        return chunks if chunks else [text]

    @staticmethod
    def _map_prompt() -> str:
        return (
            'You are an expert AI podcast transcript analyzer.\n'
            'Timestamps are [MM:SS]. [SEGMENT BREAK @MM:SS] = strong speaker-change boundary.\n\n'
            'Tasks:\n'
            '1. Register ALL names mentioned in context (e.g. Thanks David).\n'
            '2. Label each line with a speaker. Ambiguous -> [Unverified Speaker N]. Never blend speakers.\n'
            '3. Extract 3-5 LLM technical keywords.\n\n'
            'Output ONLY valid JSON: ' + MAP_SCHEMA
        )

    @staticmethod
    def _reduce_prompt(title: str) -> str:
        return (
            f'Master Editor. Video: {title}\n\n'
            '1. Resolve [Unverified Speaker N] across all segments.\n'
            '2. Deduplicate into 2-5 LLM topics.\n'
            '3. Classify: Solo / Interview / Group.\n'
            '4. Write 2-3 sentence summary.\n\n'
            'Output ONLY valid JSON: ' + REDUCE_SCHEMA
        )

    def run_pipeline(self, full_text: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        if not full_text.strip():
            return self._fallback('empty_text')
        chunks = self._chunk_text(full_text)
        logger.info(f'MapReduce: {len(chunks)} chunks, 並行度={MOONSHOT_CONCURRENCY}, title={meta.get("title", "")!r}')
        map_prompt = self._map_prompt()

        # ── Map 階段：並行 3 路發送，充分利用 Moonshot 並行配額 ──
        map_results: List[str] = [''] * len(chunks)  # 保持原始順序

        def _map_worker(idx: int, chunk: str) -> tuple:
            raw = self._call_llm(
                MOONSHOT_MAP_MODEL, map_prompt,
                f'Segment {idx+1}/{len(chunks)}:\n\n{chunk}'
            )
            return idx, raw

        with ThreadPoolExecutor(max_workers=MOONSHOT_CONCURRENCY) as pool:
            futures = {
                pool.submit(_map_worker, i, c): i
                for i, c in enumerate(chunks)
            }
            for future in as_completed(futures):
                idx, raw = future.result()
                if raw:
                    map_results[idx] = raw

        # 過濾空結果
        map_results = [r for r in map_results if r]
        if not map_results:
            return self._fallback('all_map_failed')

        # ── Reduce 階段：單次調用聚合 ──
        trimmed = [r[:MAX_MAP_SNIPPET_CHARS] for r in map_results]
        reduce_raw = self._call_llm(
            MOONSHOT_REDUCE_MODEL,
            self._reduce_prompt(meta.get('title', 'Unknown')),
            '\n===SEG===\n'.join(trimmed),
        )
        final = self._parse_json_safe(reduce_raw)
        if not final:
            return self._fallback('reduce_failed')
        script: List[Dict] = []
        seen: set = set()
        for r in map_results:
            d = self._parse_json_safe(r)
            for entry in d.get('partial_script', []):
                key = (entry.get('speaker'), entry.get('timestamp'), entry.get('text', '')[:60])
                if key not in seen:
                    seen.add(key)
                    script.append(entry)
        final['dialogue_script'] = script[:120]
        return final

    @staticmethod
    def _parse_json_safe(raw: str) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            clean = re.sub(r'```(?:json)?\s*|\s*```', '', raw).strip()
            return json.loads(clean)
        except (json.JSONDecodeError, ValueError):
            return {}

    @staticmethod
    def _fallback(reason: str = 'unknown') -> Dict[str, Any]:
        logger.warning(f'MapReduce fallback: {reason}')
        return {
            'speaker_type': 'Solo',
            'speakers': ['Speaker A'],
            'ai_topics': ['LLM'],
            'summary': f'Unavailable ({reason}).',
            'dialogue_script': [],
        }
