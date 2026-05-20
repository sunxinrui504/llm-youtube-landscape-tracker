import json
import re
from typing import List, Dict, Any
import requests
from config.settings import GROQ_API_KEY
from utils.logger import setup_logger

logger = setup_logger('MapReduceEngine')
GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'

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
        self.api_key = GROQ_API_KEY

    def _call_groq(self, model: str, system_prompt: str, user_content: str) -> str:
        if not self.api_key:
            logger.error('GROQ_API_KEY not configured.')
            return ''
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        payload = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user',   'content': user_content},
            ],
            'temperature': 0.1,
            'response_format': {'type': 'json_object'},
        }
        try:
            res = requests.post(GROQ_URL, json=payload, headers=headers, timeout=45)
            res.raise_for_status()
            return res.json()['choices'][0]['message']['content']
        except Exception as e:
            logger.error(f'Groq call failed model={model}: {e}')
            return ''

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 3000, overlap: int = 400) -> List[str]:
        segments = re.split(r'\[SEGMENT BREAK @\d{2}:\d{2}\]', text)
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
        logger.info(f'MapReduce: {len(chunks)} chunks, title={meta.get("title", "")!r}')
        map_prompt = self._map_prompt()
        map_results: List[str] = []
        for i, chunk in enumerate(chunks):
            raw = self._call_groq(
                'llama3-8b-8192', map_prompt,
                f'Segment {i+1}/{len(chunks)}:\n\n{chunk}'
            )
            if raw:
                map_results.append(raw)
        if not map_results:
            return self._fallback('all_map_failed')
        reduce_raw = self._call_groq(
            'llama3-70b-8192',
            self._reduce_prompt(meta.get('title', 'Unknown')),
            '\n===SEG===\n'.join(map_results),
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
