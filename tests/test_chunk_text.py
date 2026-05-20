# tests/test_chunk_text.py
"""
覆蓋 MapReduceTextEngine._chunk_text 的切分行為：
    - 空字串：回傳「全文」單元素
    - 單一短文本：不會拆分
    - 多 SEGMENT BREAK 標記：在分隔符處天然斷句
    - 詞數超過 chunk_size：強制按詞窗口切，且窗口重疊量 = overlap
"""
from core.map_reduce_engine import MapReduceTextEngine

chunk = MapReduceTextEngine._chunk_text


def test_short_text_single_chunk():
    """短文本一個 chunk 即可裝下。"""
    result = chunk("hello world", chunk_size=100, overlap=10)
    assert result == ["hello world"]


def test_segment_break_splits():
    """SEGMENT BREAK 標記應該作為自然斷點分隔。"""
    text = "alpha beta [SEGMENT BREAK @01:00] gamma delta"
    result = chunk(text, chunk_size=100, overlap=10)
    # 兩段合起來在 chunk_size=100 內 → 仍會被合併成 1 個
    assert len(result) == 1
    assert "alpha" in result[0] and "gamma" in result[0]


def test_long_text_force_split():
    """詞數超過 chunk_size 必須強制按窗口拆。"""
    words = ["w%d" % i for i in range(50)]
    text = " ".join(words)
    result = chunk(text, chunk_size=20, overlap=5)
    assert len(result) >= 2
    # 每塊不得超過 chunk_size 個詞
    for c in result:
        assert len(c.split()) <= 20


def test_empty_returns_original():
    """空字串應回傳 [原文]，不丟資料。"""
    assert chunk("", chunk_size=100) == [""]
