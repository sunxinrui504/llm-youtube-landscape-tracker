# core/quota_guard.py
"""
YouTube API Quota 守衛
- 追蹤當日已消耗點數（持久化到 quota_state.json）
- 超過 QUOTA_WARN_AT 時暫停 API 調用並寫入告警
- 每個操作類型對應消耗點數：
    playlistItems.list  = 1 點
    videos.list         = 1 點
    channels.list       = 1 點
    search.list         = 100 點
"""
import json
import os
from datetime import date
from typing import Optional

from config.settings import QUOTA_STATE_PATH, QUOTA_DAILY_LIMIT, QUOTA_WARN_AT
from utils.logger import setup_logger

logger = setup_logger("QuotaGuard")

COST_TABLE = {
    "playlistItems.list": 1,
    "videos.list":        1,
    "channels.list":      1,
    "search.list":        100,
}


class QuotaGuard:
    def __init__(self):
        self._state = self._load()

    # ── 持久化 ──────────────────────────────────────

    def _load(self) -> dict:
        today = str(date.today())
        if os.path.exists(QUOTA_STATE_PATH):
            try:
                with open(QUOTA_STATE_PATH, "r", encoding="utf-8") as f:
                    state = json.load(f)
                # 新的一天重置計數
                if state.get("date") != today:
                    return {"date": today, "used": 0, "paused": False}
                return state
            except Exception:
                pass
        return {"date": today, "used": 0, "paused": False}

    def _save(self):
        with open(QUOTA_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

    # ── 公開 API ────────────────────────────────────

    @property
    def used(self) -> int:
        return self._state["used"]

    @property
    def paused(self) -> bool:
        return self._state["paused"]

    def can_call(self, operation: str = "videos.list") -> bool:
        """檢查是否可以執行該 API 操作"""
        if self._state["paused"]:
            logger.warning(
                f"[QuotaGuard] API 已暫停！今日已用 {self.used}/{QUOTA_DAILY_LIMIT} 點。"
                " 跳過此次 API 調用。"
            )
            return False
        cost = COST_TABLE.get(operation, 1)
        if self.used + cost > QUOTA_DAILY_LIMIT:
            self._state["paused"] = True
            self._save()
            logger.error(
                f"[QuotaGuard] 超出每日 Quota 上限！暫停所有 API 調用。"
            )
            return False
        return True

    def charge(self, operation: str = "videos.list", count: int = 1):
        """
        扣除 Quota 點數並檢查是否觸達告警門檻。
        :param operation: 操作類型（見 COST_TABLE）
        :param count: 調用次數（批量時傳入實際請求次數）
        """
        cost = COST_TABLE.get(operation, 1) * count
        self._state["used"] += cost

        if not self._state["paused"] and self._state["used"] >= QUOTA_WARN_AT:
            logger.warning(
                f"[QuotaGuard] ⚠️  今日 API 已消耗 {self._state['used']} 點，"
                f"達到告警門檻 {QUOTA_WARN_AT}！後續 API 調用已暫停，"
                " 剩餘任務將全部走 yt-dlp 路線。"
            )
            self._state["paused"] = True

        self._save()
        logger.info(
            f"[QuotaGuard] 消耗 {cost} 點（{operation} ×{count}）"
            f"，今日累計 {self._state['used']}/{QUOTA_DAILY_LIMIT}。"
        )
