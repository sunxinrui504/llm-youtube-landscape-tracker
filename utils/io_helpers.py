# utils/io_helpers.py
"""
共用 JSON I/O 工具。

抽出原因：
    main.py 與 gc_cleanup.py 原本各自實作 load_json/save_json，
    其中 gc_cleanup.py 的版本沒有結構校驗、缺乏例外處理，
    一旦 *.json 結構意外損毀（如 list ↔ dict 不一致），會直接拋例外導致 GC 中斷。
    統一收斂到此處，全項目共享同一份「容錯 + 結構校驗」實作。
"""
import json
import os
from typing import Any, Callable

from utils.logger import setup_logger

logger = setup_logger("IOHelpers")


def load_json(path: str, default_factory: Callable[[], Any]) -> Any:
    """
    安全載入 JSON。
    - 檔案不存在 → 回傳 default_factory()
    - JSON 解析失敗 → 紀錄錯誤後回傳 default_factory()
    - 載入後型別與 default 不一致（如期望 dict 但得到 list）→ 警告後重置
    """
    default = default_factory()
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"[load_json] 讀取 {path} 失敗: {e}")
        return default

    if type(data) is not type(default):
        logger.warning(
            f"[load_json] {path} 結構不符（期望 {type(default).__name__}，"
            f"實際 {type(data).__name__}），改用預設值。"
        )
        return default
    return data


def save_json(path: str, data: Any) -> None:
    """安全寫入 JSON（utf-8、不轉義非 ASCII、縮排 2）。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[save_json] 寫入 {path} 失敗: {e}")


def save_data_js(json_path: str, data: Any) -> None:
    """
    同時寫入 data.json 和 data.js。
    data.js 內容為 window.__TRACKER_DATA__ = {...}，
    讓 index.html 用 <script src="data.js"> 載入，
    避免 file:// 協議下 fetch() 被 CORS 阻止。
    """
    save_json(json_path, data)
    js_path = json_path.replace(".json", ".js")
    try:
        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(f"window.__TRACKER_DATA__ = {json_str};\n")
    except Exception as e:
        logger.error(f"[save_data_js] 寫入 {js_path} 失敗: {e}")
