# tests/conftest.py
"""
pytest 共用設定：
    把專案根目錄推進 sys.path，這樣測試文件就能直接 `from core.xxx import ...`
    而不用依賴呼叫者所在的目錄。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
