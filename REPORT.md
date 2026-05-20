# LLM YouTube Landscape Tracker — 技術報告
Author: SUN Xinrui

本項目是一個**全自動 YouTube LLM 領域內容追蹤器**，通過 GitHub Actions 每 6 小時定時觸發，自動掃描 7 個頂級 AI/LLM YouTube 頻道的最新影片，完成**元數據採集 → 字幕轉錄 → LLM 語意分析 → 主題關聯矩陣構建**的端到端 Pipeline，最終輸出一個 HTML 儀表板（`index.html`），兼容本地 `file://` 瀏覽和 GitHub Pages 公開託管。
---

## 目錄

- [1. Problem Statement（問題陳述）](#1-problem-statement問題陳述)
- [2. Methodology（方法論）](#2-methodology方法論)
  - [2.1 系統總體架構](#21-系統總體架構)
  - [2.2 Step 1：採集調度器與雙層異常樹](#22-step-1採集調度器與雙層異常樹)
  - [2.3 Step 2：異質元數據標準化](#23-step-2異質元數據標準化)
  - [2.4 Step 3：三級降級轉錄 + SEGMENT BREAK 硬邊界](#24-step-3三級降級轉錄--segment-break-硬邊界)
  - [2.5 Step 4：Map-Reduce LLM 語意分析](#25-step-4map-reduce-llm-語意分析)
  - [2.6 Step 5：Jaccard 主題關聯矩陣](#26-step-5jaccard-主題關聯矩陣)
  - [2.7 Step 6：即時寫入與前端兼容](#27-step-6即時寫入與前端兼容)
- [3. Evaluation Dataset（評估數據集）](#3-evaluation-dataset評估數據集)
- [4. Evaluation Methods（評估方法）](#4-evaluation-methods評估方法)
- [5. Experimental Results（實驗結果）](#5-experimental-results實驗結果)
- [6. Engineering Hardening Log（工程加固記錄）](#6-engineering-hardening-log工程加固記錄)
- [7. Reproducibility（復現指南）](#7-reproducibility復現指南)
- [8. 文件與目錄結構](#8-文件與目錄結構)
- [附錄 A：核心公式匯總](#附錄-a核心公式匯總)
- [附錄 B：依賴清單](#附錄-b依賴清單)

---

## 1. Problem Statement（問題陳述）

1.問題：單一使用yt-dlp：無法識別出曾發佈、后改爲私密/不公開/刪除的影片，訪問其鏈接會報錯；純爬取，無官方授權，大規模長期運行存在封ip風險
單一使用YouTube Data API：免費配額度有限，大規模采集成本高；無法下載音視頻、無法獲取自動字幕原文
解決方法：yt-dlp爲主，YouTube Data API為輔的混合采集架構
yt-dlp支持大體量數據采集；能夠抓取音訊檔.m4a,和字幕檔.vtt/.srt
YouTube Data API識別不公開、私享或刪除的影片，確保自動化系統不會去下載；官方接口，穩定性強，無風險
實現：兩層異常樹+指數退避+滑動時間窗口
YtdlpVideoFailed    → 单个影片重试失败
        ↓
10分鐘内连续 3 个都失败
        ↓
YtdlpGlobalBroken  → yt-dlp全局抓取失敗 → 立刻切换 API 兜底
a)兩層異常樹：
第一層：YtdlpVideoFailed針對單個影片失敗：影片被私密/刪除；偶爾網絡變動
只重试本片，重试完失败 → 降级用 API 抓該影片（不影响全局）
第二層：YtdlpGlobalBroken針對整條yt-dlp路徑失敗：Ip被限流；反爬機制被觸發
直接全局降级 → 全部切换 API 模式
b)指數退避（Exponential Backoff）：
指數退避公式：t_wait = min( BACKOFF_BASE ^ attempt + jitter , BACKOFF_MAX )其中 `BACKOFF_BASE=2`，`BACKOFF_MAX=30`，`jitter ∈ U(0,1)`
每次重试的等待时间 = MIN(2^重试次数+一点点随机抖动, 最长等待上限 30 秒)
原因：越失敗越慢，指數增長的等待時間讓重試頻率快速下降，避免持續觸發速率限制；加入均勻分佈隨機抖動（Jitter）避免多個並行請求在同一時刻同時重試（Thundering Herd 問題）。上限截斷防止等待時間無限增長。
時間窗口全局降級：
時間窗口全局降級公式：
全局降级 = 连续失败 >= 3 次 and 所有失败都发生在 10 分钟内
作用：避免偶然錯誤，并非全局抓取失敗情況下被錯誤判斷；引入 10 分鐘滑動時間窗口，只有窗口內密集連續失敗才觸發全局降級，大幅降低誤切概率

2.問題：語音轉錄與文本提取中如何實現快速文本提取
解決方法：三級降級策略
優先級 1：先嘗試下載 YouTube 作者手動上傳的字幕`_download_subtitles(prefer_manual=True)`，速度極快
優先級 2：若無官方字幕，下載YouTube自動生成的字幕`_download_subtitles(prefer_manual=False)`速度極快
優先級 3（保底）：若前兩者質量太差或不存在，使用 yt-dlp 下載低位元率音訊（.m4a），並調用使用faster-whisper轉錄成文本`_transcribe_with_faster_whisper()`

3.問題：文本太长會導致LLM遺漏内容；提取出的内容是文本形式，如何分辨提取不同説話人和内容
解決方法：Map-Reduce 架構
第一步：語義分塊（chunk）
方法：採用語意感知滑動窗口分塊（Semantic-Aware Sliding Window Chunking）
1. 先按 `[SEGMENT BREAK @MM:SS]` 標記切割文字為自然語段（Segment）
2. 將語段依次填入緩衝區（Buffer），直到 word 數超過 `chunk_size=1200`
3. 超長語段內部按 word 數硬切，相鄰塊保留 `overlap=200` 個 word 的重疊
重疊窗口公式：片段i=词汇数组[i×(单段长度−重叠长度):i×(单段长度−重叠长度)+单段长度]，其中 S=1200（分块大小），O=200（重叠长度）
原因：
SEGMENT BREAK 優先切分：保證同一位說話者的連續語段不被切斷，避免 LLM 在 chunk 邊界處產生虛假的說話人切換判斷。
重疊窗口：相鄰 chunk 共享 200 個 word 的上下文，防止句子在邊界處被硬截斷導致的語意斷裂。
第二步：Map（結構化指令）：處理單個文字分塊，提取局部說話人和關鍵詞
方法：識別文字中提到的所有人名，為每行對話標注說話者，無法確認時使用 `[Unverified Speaker N]`，提取 3-5 個 LLM 技術關鍵詞
Map 階段的每個 chunk 由獨立的 worker 並行調用 LLM API
第三步：Reduce（全局消歧）：聚合所有 Map 結果，消歧、去重、生成最終摘要
方法：將 `[Unverified Speaker N]` 解析為實際人名，合併所有 chunk 的關鍵詞并去重，影片分類，生成摘要，输出结构化、可直接入库的数据

4.問題：API 有并发限制，顺序处理太慢，并发太高会被封
解决方法：429 安全網
t_retry = Retry-After header 的值（如果存在）
t_retry = LLM_RETRY_BASE × 2^(attempt - 1) （其他情况）
其中：LLM_RETRY_BASE = 3，LLM_MAX_RETRIES = 2
正常情況下不應觸發 429，重試僅作為安全網處理偶發的瞬時過載，最多2次重試

5.噪聲問題：瀏覽YouTuber主頁時發現其中有大量Shorts和短視頻，包含信息量少且缺乏連續性的技術上下文，如果強行讓 LLM 去還原劇本，LLM 會抓不到核心架構，只會吐出垃圾數據（Noise），污染我們的 themes_matrix 全局主題矩陣
解決方法：採取 「網址特徵 + 時長」雙重過濾
URL 路由審查: 檢查 yt-dlp 吐出的 webpage_url 是否匹配 *[youtube.com/shorts/](https://youtube.com/shorts/)* 模式。
時長硬防禦: 檢查 duration 欄位是否小於或等於 60 秒
命中以上任一條件，影片直接被標記為 is_shorts: true 並在中介層實施斷流，不觸發後續的轉錄與大模型調用。

6.問題：腳本每次執行都會把頻道裡的幾百支歷史影片全部重新下載、重跑 Whisper、重刷 LLM Token
解決方法：
a)processed_videos.json 的增量去重防線（Skip Mechanism）記錄所有已處理過的 video_id 及其對應的結構化特徵（如 topics），在 yt-dlp 或 YouTube API 抓取到頻道最新影片列表後，系統不會盲目進入下載和轉錄階段，而是先與數據庫比對，僅處理為處理過的影片
b)每次定時觸發（例如每 6 小時），如果頻道沒有發新影片，Pipeline 在第一步的幾秒鐘內就會安全結束；如果發了 1 支新影片，系統就只會針對這 1 支新影片啟動後續的下載、Whisper 和 LLM 流程，歷史影片完全不受影響

7.問題：GitHub Actions 是一個無狀態、每 6 小時觸發的增量環境。如果每次執行只將「今天新出的 1 支影片」與「最新 20 條老影片」一起餵給 LLM，那麼這支新影片將永遠無法與 3 個月前的歷史爆款建立關聯，導致推薦系統出現嚴重的歷史斷層
解決方案：Jaccard 矩陣的「全域增量滾動計算」
為了在零預算、無向量數據庫（Vector DB）的架構下解決這個問題，項目放棄了「動態餵給 LLM 20 條」的局部方案，而是採用了全域標籤池 + 離線 Jaccard 矩陣滾動更新的策略。
實現步驟：
a)持久化標籤庫（State Persistence）：在 data.json（即 output_payload）的 videos 數組中，持久化保留每一支影片經由 LLM 提取出的精煉標籤（即 `ai_topics` 欄位，例如：["RAG", "GraphDB", "LlamaIndex"]）。而 processed_videos.json 僅負責記錄增量去重狀態（video_id → status）。
b)全量加載與增量注入：當 GitHub Actions 執行時：讀取歷史所有的 processed_videos.json（包含 3 個月前、半年前的所有老影片數據，設總數為 $N$）。Pipeline 抓取到今天的 1 支新影片，調用 Moonshot LLM 僅為這單支影片生成 topics_new。將這支新影片及其標籤注入到總列表中，此時總影片數變為 $N+1$。
c)全域 Jaccard 矩陣重算（超輕量級）：在 Python 腳本的最後階段（Payload 構建期），不再調用任何 API，而是直接利用 CPU 跑一個雙重循環，計算新影片與全量歷史影片的 Jaccard 相似度：J(A, B) = |A∩B|/|A∪B|（交集除以並集）。因為標籤已經是精煉後的字串集合（每個影片僅 3-5 個標籤），即使歷史影片有上千支，在 Python 中進行 1 * N 的集合交並集運算也僅需數毫秒。
d)動態 Top-3 截斷：對計算結果進行降序排序，篩選出 $J > 0.1$ 且相似度最高的 3 支影片（無論它是 3 天前還是 3 個月前），寫入該新影片的 related_videos 欄位。同時，這支新影片的標籤也會反向觸發老影片的 related_videos 更新（雙向關聯）。

8.問題：只用向量 / LLM 计算太重，GitHub Actions 跑不动，每次都让 LLM 或向量模型去全库对比速度慢且成本高
解決方法：全量矩陣算法themes_matrix（全域）與 related_videos（局部）雙層結構
第一層：主題 → 影片映射（倒排索引）：themes_matrix（宏觀/全域關聯：主題對應頻道矩陣）
實現方式：全域標籤倒排索引（Inverted Index）。
邏輯：系統會遍歷全量影片，抽取出所有出現過的主題標籤（Unique Topics），並為每個標籤建立一個包含所有相關影片（video_id、channel、title）的列表，最終構成 `Dict[topic_str, List[{video_id, channel, title}]]` 倒排索引。
用途：供前端 Dashboard 渲染可點擊的「主題標籤雲」（Topic Chip Grid），點擊任一標籤即聯動篩選出與該主題相關的所有影片
第二層：影片 → 相關影片鏈（兩兩 Jaccard）:related_videos（微觀/局部關聯：單篇影片的延伸推薦）
實現方式：Jaccard 相似度
邏輯：它是 Video 物件內的一個屬性，記錄與當前影片最相關的 3 個 video_id，供前端在用戶點擊某支影片時，在側邊欄或彈窗中展示「猜你喜歡」。
Jaccard 相似度公式：J(A,B) = |A∩B| / |A∪B|
Jaccard 相似度 = 两个标签集合的交集大小 ÷ 两个标签集合的并集大小（越接近 1 = 越像，越接近 0 = 越无关）
只有兩隻影片的相似度大于 0.1（J(Ai,Aj)>0.1且i不等於j），才认为两支影片有关联，即只要有一个标签相同，就视为相关。
data.json 規格與完整結構：
為了兼顧前端「零框架、單文件雙擊直開」的極簡設計，Pipeline 最終會輸出一個標準的 JSON 結構（或包裹在 `window.__TRACKER_DATA__ = {...};` 中的 JS 文件）。這個結構完整包含了全域主題矩陣與每支影片的局部關聯

---

## 2. Methodology（方法論）

### 2.1 系統總體架構

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        main.py — 主編排器 (Orchestrator)                     │
│                                                                             │
│  ┌──────────┐    ┌──────────┐    ┌───────────┐    ┌──────────┐    ┌───────┐ │
│  │ Step 1   │───▶│ Step 2   │───▶│ Step 3   │───▶│ Step 4 │───▶│Step 5 │ │
│  │ 數據採集 │    │ 數據標準化│     │ 字幕轉錄  │      │ LLM 分析 │   │關聯矩陣│ │
│  │Ingestion │    │Transformer│    │Transcribe │    │MapReduce │   │Jaccard│ │
│  └──────────┘    └──────────┘    └───────────┘    └──────────┘    └───────┘ │
│       │                                                               │     │
│       ▼                                                               ▼     │
│  QuotaGuard                                                    data.js      │
│  (API 配額守衛)                                           (前端即時寫入)     │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
                              ┌─────────────┐
                              │ index.html  │
                              │ 靜態儀表板   │
                              └─────────────┘
```

技術棧概覽：

| 層級 | 技術 | 用途 |
|------|------|------|
| 語言 | Python 3.10+ | Pipeline 全流程 |
| 數據採集 | `yt-dlp` + YouTube Data API v3 | 頻道掃描、元數據抓取、字幕下載 |
| 語音轉錄 | `faster-whisper`（CTranslate2 + Whisper） | 本地 ASR 保底轉錄 |
| LLM 語意分析 | **Moonshot (Kimi) API**（`moonshot-v1-8k` / `moonshot-v1-32k`） | Map-Reduce 對話劇本還原 |
| 關聯計算 | Jaccard 相似度（純 Python） | 影片主題關聯矩陣 |
| 前端展示 | 原生 HTML + CSS + JavaScript（零框架） | 靜態儀表板 |
| CI/CD | GitHub Actions | 每 6h 增量追蹤 + 每週日 GC 死鏈清理 |
| 數據持久化 | JSON（`data.json` / `data.js` / `processed_videos.json`） | GitOps 狀態管理 |

### 2.2 Step 1：採集調度器與雙層異常樹

`core/ingestion.py` 中的 `IngestionDispatcher` 採用**「主路線 yt-dlp + 配額型 API 降級」**
的雙路線設計，並通過**兩層異常樹**實現對 IP 級反爬的精細應對：

```
YtdlpVideoFailed     ← 單片重試耗盡（降級到 API 獲取該片）
    │
    └─▶ 連續 N 次 → YtdlpGlobalBroken  ← 全局降級為 API-only 模式
```

**指數退避**（首次不等待，重試時）：

$$
t_{\text{wait}} = \min\bigl(\text{BACKOFF\_BASE}^{\text{attempt}} + \text{jitter},\; \text{BACKOFF\_MAX}\bigr)
$$

`BACKOFF_BASE = 2`、`BACKOFF_MAX = 30s`、`jitter ∈ U(0,1)` —— 指數增長 + 抖動防止
Thundering Herd 同步重試。

**時間窗口全局降級**：

$$
\text{trigger} \iff F \geq \text{GLOBAL\_FAIL\_THRESH} \;\wedge\; (t_{\text{now}} - t_{\text{first\_fail}}) \leq \text{GLOBAL\_FAIL\_WINDOW}
$$

`GLOBAL_FAIL_THRESH = 3`、`GLOBAL_FAIL_WINDOW = 600s`。意義：**僅當 10 分鐘窗口內密集
連續失敗** 才視為 IP 被精準阻斷；跨越窗口的零星失敗會清零，避免誤判。

**額外的數據健壯性**：

- `_extract_video_id()` 用 `^[A-Za-z0-9_-]{11}$` 正則嚴格校驗，防止頻道/播放列表的
  24 字 channel_id 污染 `processed_videos.json`（歷史 bug 治理結果，見 §6）。
- `_get_channel_videos_ytdlp()` 強制拼接 `/videos` 子路徑，避免 yt-dlp 返回 tabs/
  子播放列表項。

### 2.3 Step 2：異質元數據標準化

`core/transformer.py` 將 `yt-dlp` 與 YouTube API v3 兩種**結構完全不同**的原始 JSON
統一清洗為下列前端可直接消費的 schema：

```json
{
  "video_id": "xxxxxxxxxxx",
  "url": "https://www.youtube.com/watch?v=...",
  "title": "...",
  "channel": "...",
  "published_at": "2026-05-20",
  "duration_seconds": 1234,
  "metrics": { "views": 0, "likes": 0, "comments": 0 },
  "metadata": { "description": "...", "tags": [] },
  "processing_info": {
    "source_engine": "yt_dlp | api_v3",
    "has_manual_sub": true,
    "has_auto_sub": true
  },
  "chapters": [{ "title": "...", "start_time": 0 }]
}
```

防禦性編碼：所有 `dict.get(...)` 返回值都用 `or` 兜底（如 `raw_data.get("subtitles") or {}`），
避免外部 API 返回 `None` 時觸發鏈式 `TypeError`。時間獲取統一使用 UTC 感知的
`datetime.now(timezone.utc)`，已從棄用的 `datetime.utcnow()` 遷移。

### 2.4 Step 3：三級降級轉錄 + SEGMENT BREAK 硬邊界

`core/transcription.py` 採用 **零成本優先** 的三級降級：

| 優先級 | 方法 | API 開銷 | 速度 | 說明 |
|--------|------|----------|------|------|
| 1 | `_download_subtitles(prefer_manual=True)` | **零** | 最快 | YouTube 手動上傳字幕 |
| 2 | `_download_subtitles(prefer_manual=False)` | **零** | 快 | YouTube 自動生成字幕 |
| 3 | `_transcribe_with_faster_whisper()` | **零**（本地 CPU） | 慢 | faster-whisper 本地 ASR 保底 |

**SEGMENT BREAK 硬邊界注入**：

$$
\text{insert} \iff t_{\text{current}} - t_{\text{previous}} \geq \Delta_{\text{gap}} \quad (\Delta_{\text{gap}} = 30\text{s})
$$

YouTube 自動字幕是無分段的連續流，多人對話場景下 LLM 無法判斷說話人切換邊界。
在相鄰字幕時間差 ≥30 秒處強制插入 `[SEGMENT BREAK @MM:SS]` 錨點，
顯著降低 LLM 在 chunk 邊界處的「虛假說話人切換」。

faster-whisper（保底）配置：`base` 模型（74M 參數），CTranslate2 後端，
INT8 量化（記憶體 −50%、推理速度 ×2），`beam_size=5` Beam Search 解碼：

$$
\hat{y} = \arg\max_{y} \sum_{t=1}^{T} \log P(y_t \mid y_{<t}, X)
$$

### 2.5 Step 4：Map-Reduce LLM 語意分析

`core/map_reduce_engine.py` 是項目核心智能組件。將字幕長文流轉化為說話人、
對話劇本、主題關鍵詞、摘要的結構化結果。

**分塊策略 — 語意感知滑動窗口**：

1. 先按 `[SEGMENT BREAK @MM:SS]` 切割為自然語段；
2. 依次裝入緩衝區直至 word 數超過 `chunk_size = 1200`；
3. 超長語段內部以 word 為單位硬切，相鄰塊保留 `overlap = 200` words：

$$
\text{chunk}_i = \text{words}\bigl[i(S-O) \;:\; i(S-O)+S\bigr] \quad (S=1200,\; O=200)
$$

**Map-Reduce 並行架構**：

```
           ┌─────────────────────────────────────────────┐
           │              原始字幕文字流                  │
           └────────────────┬────────────────────────────┘
                            │
                     _chunk_text()
               (SEGMENT BREAK 語意分塊)
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
      ┌──────────┐   ┌──────────┐   ┌──────────┐
      │ Chunk 1  │   │ Chunk 2  │   │ Chunk 3  │   ← ThreadPoolExecutor
      │ Map 8k   │   │ Map 8k   │   │ Map 8k   │      max_workers=3
      └────┬─────┘   └────┬─────┘   └────┬─────┘      並行發送
           └──────────────┼──────────────┘
                          ▼
                  ┌──────────────┐
                  │  Reduce 32k  │  ← 單次調用，全局消歧+分類+摘要
                  └──────────────┘
```

| 階段 | 模型 | 上下文 | 任務 |
|------|------|--------|------|
| Map | `moonshot-v1-8k` | 8 K | 局部說話人標注 + 關鍵詞提取（並行 ×3） |
| Reduce | `moonshot-v1-32k` | 32 K | 跨段說話人消歧 + 主題去重 + 影片分類 + 摘要 |

**配額對齊**：`ThreadPoolExecutor(max_workers=3)` 精確匹配 Moonshot 帳號並發配額 3，
正常情況下不會觸發 429。同時保留 2 次安全網重試，優先遵從服務端 `Retry-After`：

$$
t_{\text{retry}} =
\begin{cases}
\text{Retry-After header} & \text{if header exists} \\
\text{LLM\_RETRY\_BASE} \cdot 2^{(a-1)} & \text{otherwise}
\end{cases}
$$

**Body 體積防禦**：`MAX_USER_CONTENT_CHARS = 28000` 截斷單次請求 user content；
`MAX_MAP_SNIPPET_CHARS = 4000` 截斷 Reduce 階段每段拼接的 Map 結果，
從根源防止 Groq 時代屢屢出現的 HTTP 413（body too large）問題（見 §6 工程加固）。

**LLM 輸出強約束**：`temperature=0.1`、`response_format={"type":"json_object"}`、
`timeout=60s`；解析側用 `_parse_json_safe()` 剝離 markdown code fence
後 `json.loads`，遇異常返回空 dict 走 `_fallback()`。

### 2.6 Step 5：Jaccard 主題關聯矩陣

`core/graph_matrix.py` 採用 **零依賴、零向量化** 的 Jaccard IoU：

$$
J(A, B) = \frac{|A \cap B|}{|A \cup B|}, \quad A, B \subseteq \text{ai\_topics}
$$

雙層輸出結構：

```
第一層 — 主題 → 影片倒排索引:
  themes_matrix = { "Transformer": [v1, v4, v7], "RAG": [v2, v3], ... }

第二層 — 影片 → 相關影片鏈（兩兩 Jaccard, 閾值 0.1, Top-3）:
  v1.related_videos = [{v4, 0.67}, {v7, 0.40}, {v2, 0.20}]
```

時間複雜度 \(O(n^2 k)\)，當前規模 \(n \leq 70\)、\(k \leq 5\) 計算成本可忽略。
選 Jaccard 而非 cosine/embedding 是工程權衡：在 LLM 精煉後的 2–5 個離散主題標籤上，
向量化會引入噪聲而非降噪。

### 2.7 Step 6：即時寫入與前端兼容

每處理完一支影片即調用 `save_data_js()` 同時寫入：

- `data.json`：標準 JSON，供程序與 GC 腳本讀取；
- `data.js`：`window.__TRACKER_DATA__ = {...};`，供 `index.html` 用 `<script src>`
  載入。

**為什麼要寫兩份**：`fetch("data.json")` 在 `file://` 協議下會被瀏覽器 CORS 阻止。
寫一份注入全局變量的 `.js`，讓用戶 **雙擊** `index.html` 就能直接看到最新結果，
同時完全兼容 GitHub Pages 的 `https://` 訪問。

---

## 3. Evaluation Dataset（評估數據集）

評估數據來自 Pipeline 自身在生產環境上持續運行所產生的真實 YouTube 影片數據。
本項目不存在「離線標註的測試集」，而是以**真實生產流量**作為持續評估對象，
每 6 小時調度一次累積。

| 項 | 配置 |
|---|---|
| 追蹤頻道數 | **7** 個（見下表） |
| 每頻道每輪抓取上限 | 10 支最新影片 |
| Pipeline 觸發頻率 | 每 6 小時 + 手動 |
| 增量去重鍵 | `processed_videos.json` 中的 11 字 `video_id` |
| Shorts 過濾規則 | `/shorts/` 路徑 ∨ 時長 ≤ 60s |
| 數據快照位置 | `data.json` / `data.js`（隨每次 CI run 提交回 Git） |

**追蹤頻道清單**（`config/settings.py::TARGET_CHANNELS`）：

| 頻道 | Channel ID |
|------|------------|
| Andrej Karpathy | `UCXUPKJO5MZQN11PqgIvyuvQ` |
| 3Blue1Brown | `UCYO_jab_esuFRV4b17AJtAw` |
| Yannic Kilcher | `UCZHmQk67mSJgfCCTn7xBfew` |
| Two Minute Papers | `UCbfYPyITQ-7l4upoX8nvctg` |
| AI Explained | `UCNJ1Ymd5yFuUPtn21xtRbbw` |
| Sam Witteveen AI | `UC55ODQSvARtgSyc8ThfiepQ` |
| IBM Technology | `UCKWaEZ-_VweaEx1j62do_vQ` |

---

## 4. Evaluation Methods（評估方法）

評估分為三類：**單元測試（正確性）**、**生產運行指標（工程性）**、
**LLM 輸出結構合規性（語意品質）**。

### 4.1 單元測試（`tests/`）

| 測試模組 | 覆蓋目標 |
|----------|----------|
| `test_extract_video_id.py` | 11 字 `video_id` 正則校驗、24 字 `channel_id` 拒絕、空輸入兜底 |
| `test_chunk_text.py` | `_chunk_text` 在 SEGMENT BREAK / overlap 邊界的切片正確性 |
| `test_io_helpers.py` | `load_json` 容錯路徑、`save_json` 寫入、`save_data_js` 雙寫一致性 |
| `test_transformer.py` | yt-dlp / api_v3 雙路徑的標準化結果欄位齊整、空值兜底 |

CI 在 `.github/workflows/tests.yml` 中作為 PR 強制關卡執行。

### 4.2 生產運行指標（工程性）

由 `utils/logger.py` 統一輸出，每輪 CI 日誌與 `quota_state.json` 為真實數據源：

| 指標 | 觀測方式 | 期望 |
|------|----------|------|
| YouTube API 每日消耗點數 | `quota_state.json::used` | < 100 / 10000 |
| `yt-dlp` 單片重試平均次數 | Ingestion logger | < 1.5（穩態） |
| `YtdlpGlobalBroken` 觸發頻次 | Ingestion logger | 月度 ≤ 1 次 |
| 字幕命中率（Step 3 優先級 1+2 成功比例） | Transcription logger | ≥ 80% |
| faster-whisper 回退率 | `transcription_source == faster_whisper` | ≤ 20% |
| Map-Reduce 平均並行度 | 日誌中 chunk 數 / 串行時間估計 | ≈ 3× |
| LLM 429 重試觸發率 | MapReduce logger | ≤ 1% |

### 4.3 LLM 輸出結構合規性（語意品質）

`_parse_json_safe()` 對 Reduce 結果做 schema 級隱式校驗：

- **必有欄位**：`speaker_type`、`speakers`、`ai_topics`、`summary`；
- **可導出欄位**：`dialogue_script`（去重後保留 Top-120 條對白）；
- 解析失敗→`_fallback("reduce_failed")` 寫入安全默認值，
  Pipeline **不中斷**。

「夠用」標準：經人工抽樣，`speaker_type` 三分類（Solo / Interview / Group）
與影片實際情況一致；`ai_topics` 與影片標題/描述有可解釋的語意重疊；
摘要為 2–3 句的自然語言段落。

---

## 5. Experimental Results（實驗結果）

下表為**累計到當前**的真實運行成果（持續刷新，以倉庫中 `data.json` 為準）：

### 5.1 數據面

| 項 | 觀測值 |
|---|---|
| `data.json::videos[].length` | 隨時間單調增長（增量採集） |
| 主題倒排索引 `themes_matrix` 規模 | 約 30–80 個獨立 LLM 主題標籤 |
| 平均每片 `related_videos` 條數 | ≤ 3（Top-K 截斷） |
| `processed_videos.json` 規模 | 包含 `ok` / `filtered_shorts` / `fetch_failed` 三類狀態鍵 |

### 5.2 工程面

| 項 | 觀測值 | 目標 | 達成 |
|---|---|---|---|
| YouTube API 日均消耗 | 個位數點 | < 100 | ✅ |
| LLM 全部走 Moonshot 免費額度 | RPM ≤ 20, 並發 ≤ 3 | 符合 | ✅ |
| 雙擊 `index.html` 可訪問 | data.js 注入 | ✅ | ✅ |
| Pipeline 抗單點故障 | API/yt-dlp/字幕/whisper 任一斷服均有 fallback | ✅ | ✅ |
| 增量去重正確性 | `video_id` 正則校驗無 channel_id 污染 | ✅ | ✅ |

### 5.3 前端面

`index.html` 提供：

- **主題標籤雲**（按出現頻率排序，可點擊聯動篩選）
- **影片總表**：標題、頻道、發布日、說話類型、說話人、AI 主題、AI 摘要、相關影片
- **三維篩選**：關鍵字搜索 × 頻道篩選 × 說話類型篩選 × 主題標籤篩選

---

## 6. Engineering Hardening Log（工程加固記錄）

下表是項目演化過程中**已落地** 的關鍵加固與 bug 修復，每一項在代碼中都有對應實現：

| # | 問題 | 影響 | 修復 |
|---|------|------|------|
| 1 | yt-dlp 頻道列表返回 24 字 channel_id 污染 `processed_videos.json` | 增量去重失效、後續永遠跳過該頻道 | `_extract_video_id` + `_VIDEO_ID_RE` 正則嚴格校驗 |
| 2 | 外部 API 偶爾返回 `None` 字段，`dict.get()` 鏈式 `TypeError` | Pipeline 崩潰 | 全鏈路 `or {}` / `or []` 兜底 |
| 3 | Groq 時代 HTTP 413（body too large）頻繁 | LLM 階段直接失敗 | `MAX_USER_CONTENT_CHARS=28000` + `MAX_MAP_SNIPPET_CHARS=4000` + 遷移 Moonshot |
| 4 | Groq 免費 API 429 與冷卻不足 | 連鎖失敗 | 429 安全網 + 與配額對齊的 `ThreadPoolExecutor(max_workers=3)` |
| 5 | JSON 結構校驗缺位導致啟動阻塞 | 啟動即崩 | `utils/io_helpers.load_json` 加入型別校驗 + `_load` 字段 `setdefault` 兜底 |
| 6 | `_parse_json_safe` 中誤寫成未閉合原始字符串 `r'rtrep` | basedpyright 報「字符串未終止」、Reduce 結果永走 fallback | 改回 `return json.loads(clean)` |
| 7 | `fetch_metadata` 類型注解寫成 `Dict` 但實際返回 2-tuple | 類型檢查紅線 | 改為 `Tuple[Dict[str, Any], str]` |
| 8 | `core/transformer.py` 仍用 `datetime.utcnow()` | Python 3.12+ DeprecationWarning，未來會移除 | 抽出 `_today_utc()` 改用 `datetime.now(timezone.utc)` |
| 9 | `_inject_segment_breaks` / `_chunk_text` 時間正則 `\d{2}` | ≥ 100 分鐘的長影片 SEGMENT BREAK 全部失效 | 兩處正則改為 `\d{1,3}` |
| 10 | `QuotaGuard._load` 不校驗字段齊整 | `quota_state.json` 殘缺時 KeyError | 加入頂層 dict 校驗 + `setdefault("used", 0)` 等兜底 |
| 11 | `gc_cleanup.py` 用 `load_json(..., dict)` 退化為 `{}` | 結構不一致風險 | 默認 schema 對齊 `main.py` |

---

## 7. Reproducibility（復現指南）

### 7.1 本地運行

```bash
# 1. Clone
git clone https://github.com/<your-account>/llm-youtube-landscape-tracker.git
cd llm-youtube-landscape-tracker

# 2. 安裝依賴（建議 Python 3.10+）
python -m pip install -r requirements.txt

# 3. 設置環境變量（也可直接寫死在 config/settings.py，但建議用 env）
$env:YOUTUBE_API_KEY  = "<your YouTube Data API v3 key>"
$env:MOONSHOT_API_KEY = "<your Moonshot/Kimi API key>"

# 4. 跑 Pipeline
python main.py

# 5. 雙擊 index.html 即可看到結果（依賴自動生成的 data.js）
```

> **PowerShell 提示**：請使用分號 `;` 連接命令，不要使用 `&&`。

### 7.2 CI / CD（GitHub Actions）

| 工作流 | 觸發 | 功能 |
|--------|------|------|
| `.github/workflows/tracker.yml` | 每 6h / 手動 | 主 Pipeline：採集+轉錄+分析+寫入+Git Push |
| `.github/workflows/update_tracker.yml` | 每 6h / 手動 | 備用版本（使用 GH_PAT） |
| `.github/workflows/gc_cleanup.yml` | 每週日 03:00 UTC / 手動 | 死鏈清理 + 矩陣重建 |
| `.github/workflows/weekly_gc.yml` | 每週日 00:00 UTC / 手動 | 備用 GC |
| `.github/workflows/tests.yml` | PR / push | 單元測試關卡 |

所有寫入類工作流共用 `concurrency group`，配合 `git pull --rebase`
保證對 JSON 數據文件的線性安全合流。

### 7.3 GitHub Pages 部署

開啟倉庫 *Settings → Pages → Source: main / root*，
訪問 `https://<account>.github.io/llm-youtube-landscape-tracker/` 即可。
無需 build 步驟，靜態文件直接服務。

---

## 8. 文件與目錄結構

```
.
├── main.py                       # 主編排器
├── gc_cleanup.py                 # 每週死鏈清理
├── config/
│   └── settings.py               # 頻道清單 / API key / 限流參數
├── core/
│   ├── ingestion.py              # 採集調度器（兩層異常樹 + 指數退避）
│   ├── transformer.py            # yt-dlp / api_v3 異質結構標準化
│   ├── transcription.py          # 三級降級轉錄 + SEGMENT BREAK
│   ├── map_reduce_engine.py      # Map-Reduce LLM 引擎（Moonshot）
│   ├── graph_matrix.py           # Jaccard 主題關聯矩陣
│   └── quota_guard.py            # YouTube API 配額守衛
├── utils/
│   ├── io_helpers.py             # 容錯 JSON I/O + data.js 注入
│   └── logger.py                 # 統一格式化日誌
├── tests/                        # 單元測試
├── data.json / data.js           # Pipeline 輸出（前端數據源）
├── processed_videos.json         # 增量去重狀態庫
├── quota_state.json              # API 配額計數器持久化
├── index.html                    # 零框架靜態儀表板
├── DESIGN.md                     # 系統設計文檔
└── README.md                     # 本報告
```

---

## 附錄 A：核心公式匯總

| # | 公式 | 用途 | 位置 |
|---|------|------|------|
| 1 | t=min(B^a+U(0,1),T max) | 指數退避重試 | `core/ingestion.py` |
| 2 | trigger⟺F≥N∧Δt≤W | 時間窗口全局降級 | `core/ingestion.py` |
| 3 | insert⟺ti−ti−1≥30s | SEGMENT BREAK 硬邊界 | `core/transcription.py` |
| 4 | y=arg max ∑ t logP(yt∣y <t,X) | Beam Search 解碼 | faster-whisper |
| 5 | chunk i=words[ i(S−O):i(S−O)+S] | 滑動窗口分塊 | `core/map_reduce_engine.py` |
| 6 | J(A,B) = ∣A∩B∣ / ∣A∪B∣ | Jaccard 相似度 | `core/graph_matrix.py` |
| 7 | t retry=B⋅2^(a−1) | LLM 429 退避 | `core/map_reduce_engine.py` |

---

## 附錄 B：依賴清單

| 包 | 用途 |
|---|------|
| `yt-dlp` | YouTube 影片/字幕/音訊下載 |
| `requests` | HTTP 客戶端（Moonshot API + YouTube API） |
| `curl_cffi` | 反爬伪装 HTTP 客戶端（yt-dlp 配套） |
| `google-api-python-client` | YouTube Data API v3 SDK |
| `isodate` | ISO 8601 時長解析 |
| `faster-whisper` | 本地 Whisper ASR（CTranslate2 引擎） |

---

## License

MIT（見 [`LICENSE`](./LICENSE)）。

