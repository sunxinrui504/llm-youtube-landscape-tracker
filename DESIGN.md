# LLM YouTube Landscape Tracker — 系統設計文件

> 本文件隨架構演進持續更新，記錄每一個設計決策及其理由。

---

## 1. 項目目標

追蹤多個頂級 LLM YouTube 頻道，通過 AI 轉錄與語意分析，生成一張可在瀏覽器中實時查看的頻道主題關係表格：

- 每一行代表一支影片，包含：頻道、講者、AI 提取主題、摘要、相關影片
- 表格自動更新（GitHub Actions 定時驅動）
- 前端為純靜態 HTML，托管在 GitHub Pages

---

## 2. 追蹤頻道清單

| 頻道 | 定位 |
|------|------|
| @AndrejKarpathy | 前 Tesla AI 總監、OpenAI 創始成員，深度 LLM 教學 |
| @MatthewBerman | 高頻更新 LLM 工具評測與新模型速覽 |
| @YannicKilcher | DeepMind 研究員，深度論文解讀 |
| @TwoMinutePapers | AI 論文通俗化解讀 |
| @aiexplained-official | LLM 能力邊界深度探討 |
| @samwitteveenai | AI 開發實戰與工程教學 |
| @WolframResearch | 計算智能與 AI 基礎理論 |

---

## 3. 採集層（Ingestion Layer）

### 3.1 核心引擎選型：yt-dlp（主）+ YouTube Data API v3（輔）

**選擇 yt-dlp 而非 OpenClaw 的理由**：

OpenClaw 基於 RSS/Atom feed，只能拿到影片標題和 URL，無法獲取字幕、音訊、章節等媒體內容——而這些是本系統 AI 轉錄管線的必要輸入。yt-dlp 是唯一能完整支持整個 Pipeline 的工具。

| 能力 | yt-dlp | YouTube API v3 | OpenClaw |
|------|--------|----------------|---------|
| 字幕下載 | ✅ | ❌ | ❌ |
| 音訊下載 | ✅ | ❌ | ❌ |
| 章節/Timeline | ✅ | ❌ | ❌ |
| 互動數據（觀看/點讚）| ✅ | ✅ | ❌ |
| 影片狀態驗證 | ❌ | ✅ | ❌ |
| 費用 | 免費 | 免費（有配額）| 免費 |

### 3.2 兩層異常樹設計

```
YtdlpVideoFailed   — 單支影片重試 MAX_RETRIES(3) 次後降級至 API
                      只影響此支影片，其他繼續走 yt-dlp

YtdlpGlobalBroken  — 連續 GLOBAL_FAIL_THRESH(5) 支觸發 VideoFailed
                      整個 Pipeline 切換 api_only 模式
```

**與原始思路的差異**：原始設計中 `YtdlpBrokenException` 同時承擔影片級和全局級的語義，容易造成混淆。新設計將兩者分開：VideoFailed 是「局部降級」，GlobalBroken 是「全局切換」，調度器通過 `_consecutive_failures` 計數器自動識別。

### 3.3 Quota 防禦

| 操作 | 消耗點數 |
|------|----------|
| `playlistItems.list`（獲取頻道影片列表）| 1 點/次 |
| `videos.list`（單片/批量元數據）| 1 點/次（最多 50 個 ID）|
| `search.list` | 100 點/次（**禁止使用**）|

每日預算估算（7 個頻道 × 每 6 小時更新一次）：

```
列表掃描：7 頻道 × 4 次/天 × 1 點 = 28 點/天
降級備援：假設 10 支影片需降級 × 1 點 = 10 點/天
GC（週日）：200 支 ÷ 50 × 1 點 = 4 點/週

總計：~40 點/天，遠低於 10,000 點上限
```

`QuotaGuard` 在消耗達到 8,000 點時自動暫停 API 並寫入告警，所有後續任務回退 yt-dlp。

---

## 4. 轉錄層（Transcription Layer）

### 4.1 三級字幕策略

```
優先級 1：YouTube 手動字幕（Manual Captions）— 最準確，直接下載 .vtt
優先級 2：YouTube 自動字幕（Auto-generated）— 免費，質量中等
優先級 3：faster-whisper 本地 ASR（base 模型，CPU int8 量化）— 保底
```

**為什麼選 faster-whisper 而非 Groq Whisper API**：

- faster-whisper 完全本地運行，零 API 費用，GitHub Actions ubuntu runner 可以直接跑 `base` 模型（~150MB）
- 不依賴外部服務可用性，Pipeline 更健壯
- Groq Whisper 仍作為可選升級路線（更換 `_transcribe_with_faster_whisper` 實現即可）

### 4.2 SEGMENT BREAK 硬邊界預處理

