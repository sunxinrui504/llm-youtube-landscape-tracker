# tests/test_io_helpers.py
"""
覆蓋 utils/io_helpers 的容錯能力：
    - 檔案不存在 → 回傳 default_factory()
    - JSON 損壞 → 回傳 default_factory() 而不拋例外
    - 結構型別錯配（期望 dict 卻拿到 list，回歸過往坑）→ 回傳 default_factory()
    - 正常往返（save → load）→ 內容一致
"""
import json
from pathlib import Path

from utils.io_helpers import load_json, save_json


def test_load_missing_returns_default(tmp_path: Path):
    """檔案不存在時應回傳 default_factory() 結果。"""
    missing = tmp_path / "no_such.json"
    assert load_json(str(missing), dict) == {}
    assert load_json(str(missing), list) == []


def test_load_corrupted_returns_default(tmp_path: Path):
    """損壞 JSON 不應拋例外，需安全降級。"""
    bad = tmp_path / "broken.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert load_json(str(bad), dict) == {}


def test_load_type_mismatch_returns_default(tmp_path: Path):
    """期望 dict 卻載入到 list（歷史污染情境），需被攔下重置。"""
    polluted = tmp_path / "list_when_dict.json"
    polluted.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_json(str(polluted), dict) == {}


def test_save_then_load_roundtrip(tmp_path: Path):
    """save_json → load_json 應內容守恆，含中文與巢狀結構。"""
    p = tmp_path / "round.json"
    data = {"频道": "Karpathy", "videos": [{"id": "abc", "n": 1}]}
    save_json(str(p), data)
    assert load_json(str(p), dict) == data
    # 校驗檔案是 utf-8 且不轉義中文
    raw = p.read_text(encoding="utf-8")
    assert "频道" in raw and "Karpathy" in raw
    # 確保是合法 JSON
    json.loads(raw)
