# LLM YouTube Landscape Tracker — Technical Report
Author: SUN Xinrui

This project is a **fully automated YouTube LLM domain content tracker** that is triggered every 6 hours via GitHub Actions, automatically scanning the latest videos from 7 top-tier AI/LLM YouTube channels, completing an end-to-end Pipeline of **metadata collection → subtitle transcription → LLM semantic analysis → topic correlation matrix construction**, ultimately outputting an HTML dashboard (`index.html`) that is compatible with both local `file://` browsing and GitHub Pages public hosting.
---

## Table of Contents

- [1. Problem Statement](#1-problem-statement)
- [2. Methodology](#2-methodology)
  - [2.1 Overall System Architecture](#21-overall-system-architecture)
  - [2.2 Step 1: Ingestion Dispatcher & Two-Layer Exception Tree](#22-step-1-ingestion-dispatcher--two-layer-exception-tree)
  - [2.3 Step 2: Heterogeneous Metadata Normalization](#23-step-2-heterogeneous-metadata-normalization)
  - [2.4 Step 3: Three-Level Fallback Transcription + SEGMENT BREAK Hard Boundaries](#24-step-3-three-level-fallback-transcription--segment-break-hard-boundaries)
  - [2.5 Step 4: Map-Reduce LLM Semantic Analysis](#25-step-4-map-reduce-llm-semantic-analysis)
  - [2.6 Step 5: Jaccard Topic Correlation Matrix](#26-step-5-jaccard-topic-correlation-matrix)
  - [2.7 Step 6: Real-Time Write & Frontend Compatibility](#27-step-6-real-time-write--frontend-compatibility)
- [3. Evaluation Dataset](#3-evaluation-dataset)
- [4. Evaluation Methods](#4-evaluation-methods)
- [5. Experimental Results](#5-experimental-results)
- [6. Engineering Hardening Log](#6-engineering-hardening-log)
- [7. Reproducibility](#7-reproducibility)
- [8. File & Directory Structure](#8-file--directory-structure)
- [Appendix A: Core Formula Summary](#appendix-a-core-formula-summary)
- [Appendix B: Dependency List](#appendix-b-dependency-list)

---

## 1. Problem Statement

1. Problem: Using yt-dlp alone: cannot identify videos that were once published but later made private/unlisted/deleted — accessing their links will throw errors; pure scraping without official authorization poses IP ban risks during large-scale long-term operation.
Using YouTube Data API alone: free quota is limited, large-scale collection is costly; cannot download audio/video, cannot obtain raw auto-generated subtitles.
Solution: A hybrid collection architecture with yt-dlp as the primary and YouTube Data API as the secondary.
yt-dlp supports large-volume data collection; can fetch audio files (.m4a) and subtitle files (.vtt/.srt).
YouTube Data API identifies private, restricted, or deleted videos, ensuring the automated system won't attempt to download them; official interface, strong stability, no risk.
Implementation: Two-layer exception tree + exponential backoff + sliding time window.
YtdlpVideoFailed    → single video retry failed
        ↓
3 consecutive failures within 10 minutes
        ↓
YtdlpGlobalBroken  → yt-dlp global fetch failure → immediately switch to API fallback
a) Two-layer exception tree:
Layer 1: YtdlpVideoFailed targets individual video failures: video made private/deleted; occasional network fluctuations.
Only retries the current video; if retry fails → falls back to API for that video (does not affect global state).
Layer 2: YtdlpGlobalBroken targets the entire yt-dlp path failure: IP rate-limited; anti-scraping mechanism triggered.
Directly triggers global fallback → switches entirely to API mode.
b) Exponential Backoff:
Exponential backoff formula: t_wait = min( BACKOFF_BASE ^ attempt + jitter , BACKOFF_MAX ) where `BACKOFF_BASE=2`, `BACKOFF_MAX=30`, `jitter ∈ U(0,1)`
Wait time for each retry = MIN(2^retry_count + a small random jitter, maximum wait cap of 30 seconds)
Rationale: The more failures occur, the slower it retries — exponentially growing wait times cause retry frequency to drop rapidly, avoiding continuous triggering of rate limits; adding uniformly distributed random jitter avoids multiple parallel requests retrying at the exact same moment (Thundering Herd problem). Upper bound truncation prevents infinite growth of wait times.
Time window global fallback:
Time window global fallback formula:
Global fallback = consecutive failures >= 3 AND all failures occurred within 10 minutes
Purpose: Avoids erroneous judgment under occasional errors that are not actual global fetch failures; introduces a 10-minute sliding time window — only dense consecutive failures within the window trigger global fallback, greatly reducing false-switch probability.

2. Problem: How to achieve fast text extraction in speech transcription and text extraction.
Solution: Three-level fallback strategy.
Priority 1: First attempt to download YouTube creator's manually uploaded subtitles `_download_subtitles(prefer_manual=True)`, extremely fast.
Priority 2: If no official subtitles, download YouTube's auto-generated subtitles `_download_subtitles(prefer_manual=False)`, extremely fast.
Priority 3 (last resort): If the first two are of too poor quality or don't exist, use yt-dlp to download low-bitrate audio (.m4a) and call faster-whisper to transcribe into text `_transcribe_with_faster_whisper()`.

3. Problem: Text too long causes LLM to miss content; extracted content is in text form — how to distinguish different speakers and content.
Solution: Map-Reduce Architecture.
Step 1: Semantic chunking.
Method: Semantic-Aware Sliding Window Chunking.
1. First split text into natural segments by `[SEGMENT BREAK @MM:SS]` markers.
2. Fill segments sequentially into a buffer until word count exceeds `chunk_size=1200`.
3. Internally hard-split overly long segments by word count; adjacent chunks retain `overlap=200` words of overlap.
Overlap window formula: chunk_i = words[i×(chunk_size−overlap) : i×(chunk_size−overlap)+chunk_size], where S=1200 (chunk size), O=200 (overlap size).
Rationale:
SEGMENT BREAK priority splitting: Ensures continuous speech segments from the same speaker are not split, preventing the LLM from producing false speaker-switch judgments at chunk boundaries.
Overlap window: Adjacent chunks share 200 words of context, preventing semantic rupture caused by hard truncation at boundaries.
Step 2: Map (structured instruction): Process individual text chunks, extract local speakers and keywords.
Method: Identify all person names mentioned in the text, annotate the speaker for each dialogue line, use `[Unverified Speaker N]` when uncertain, extract 3-5 LLM technical keywords.
Each chunk in the Map phase is processed by independent workers calling the LLM API in parallel.
Step 3: Reduce (global disambiguation): Aggregate all Map results, disambiguate, deduplicate, and generate final summary.
Method: Resolve `[Unverified Speaker N]` to actual names, merge and deduplicate keywords from all chunks, classify the video, generate summary, output structured data ready for database insertion.

4. Problem: API has concurrency limits; sequential processing is too slow, while too-high concurrency will get blocked.
Solution: 429 Safety Net.
t_retry = Retry-After header value (if exists)
t_retry = LLM_RETRY_BASE × 2^(attempt - 1) (otherwise)
Where: LLM_RETRY_BASE = 3, LLM_MAX_RETRIES = 2.
Under normal conditions, 429 should not be triggered; retries serve only as a safety net for handling occasional transient overloads, with a maximum of 2 retries.

5. Noise Problem: When browsing YouTuber homepages, there are numerous Shorts and short videos containing little information and lacking continuous technical context. If we force the LLM to reconstruct scripts from these, the LLM cannot capture the core architecture and will only produce garbage data (Noise), polluting our themes_matrix global topic matrix.
Solution: Adopt "URL pattern + duration" dual filtering.
URL route inspection: Check whether the webpage_url output by yt-dlp matches the *[youtube.com/shorts/](https://youtube.com/shorts/)* pattern.
Duration hard defense: Check whether the duration field is less than or equal to 60 seconds.
If either condition is met, the video is directly tagged as is_shorts: true and circuit-broken at the middleware layer, without triggering subsequent transcription and LLM calls.

6. Problem: Every time the script executes, it would re-download all hundreds of historical videos from the channel, re-run Whisper, and re-consume LLM Tokens.
Solution:
a) processed_videos.json incremental deduplication defense (Skip Mechanism): Records all previously processed video_ids and their corresponding structured features (such as topics). After yt-dlp or YouTube API fetches the latest video list from a channel, the system does not blindly enter the download and transcription phase, but first cross-references with the database and only processes unprocessed videos.
b) Each scheduled trigger (e.g., every 6 hours), if the channel has not released new videos, the Pipeline safely terminates within the first few seconds of the first step; if 1 new video was released, the system only initiates the subsequent download, Whisper, and LLM processes for this single new video — historical videos remain completely unaffected.

7. Problem: GitHub Actions is a stateless, incrementally-triggered environment running every 6 hours. If each execution only feeds "today's 1 new video" together with "the latest 20 old videos" to the LLM, then this new video can never establish associations with historical hits from 3 months ago, causing severe historical disconnection in the recommendation system.
Solution: Jaccard Matrix "Global Incremental Rolling Computation".
To solve this problem under a zero-budget architecture without a Vector Database, the project abandons the "dynamically feed 20 items to LLM" local approach, and instead adopts a strategy of global tag pool + offline Jaccard matrix rolling update.
Implementation steps:
a) State Persistence: In the videos array of data.json (i.e., output_payload), persistently retain the refined tags extracted by LLM for every video (i.e., the `ai_topics` field, e.g., ["RAG", "GraphDB", "LlamaIndex"]). Meanwhile, processed_videos.json is only responsible for recording incremental deduplication state (video_id → status).
b) Full Load & Incremental Injection: When GitHub Actions executes: reads the historical processed_videos.json (containing all old videos from 3 months ago, half a year ago, with total count $N$). Pipeline fetches today's 1 new video, calls Moonshot LLM to generate topics_new only for this single video. Injects this new video and its tags into the total list; at this point, total video count becomes $N+1$.
c) Global Jaccard Matrix Recomputation (ultra-lightweight): In the final stage of the Python script (Payload construction phase), no API is called; instead, it directly uses CPU to run a double loop, computing Jaccard similarity between the new video and all historical videos: J(A, B) = |A∩B|/|A∪B| (intersection divided by union). Since tags are already refined string sets (only 3-5 tags per video), even with thousands of historical videos, performing 1 × N set intersection/union operations in Python takes only milliseconds.
d) Dynamic Top-3 Truncation: Sort the computed results in descending order, filter out videos with $J > 0.1$ and the highest 3 similarities (regardless of whether it's from 3 days ago or 3 months ago), and write them into the new video's related_videos field. Simultaneously, this new video's tags also trigger reverse updates to old videos' related_videos (bidirectional association).

8. Problem: Using vectors / LLM computation alone is too heavy — GitHub Actions cannot handle it; having LLM or vector models compare against the entire database every time is slow and costly.
Solution: Full matrix algorithm with themes_matrix (global) and related_videos (local) dual-layer structure.
Layer 1: Topic → Video Mapping (Inverted Index): themes_matrix (macro/global association: topic-to-channel matrix)
Implementation: Global tag inverted index.
Logic: The system traverses all videos, extracts all unique topic tags that have appeared, and for each tag builds a list containing all related videos (video_id, channel, title), ultimately forming a `Dict[topic_str, List[{video_id, channel, title}]]` inverted index.
Purpose: Provides the frontend Dashboard with a clickable "Topic Tag Cloud" (Topic Chip Grid); clicking any tag links and filters all videos related to that topic.
Layer 2: Video → Related Video Chain (Pairwise Jaccard): related_videos (micro/local association: extended recommendations for a single video)
Implementation: Jaccard similarity.
Logic: It is a property within a Video object, recording the 3 most related video_ids to the current video, for the frontend to display "You might also like" in a sidebar or popup when a user clicks a video.
Jaccard similarity formula: J(A,B) = |A∩B| / |A∪B|
Jaccard similarity = size of intersection of two tag sets ÷ size of union of two tag sets (closer to 1 = more similar, closer to 0 = more unrelated).
Only when the similarity between two videos is greater than 0.1 (J(Ai,Aj)>0.1 and i≠j) are the two videos considered related — as long as one tag is the same, they are deemed related.
data.json specification and complete structure:
To balance the frontend's "zero-framework, single-file double-click to open" minimalist design, the Pipeline ultimately outputs a standard JSON structure (or a JS file wrapped in `window.__TRACKER_DATA__ = {...};`). This structure comprehensively contains both the global topic matrix and each video's local associations.

---

## 2. Methodology

### 2.1 Overall System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        main.py — Orchestrator                                │
│                                                                             │
│  ┌──────────┐    ┌──────────┐    ┌───────────┐    ┌──────────┐    ┌───────┐ │
│  │ Step 1   │───▶│ Step 2   │───▶│ Step 3   │───▶│ Step 4 │───▶│Step 5 │ │
│  │ Data     │    │ Data     │    │ Subtitle  │    │ LLM    │    │Correl.│ │
│  │Collection│    │Normalize │    │Transcribe │    │Analysis│    │Matrix │ │
│  │Ingestion │    │Transformer│    │Transcribe │    │MapReduce │   │Jaccard│ │
│  └──────────┘    └──────────┘    └───────────┘    └──────────┘    └───────┘ │
│       │                                                               │     │
│       ▼                                                               ▼     │
│  QuotaGuard                                                    data.js      │
│  (API Quota Guard)                                       (Frontend Write)   │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
                              ┌─────────────┐
                              │ index.html  │
                              │Static Board │
                              └─────────────┘
```

Technology Stack Overview:

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Language | Python 3.10+ | Full Pipeline flow |
| Data Collection | `yt-dlp` + YouTube Data API v3 | Channel scanning, metadata fetching, subtitle download |
| Speech Transcription | `faster-whisper` (CTranslate2 + Whisper) | Local ASR fallback transcription |
| LLM Semantic Analysis | **Moonshot (Kimi) API** (`moonshot-v1-8k` / `moonshot-v1-32k`) | Map-Reduce dialogue script reconstruction |
| Correlation Computation | Jaccard Similarity (pure Python) | Video topic correlation matrix |
| Frontend Display | Native HTML + CSS + JavaScript (zero framework) | Static dashboard |
| CI/CD | GitHub Actions | Incremental tracking every 6h + weekly Sunday GC dead link cleanup |
| Data Persistence | JSON (`data.json` / `data.js` / `processed_videos.json`) | GitOps state management |

### 2.2 Step 1: Ingestion Dispatcher & Two-Layer Exception Tree

`core/ingestion.py`'s `IngestionDispatcher` adopts a **"primary route yt-dlp + quota-based API fallback"** dual-route design, achieving fine-grained response to IP-level anti-scraping through a **two-layer exception tree**:

```
YtdlpVideoFailed     ← Single video retry exhausted (fall back to API for that video)
    │
    └─▶ N consecutive → YtdlpGlobalBroken  ← Global fallback to API-only mode
```

**Exponential Backoff** (no wait on first attempt, on retry):

$$
t_{\text{wait}} = \min\bigl(\text{BACKOFF\_BASE}^{\text{attempt}} + \text{jitter},\; \text{BACKOFF\_MAX}\bigr)
$$

`BACKOFF_BASE = 2`, `BACKOFF_MAX = 30s`, `jitter ∈ U(0,1)` — exponential growth + jitter to prevent Thundering Herd synchronized retries.

**Time Window Global Fallback**:

$$
\text{trigger} \iff F \geq \text{GLOBAL\_FAIL\_THRESH} \;\wedge\; (t_{\text{now}} - t_{\text{first\_fail}}) \leq \text{GLOBAL\_FAIL\_WINDOW}
$$

`GLOBAL_FAIL_THRESH = 3`, `GLOBAL_FAIL_WINDOW = 600s`. Meaning: **only when dense consecutive failures occur within a 10-minute window** is it considered that the IP has been precisely blocked; sporadic failures across the window boundary will reset the counter, avoiding false positives.

**Additional Data Robustness**:

- `_extract_video_id()` uses the `^[A-Za-z0-9_-]{11}$` regex for strict validation, preventing 24-character channel_ids from channels/playlists from polluting `processed_videos.json` (result of historical bug remediation, see §6).
- `_get_channel_videos_ytdlp()` forcefully appends the `/videos` subpath, preventing yt-dlp from returning tabs/sub-playlist items.

### 2.3 Step 2: Heterogeneous Metadata Normalization

`core/transformer.py` uniformly cleanses the two **structurally completely different** raw JSONs from `yt-dlp` and YouTube API v3 into the following frontend-consumable schema:

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

Defensive coding: All `dict.get(...)` return values use `or` fallback (e.g., `raw_data.get("subtitles") or {}`), preventing chained `TypeError` when external APIs return `None`. Time retrieval uniformly uses UTC-aware `datetime.now(timezone.utc)`, migrated from the deprecated `datetime.utcnow()`.

### 2.4 Step 3: Three-Level Fallback Transcription + SEGMENT BREAK Hard Boundaries

`core/transcription.py` adopts a **zero-cost priority** three-level fallback:

| Priority | Method | API Cost | Speed | Description |
|----------|--------|----------|-------|-------------|
| 1 | `_download_subtitles(prefer_manual=True)` | **Zero** | Fastest | YouTube manually uploaded subtitles |
| 2 | `_download_subtitles(prefer_manual=False)` | **Zero** | Fast | YouTube auto-generated subtitles |
| 3 | `_transcribe_with_faster_whisper()` | **Zero** (local CPU) | Slow | faster-whisper local ASR fallback |

**SEGMENT BREAK Hard Boundary Injection**:

$$
\text{insert} \iff t_{\text{current}} - t_{\text{previous}} \geq \Delta_{\text{gap}} \quad (\Delta_{\text{gap}} = 30\text{s})
$$

YouTube auto-generated subtitles are unsegmented continuous streams; in multi-speaker dialogue scenarios, the LLM cannot determine speaker-switch boundaries. Forcefully inserting `[SEGMENT BREAK @MM:SS]` anchors at points where adjacent subtitle time gaps ≥30 seconds significantly reduces "false speaker switches" by the LLM at chunk boundaries.

faster-whisper (fallback) configuration: `base` model (74M parameters), CTranslate2 backend, INT8 quantization (memory −50%, inference speed ×2), `beam_size=5` Beam Search decoding:

$$
\hat{y} = \arg\max_{y} \sum_{t=1}^{T} \log P(y_t \mid y_{<t}, X)
$$

### 2.5 Step 4: Map-Reduce LLM Semantic Analysis

`core/map_reduce_engine.py` is the project's core intelligent component. It transforms subtitle long-text streams into structured results of speakers, dialogue scripts, topic keywords, and summaries.

**Chunking Strategy — Semantic-Aware Sliding Window**:

1. First split by `[SEGMENT BREAK @MM:SS]` into natural segments;
2. Fill sequentially into a buffer until word count exceeds `chunk_size = 1200`;
3. Internally hard-split overly long segments by word; adjacent chunks retain `overlap = 200` words:

$$
\text{chunk}_i = \text{words}\bigl[i(S-O) \;:\; i(S-O)+S\bigr] \quad (S=1200,\; O=200)
$$

**Map-Reduce Parallel Architecture**:

```
           ┌─────────────────────────────────────────────┐
           │           Raw Subtitle Text Stream           │
           └────────────────┬────────────────────────────┘
                            │
                     _chunk_text()
               (SEGMENT BREAK Semantic Chunking)
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
      ┌──────────┐   ┌──────────┐   ┌──────────┐
      │ Chunk 1  │   │ Chunk 2  │   │ Chunk 3  │   ← ThreadPoolExecutor
      │ Map 8k   │   │ Map 8k   │   │ Map 8k   │      max_workers=3
      └────┬─────┘   └────┬─────┘   └────┬─────┘      Parallel dispatch
           └──────────────┼──────────────┘
                          ▼
                  ┌──────────────┐
                  │  Reduce 32k  │  ← Single call, global disambiguation+classification+summary
                  └──────────────┘
```

| Phase | Model | Context | Task |
|-------|-------|---------|------|
| Map | `moonshot-v1-8k` | 8 K | Local speaker annotation + keyword extraction (parallel ×3) |
| Reduce | `moonshot-v1-32k` | 32 K | Cross-segment speaker disambiguation + topic dedup + video classification + summary |

**Quota Alignment**: `ThreadPoolExecutor(max_workers=3)` precisely matches the Moonshot account concurrency quota of 3; under normal conditions, 429 will not be triggered. A 2-retry safety net is also retained, prioritizing the server-side `Retry-After`:

$$
t_{\text{retry}} =
\begin{cases}
\text{Retry-After header} & \text{if header exists} \\
\text{LLM\_RETRY\_BASE} \cdot 2^{(a-1)} & \text{otherwise}
\end{cases}
$$

**Body Size Defense**: `MAX_USER_CONTENT_CHARS = 28000` truncates single-request user content; `MAX_MAP_SNIPPET_CHARS = 4000` truncates each Map result concatenated in the Reduce phase, fundamentally preventing the HTTP 413 (body too large) issues that frequently occurred during the Groq era (see §6 Engineering Hardening).

**LLM Output Strong Constraints**: `temperature=0.1`, `response_format={"type":"json_object"}`, `timeout=60s`; on the parsing side, `_parse_json_safe()` strips markdown code fences then `json.loads`; on exception, returns empty dict and falls through to `_fallback()`.

### 2.6 Step 5: Jaccard Topic Correlation Matrix

`core/graph_matrix.py` adopts **zero-dependency, zero-vectorization** Jaccard IoU:

$$
J(A, B) = \frac{|A \cap B|}{|A \cup B|}, \quad A, B \subseteq \text{ai\_topics}
$$

Dual-layer output structure:

```
Layer 1 — Topic → Video Inverted Index:
  themes_matrix = { "Transformer": [v1, v4, v7], "RAG": [v2, v3], ... }

Layer 2 — Video → Related Video Chain (Pairwise Jaccard, threshold 0.1, Top-3):
  v1.related_videos = [{v4, 0.67}, {v7, 0.40}, {v2, 0.20}]
```

Time complexity \(O(n^2 k)\); at current scale \(n \leq 70\), \(k \leq 5\), computational cost is negligible. Choosing Jaccard over cosine/embedding is an engineering trade-off: on 2–5 discrete topic tags refined by LLM, vectorization would introduce noise rather than reduce it.

### 2.7 Step 6: Real-Time Write & Frontend Compatibility

After processing each video, `save_data_js()` is called to write simultaneously:

- `data.json`: Standard JSON, for program and GC script reading;
- `data.js`: `window.__TRACKER_DATA__ = {...};`, for `index.html` to load via `<script src>`.

**Why write two copies**: `fetch("data.json")` is blocked by browser CORS under the `file://` protocol. Writing a `.js` file that injects a global variable allows users to **double-click** `index.html` to directly view the latest results, while maintaining full compatibility with GitHub Pages `https://` access.

---

## 3. Evaluation Dataset

Evaluation data comes from real YouTube video data continuously generated by the Pipeline itself running in the production environment. This project does not have an "offline annotated test set"; instead, it uses **real production traffic** as the continuous evaluation target, accumulating with each 6-hour scheduling cycle.

| Item | Configuration |
|------|---------------|
| Tracked channel count | **7** (see table below) |
| Per-channel per-round fetch limit | 10 most recent videos |
| Pipeline trigger frequency | Every 6 hours + manual |
| Incremental deduplication key | 11-character `video_id` in `processed_videos.json` |
| Shorts filtering rule | `/shorts/` path ∨ duration ≤ 60s |
| Data snapshot location | `data.json` / `data.js` (committed back to Git with each CI run) |

**Tracked Channel List** (`config/settings.py::TARGET_CHANNELS`):

| Channel | Channel ID |
|---------|------------|
| Andrej Karpathy | `UCXUPKJO5MZQN11PqgIvyuvQ` |
| 3Blue1Brown | `UCYO_jab_esuFRV4b17AJtAw` |
| Yannic Kilcher | `UCZHmQk67mSJgfCCTn7xBfew` |
| Two Minute Papers | `UCbfYPyITQ-7l4upoX8nvctg` |
| AI Explained | `UCNJ1Ymd5yFuUPtn21xtRbbw` |
| Sam Witteveen AI | `UC55ODQSvARtgSyc8ThfiepQ` |
| IBM Technology | `UCKWaEZ-_VweaEx1j62do_vQ` |

---

## 4. Evaluation Methods

Evaluation is divided into three categories: **Unit Testing (Correctness)**, **Production Runtime Metrics (Engineering)**, **LLM Output Structural Compliance (Semantic Quality)**.

### 4.1 Unit Testing (`tests/`)

| Test Module | Coverage Target |
|-------------|-----------------|
| `test_extract_video_id.py` | 11-char `video_id` regex validation, 24-char `channel_id` rejection, empty input fallback |
| `test_chunk_text.py` | `_chunk_text` slice correctness at SEGMENT BREAK / overlap boundaries |
| `test_io_helpers.py` | `load_json` fault-tolerant paths, `save_json` write, `save_data_js` dual-write consistency |
| `test_transformer.py` | yt-dlp / api_v3 dual-path normalization result field completeness, null value fallback |

CI is enforced as a PR mandatory gate in `.github/workflows/tests.yml`.

### 4.2 Production Runtime Metrics (Engineering)

Uniformly output by `utils/logger.py`; each CI round's logs and `quota_state.json` serve as real data sources:

| Metric | Observation Method | Expected |
|--------|--------------------|----------|
| YouTube API daily quota consumption | `quota_state.json::used` | < 100 / 10000 |
| `yt-dlp` average retry count per video | Ingestion logger | < 1.5 (steady state) |
| `YtdlpGlobalBroken` trigger frequency | Ingestion logger | Monthly ≤ 1 time |
| Subtitle hit rate (Step 3 Priority 1+2 success ratio) | Transcription logger | ≥ 80% |
| faster-whisper fallback rate | `transcription_source == faster_whisper` | ≤ 20% |
| Map-Reduce average parallelism | chunk count / serial time estimate in logs | ≈ 3× |
| LLM 429 retry trigger rate | MapReduce logger | ≤ 1% |

### 4.3 LLM Output Structural Compliance (Semantic Quality)

`_parse_json_safe()` performs schema-level implicit validation on Reduce results:

- **Required fields**: `speaker_type`, `speakers`, `ai_topics`, `summary`;
- **Exportable fields**: `dialogue_script` (top-120 dialogue lines retained after deduplication);
- On parse failure → `_fallback("reduce_failed")` writes safe default values; Pipeline **does not halt**.

"Good enough" standard: Through manual sampling, `speaker_type` tri-classification (Solo / Interview / Group) matches actual video situations; `ai_topics` has explainable semantic overlap with video titles/descriptions; summaries are 2–3 sentence natural language paragraphs.

---

## 5. Experimental Results

The table below shows **cumulative-to-date** real operational results (continuously refreshed; `data.json` in the repository is the source of truth):

### 5.1 Data Aspects

| Item | Observed Value |
|------|----------------|
| `data.json::videos[].length` | Monotonically increasing over time (incremental collection) |
| Topic inverted index `themes_matrix` scale | ~30–80 unique LLM topic tags |
| Average `related_videos` entries per video | ≤ 3 (Top-K truncation) |
| `processed_videos.json` scale | Contains three status key types: `ok` / `filtered_shorts` / `fetch_failed` |

### 5.2 Engineering Aspects

| Item | Observed Value | Target | Achieved |
|------|----------------|--------|----------|
| YouTube API daily average consumption | Single-digit quota points | < 100 | ✅ |
| LLM entirely on Moonshot free quota | RPM ≤ 20, concurrency ≤ 3 | Compliant | ✅ |
| Double-click `index.html` accessible | data.js injection | ✅ | ✅ |
| Pipeline single-point-of-failure resilience | API/yt-dlp/subtitles/whisper any service down has fallback | ✅ | ✅ |
| Incremental dedup correctness | `video_id` regex validation, no channel_id pollution | ✅ | ✅ |

### 5.3 Frontend Aspects

`index.html` provides:

- **Topic Tag Cloud** (sorted by occurrence frequency, clickable with linked filtering)
- **Video Master Table**: title, channel, publish date, speaker type, speakers, AI topics, AI summary, related videos
- **Multi-dimensional Filtering**: keyword search × channel filter × speaker type filter × topic tag filter

---

## 6. Engineering Hardening Log

The table below lists **implemented** key hardening measures and bug fixes during project evolution; each item has a corresponding implementation in the code:

| # | Problem | Impact | Fix |
|---|---------|--------|-----|
| 1 | yt-dlp channel list returns 24-char channel_id polluting `processed_videos.json` | Incremental dedup fails; channel permanently skipped thereafter | `_extract_video_id` + `_VIDEO_ID_RE` strict regex validation |
| 2 | External API occasionally returns `None` fields, `dict.get()` chained `TypeError` | Pipeline crash | Full-chain `or {}` / `or []` fallback |
| 3 | Groq-era HTTP 413 (body too large) frequent | LLM phase direct failure | `MAX_USER_CONTENT_CHARS=28000` + `MAX_MAP_SNIPPET_CHARS=4000` + migration to Moonshot |
| 4 | Groq free API 429 with insufficient cooldown | Cascading failures | 429 safety net + quota-aligned `ThreadPoolExecutor(max_workers=3)` |
| 5 | JSON structure validation missing causing startup blocking | Crash on startup | `utils/io_helpers.load_json` with type validation + `_load` field `setdefault` fallback |
| 6 | `_parse_json_safe` mistakenly written as unclosed raw string `r'rtrep` | basedpyright reports "unterminated string"; Reduce results always fall through to fallback | Changed back to `return json.loads(clean)` |
| 7 | `fetch_metadata` type annotation written as `Dict` but actually returns 2-tuple | Type checker red line | Changed to `Tuple[Dict[str, Any], str]` |
| 8 | `core/transformer.py` still uses `datetime.utcnow()` | Python 3.12+ DeprecationWarning, will be removed in future | Extracted `_today_utc()` using `datetime.now(timezone.utc)` |
| 9 | `_inject_segment_breaks` / `_chunk_text` time regex `\d{2}` | SEGMENT BREAK completely fails for videos ≥ 100 minutes | Both regex patterns changed to `\d{1,3}` |
| 10 | `QuotaGuard._load` doesn't validate field completeness | KeyError when `quota_state.json` is incomplete | Added top-level dict validation + `setdefault("used", 0)` fallback |
| 11 | `gc_cleanup.py` uses `load_json(..., dict)` degrades to `{}` | Structure inconsistency risk | Default schema aligned with `main.py` |

---

## 7. Reproducibility

### 7.1 Local Execution

```bash
# 1. Clone
git clone https://github.com/<your-account>/llm-youtube-landscape-tracker.git
cd llm-youtube-landscape-tracker

# 2. Install dependencies (Python 3.10+ recommended)
python -m pip install -r requirements.txt

# 3. Set environment variables (can also hardcode in config/settings.py, but env recommended)
$env:YOUTUBE_API_KEY  = "<your YouTube Data API v3 key>"
$env:MOONSHOT_API_KEY = "<your Moonshot/Kimi API key>"

# 4. Run Pipeline
python main.py

# 5. Double-click index.html to view results (depends on auto-generated data.js)
```

> **PowerShell Note**: Use semicolons `;` to chain commands; do not use `&&`.

### 7.2 CI / CD (GitHub Actions)

| Workflow | Trigger | Function |
|----------|---------|----------|
| `.github/workflows/tracker.yml` | Every 6h / manual | Main Pipeline: collect+transcribe+analyze+write+Git Push |
| `.github/workflows/update_tracker.yml` | Every 6h / manual | Backup version (uses GH_PAT) |
| `.github/workflows/gc_cleanup.yml` | Every Sunday 03:00 UTC / manual | Dead link cleanup + matrix rebuild |
| `.github/workflows/weekly_gc.yml` | Every Sunday 00:00 UTC / manual | Backup GC |
| `.github/workflows/tests.yml` | PR / push | Unit test gate |

All write-type workflows share a `concurrency group`, combined with `git pull --rebase` to ensure linear safe merging of JSON data files.

### 7.3 GitHub Pages Deployment

Enable in repository *Settings → Pages → Source: main / root*; access `https://<account>.github.io/llm-youtube-landscape-tracker/`. No build step needed; static files served directly.

---

## 8. File & Directory Structure

```
.
├── main.py                       # Orchestrator
├── gc_cleanup.py                 # Weekly dead link cleanup
├── config/
│   └── settings.py               # Channel list / API keys / rate limit params
├── core/
│   ├── ingestion.py              # Ingestion dispatcher (two-layer exception tree + exponential backoff)
│   ├── transformer.py            # yt-dlp / api_v3 heterogeneous structure normalization
│   ├── transcription.py          # Three-level fallback transcription + SEGMENT BREAK
│   ├── map_reduce_engine.py      # Map-Reduce LLM engine (Moonshot)
│   ├── graph_matrix.py           # Jaccard topic correlation matrix
│   └── quota_guard.py            # YouTube API quota guard
├── utils/
│   ├── io_helpers.py             # Fault-tolerant JSON I/O + data.js injection
│   └── logger.py                 # Unified formatted logging
├── tests/                        # Unit tests
├── data.json / data.js           # Pipeline output (frontend data source)
├── processed_videos.json         # Incremental deduplication state store
├── quota_state.json              # API quota counter persistence
├── index.html                    # Zero-framework static dashboard
├── DESIGN.md                     # System design document
└── README.md                     # This report
```

---

## Appendix A: Core Formula Summary

| # | Formula | Purpose | Location |
|---|---------|---------|----------|
| 1 | t=min(B^a+U(0,1),T max) | Exponential backoff retry | `core/ingestion.py` |
| 2 | trigger⟺F≥N∧Δt≤W | Time window global fallback | `core/ingestion.py` |
| 3 | insert⟺ti−ti−1≥30s | SEGMENT BREAK hard boundary | `core/transcription.py` |
| 4 | y=arg max ∑ t logP(yt∣y <t,X) | Beam Search decoding | faster-whisper |
| 5 | chunk i=words[ i(S−O):i(S−O)+S] | Sliding window chunking | `core/map_reduce_engine.py` |
| 6 | J(A,B) = |A∩B| / |A∪B| | Jaccard similarity | `core/graph_matrix.py` |
| 7 | t retry=B⋅2^(a−1) | LLM 429 backoff | `core/map_reduce_engine.py` |

---

## Appendix B: Dependency List

| Package | Purpose |
|---------|---------|
| `yt-dlp` | YouTube video/subtitle/audio download |
| `requests` | HTTP client (Moonshot API + YouTube API) |
| `curl_cffi` | Anti-scraping disguised HTTP client (yt-dlp companion) |
| `google-api-python-client` | YouTube Data API v3 SDK |
| `isodate` | ISO 8601 duration parsing |
| `faster-whisper` | Local Whisper ASR (CTranslate2 engine) |

---

## License

MIT (see [`LICENSE`](./LICENSE)).