VTT 字幕的核心問題：自動生成字幕沒有標點，說話人切換時沒有任何標記，LLM 難以定位對話邊界。

解決方案：在餵給 LLM 之前，對每段**時間差超過 30 秒**的相鄰字幕行插入：

```
[SEGMENT BREAK @MM:SS]
```

Map Prompt 明確告知 LLM 這是**強說話人邊界訊號**，顯著提升多人場景下的說話人識別精度。

---

## 5. AI 分析層（Map-Reduce Engine）

### 5.1 架構

```
全文字幕（可能 30,000+ 詞）
    ↓  按 SEGMENT BREAK 優先切割，再按 3000 詞滑動窗口
[切片 1] [切片 2] ... [切片 N]
    ↓  Map：llama3-8b（快速/免費）並行處理
{partial_script, partial_keywords} × N
    ↓  Reduce：llama3-70b（強推理）整合
{speaker_type, speakers, ai_topics, summary, dialogue_script}
```

### 5.2 多人口說防禦策略

- LLM 必須先建立「說話人註冊表」（掃描所有名字提及）
- 身份模糊時強制使用 `[Unverified Speaker N]`，禁止盲猜
- Reduce 階段跨切片解析 Unverified 身份
- 稱呼錨定（"Thanks Andrej", "To your point David"）作為強邊界判定

---

## 6. 數據層

### 6.1 processed_videos.json（增量狀態庫）

```json
{
  "dQw4w9WgXcQ": {
    "title": "...",
    "processed_at": "2026-05-20T10:00:00Z",
    "status": "ok"
  }
}
```

**防算力破產**：GitHub Actions 每次啟動前先比對此文件，已存在的影片 ID 直接跳過，確保每次只處理真正的新影片。

### 6.2 data.json（前端消費格式）

```json
{
  "last_updated": "2026-05-20T10:00:00Z",
  "themes_matrix": {
    "RAG": [{"video_id": "...", "channel": "...", "title": "..."}]
  },
  "videos": [
    {
      "video_id": "...",
      "url": "https://youtube.com/watch?v=...",
      "title": "...",
      "channel": "...",
      "published_at": "2026-05-20",
      "duration_seconds": 3600,
      "metrics": {"views": 0, "likes": 0, "comments": 0},
      "speaker_type": "Interview",
      "speakers": ["Andrej Karpathy", "Lex Fridman"],
      "ai_topics": ["LLM OS", "Tokenization", "RLHF"],
      "summary": "...",
      "dialogue_script": [{"speaker": "...", "timestamp": "00:00", "text": "..."}],
      "related_videos": [{"video_id": "...", "title": "...", "reason": "..."}]
    }
  ]
}
```

### 6.3 跨頻道關聯計算

使用 **Jaccard 相似度**（全量標籤矩陣）：

```
相似度 = |A ∩ B| / |A ∪ B|
```

每次有新影片加入後，Python 對所有歷史影片重新全量計算，確保跨越任意時間跨度的關聯都能被發現（解決冷啟動問題）。

---

## 7. 自動化層（GitHub Actions）

### 7.1 主管線（每 6 小時）

```
.github/workflows/update_tracker.yml
```

- `concurrency.group: production_deploy`（排隊鎖，防止 Race Condition）
- `git pull --rebase origin main`（線性合流，防止 Push 衝突）
- 執行前自動更新 yt-dlp 至最新版本

### 7.2 週度 GC（每週日 00:00 UTC）

```
.github/workflows/weekly_gc.yml
```

- 批量驗證所有已追蹤影片的公開狀態（50 個 ID / 次 = 1 點 Quota）
- 發現私享/刪除影片 → 從 data.json 和 processed_videos.json 剔除 → 重算矩陣

---

## 8. 前端

純靜態 `index.html`，直接讀取 `data.json`，部署在 GitHub Pages。

功能：
- 全文搜索（標題、頻道、講者、主題）
- 按頻道 / 講者類型篩選
- 主題矩陣點擊過濾
- 相關影片快速跳轉

---

## 9. 所需環境變量（GitHub Secrets）

| 變量名 | 用途 |
|--------|------|
| `GH_PAT` | GitHub Actions 推送代碼 |
| `YOUTUBE_API_KEY` | YouTube Data API v3 |
| `GROQ_API_KEY` | Groq LLM + Whisper API（可選升級）|

---

## 10. 依賴清單

```
yt-dlp                  # 核心採集引擎
requests                # HTTP 客戶端
google-api-python-client # YouTube API SDK
isodate                 # ISO 8601 時長解析
faster-whisper          # 本地 ASR 轉錄（保底路線）
groq                    # Groq LLM SDK（可選）
```
