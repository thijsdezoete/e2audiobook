# CWA-Narrator: Final Project Spec

## What This Is

A Docker service that converts your Calibre ebook library into M4B audiobooks using local GPU-accelerated TTS, outputting directly into your Audiobookshelf library for playback via Plappa.

---

## Your Stack (As Deployed via Dokploy)

```
┌─────────────────────────────────────────────────────────────────────┐
│  Dokploy / Docker Compose                                           │
│                                                                     │
│  calibre-web-automated ──► calibre-library-local (Docker volume)    │
│       :8083                       │                                 │
│                                   │ rsync (every 5 min)             │
│  cwa-book-downloader              ▼                                 │
│       :8084              /mnt/nas/media/books  ◄── CWA-Narrator     │
│                                                     reads from here │
│  audiobookshelf                                                     │
│       :13378 ◄── /mnt/nas/media/audiobooks  ◄── CWA-Narrator       │
│                                                   writes here       │
│  library-syncer (rsync)                                             │
│  book-mover (staging → ingest)                                      │
│                                                                     │
│  ┌─────────── NEW ──────────────────────────────┐                   │
│  │  kokoro-tts         (GPU, :8880)             │                   │
│  │  cwa-narrator       (orchestrator, :8585)    │                   │
│  └──────────────────────────────────────────────┘                   │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Paths

| What | Path | Access |
|------|------|--------|
| Calibre library (synced copy) | `/mnt/nas/media/books` | Read |
| Audiobookshelf audiobooks | `/mnt/nas/media/audiobooks` | Write |

**Why read from the NAS copy, not the Docker volume?**
The `calibre-library-local` volume is CWA's live workspace. The rsync copy at `/mnt/nas/media/books` is at most 5 minutes stale — irrelevant when conversions take 45+ minutes. Zero coupling, zero risk to CWA.

---

## Locked-In Decisions

| Decision | Choice |
|----------|--------|
| TTS Model | Kokoro-82M (82M params, FP32, Apache 2.0) |
| Default Voice | `af_heart` (Grade A) |
| Voice Roster | 2F: `af_heart`, `af_bella` · 2M: `am_fenrir`, `am_michael` |
| Output Format | Single M4B per book with embedded chapter markers |
| Input Formats | EPUB / KEPUB only |
| Conversion Mode | Selective per-book with batch "convert all" option |
| Library Source | `/mnt/nas/media/books` (NAS rsync copy) |
| Output Target | `/mnt/nas/media/audiobooks` (Audiobookshelf watched dir) |

### Voice Roster

| Voice ID | Gender | Grade | Role |
|----------|--------|-------|------|
| `af_heart` | Female | **A** | Default |
| `af_bella` | Female | **A-** | Alt female |
| `am_fenrir` | Male | **C+** | Default male |
| `am_michael` | Male | **C+** | Alt male |

---

## Pipeline: Book → Audiobook

```
 1. SELECT BOOK
    └─ Via web UI or API

 2. EXTRACT TEXT
    ├─ Parse EPUB/KEPUB via ebooklib
    ├─ Detect chapters:
    │   ① EPUB TOC nav / NCX
    │   ② <h1>/<h2> headings
    │   ③ Regex: "Chapter \d+", "PART [IVX]+"
    │   ④ Fallback: split every ~5,000 words
    └─ Strip HTML → clean text per chapter

 3. SYNTHESIZE AUDIO
    ├─ Split chapters into ≤450 token chunks at sentence boundaries
    ├─ POST each chunk to Kokoro-FastAPI /v1/audio/speech
    ├─ Concatenate chunks per chapter (50ms crossfade)
    └─ Output: WAV per chapter

 4. BUILD M4B
    ├─ Encode WAVs → AAC via ffmpeg
    ├─ Merge into single M4B with chapter markers
    ├─ Embed metadata + cover art
    └─ Output: {Title}.m4b

 5. OUTPUT
    ├─ Write to /audiobooks/{Author}/{Title}/
    │   ├── {Title}.m4b
    │   ├── cover.jpg
    │   ├── desc.txt
    │   └── reader.txt
    └─ Trigger Audiobookshelf library scan
```

---

## Docker Compose (Additions to Your Stack)

```yaml
  kokoro-tts:
    image: ghcr.io/remsky/kokoro-fastapi-gpu:v0.2.1
    container_name: kokoro-tts
    environment:
      - TZ=Europe/Amsterdam
    ports:
      - "8880:8880"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    volumes:
      - kokoro-models:/app/models
    restart: unless-stopped

  cwa-narrator:
    build: ./cwa-narrator
    container_name: cwa-narrator
    environment:
      - TZ=Europe/Amsterdam
      - PUID=1000
      - PGID=1000
      - TTS_API_URL=http://kokoro-tts:8880
      - CALIBRE_LIBRARY_PATH=/calibre-library
      - AUDIOBOOK_OUTPUT_PATH=/audiobooks
      - DEFAULT_VOICE=af_heart
      - TTS_SPEED=1.0
      - ABS_API_URL=http://audiobookshelf:80
      - ABS_API_TOKEN=
      - AUTO_CONVERT=false
      - NARRATOR_PORT=8585
    volumes:
      - /mnt/nas/media/books:/calibre-library:ro
      - /mnt/nas/media/audiobooks:/audiobooks
      - narrator-data:/app/data
    ports:
      - "8585:8585"
    depends_on:
      - kokoro-tts
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8585/api/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
```

Additional volumes:
```yaml
  kokoro-models:
  narrator-data:
```

---

## Database Schema

```sql
CREATE TABLE jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    calibre_book_id INTEGER UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL,
    series          TEXT,
    series_index    REAL,
    voice           TEXT NOT NULL DEFAULT 'af_heart',
    status          TEXT NOT NULL DEFAULT 'pending',
    chapters_total  INTEGER DEFAULT 0,
    chapters_done   INTEGER DEFAULT 0,
    error_message   TEXT,
    epub_path       TEXT,
    output_path     TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Populated on first run with defaults from env vars.
-- Web UI edits write here; these override env vars at runtime.
```

---

## Implementation Phases

---

### Phase 1: Proof of Concept

**Goal:** One EPUB in, one M4B out. Single standalone Python script, run manually, validates the entire conversion pipeline end-to-end.

**Prerequisites (verified):**
- Kokoro-FastAPI running on GTX 1650 with GPU acceleration ✓
- `af_heart` voice available and generating quality audio ✓
- API endpoint confirmed: `POST /v1/audio/speech` with WAV output ✓

**Not built yet:** Docker image, web UI, job queue, config system, background services, error recovery.

---

#### Script: `poc.py`

A single self-contained Python script. No module structure, no imports from a package — just one file that does everything. Run with:

```bash
python poc.py /path/to/book.epub --tts-url http://localhost:11880 --voice af_heart --output ./output
```

**Arguments:**
- `epub_path` (positional): path to an EPUB or KEPUB file
- `--tts-url`: Kokoro-FastAPI base URL (default: `http://localhost:11880`)
- `--voice`: voice ID (default: `af_heart`)
- `--output`: output directory (default: `./output`)
- `--serve`: after conversion, start a local HTTP server to play the result (default: true)
- `--port`: HTTP server port (default: `8590`)

---

**Step 1: Parse EPUB**

```
Input:  /path/to/book.epub
Output: metadata dict + ordered list of (chapter_title, chapter_text)
```

- Open EPUB with `ebooklib`
- Extract metadata: title, author, language, publisher, date, description
- Extract cover image: check metadata for cover reference, fall back to first image in spine
- Detect chapters using the 4-level fallback chain:
  1. EPUB TOC (nav document for EPUB3, NCX for EPUB2): map TOC entries to spine items, extract text per entry
  2. HTML headings: scan each spine item for `<h1>`/`<h2>`, split at those boundaries
  3. Regex patterns: scan text for "Chapter \d+", "CHAPTER \d+", "Part \w+", "PART \w+"
  4. Fixed split: divide total text into chunks of ~5,000 words at paragraph boundaries
- For each chapter:
  - Get the HTML content from the spine item(s)
  - Strip all HTML tags with BeautifulSoup (get_text with separator='\n')
  - Normalize whitespace: collapse multiple newlines to double, strip leading/trailing
  - Skip chapters with < 50 words (likely blank pages, copyright, etc.)
- If KEPUB: before text extraction, strip Kobo-specific `<span class="koboSpan">` wrappers
- Output to console: "Found {N} chapters: {list of titles with word counts}"

**Edge cases to handle in PoC:**
- EPUB with no TOC → fall through to headings/regex/fixed
- Single-spine-item EPUB (entire book in one HTML file) → heading/regex detection works within the single document
- Chapters with only images → skip (< 50 words after text extraction)

---

**Step 2: Chunk Text for TTS**

```
Input:  chapter_text (string, potentially thousands of words)
Output: list of text chunks, each ≤ 450 tokens
```

- Tokenize into sentences using `nltk.sent_tokenize()`
- Estimate token count per sentence: `len(sentence) / 3.5` (rough chars-to-tokens for English)
- Accumulate sentences into a chunk until adding the next sentence would exceed 450 tokens
- Flush the chunk, start a new one
- Never split mid-sentence
- If a single sentence exceeds 450 tokens (rare — very long sentences): split at the last comma or semicolon before 400 tokens, or as a last resort, split at 400 tokens at a word boundary
- Output: list of strings, each ready to send to Kokoro

---

**Step 3: Synthesize Audio**

```
Input:  list of text chunks per chapter
Output: one WAV file per chapter in a temp directory
```

For each chapter:
- For each chunk in the chapter:
  - POST to `{tts_url}/v1/audio/speech`:
    ```json
    {
      "model": "kokoro",
      "input": "<chunk text>",
      "voice": "af_heart",
      "response_format": "wav"
    }
    ```
  - Receive WAV bytes
  - On HTTP error: retry up to 3 times with 2/4/8 second backoff, then abort the script with error
  - Print progress: "Chapter {n}/{total} — Chunk {i}/{count}"
- Concatenate all chunk WAVs for this chapter into a single chapter WAV:
  - Load each chunk with `pydub.AudioSegment.from_wav()`
  - Append with 50ms crossfade: `combined = combined.append(chunk, crossfade=50)`
  - Export to `{temp_dir}/chapter_{nn}.wav`
- After all chapters: print total audio duration

---

**Step 4: Build M4B**

```
Input:  chapter WAV files + metadata + cover image
Output: single .m4b file with chapter markers
```

All ffmpeg operations via `subprocess.run()`.

**4a. Encode chapters to AAC:**
For each chapter WAV:
```bash
ffmpeg -i chapter_01.wav -c:a aac -b:a 128k chapter_01.m4a
```

**4b. Create concat file:**
```
file 'chapter_01.m4a'
file 'chapter_02.m4a'
...
```

**4c. Concatenate:**
```bash
ffmpeg -f concat -safe 0 -i concat.txt -c copy combined.m4a
```

**4d. Generate chapter metadata file (`ffmetadata.txt`):**
Compute chapter timestamps from actual audio duration of each chapter file (via `ffprobe`).
```ini
;FFMETADATA1
title=Book Title
artist=Author Name
album=Book Title
genre=Audiobook
date=2024

[CHAPTER]
TIMEBASE=1/1000
START=0
END=1234567
title=Chapter 1: The Beginning

[CHAPTER]
TIMEBASE=1/1000
START=1234567
END=2345678
title=Chapter 2: The Journey
```

**4e. Apply metadata + cover:**
```bash
ffmpeg -i combined.m4a -i cover.jpg -i ffmetadata.txt \
  -map 0:a -map 1:v -map_metadata 2 \
  -c:a copy -c:v mjpeg -disposition:v attached_pic \
  -movflags +faststart \
  "Book Title.m4b"
```

**4f. Validate output:**
- File exists and > 0 bytes
- `ffprobe` confirms expected chapter count
- Print: file size, total duration, chapter count

---

**Step 5: Output & Serve**

```
Input:  M4B file + metadata
Output: Audiobookshelf-ready directory + local playback URL
```

- Create output directory: `{output}/{Author}/{Title}/`
- Move M4B into directory
- Copy cover as `cover.jpg` (resize to max 800x800 with Pillow if larger)
- Write `desc.txt` from book description
- Write `reader.txt`: `"AI Narration (af_heart)"`
- Sanitize all filenames (replace `/\:*?"<>|` with `_`)

**Local playback server:**
- Start a minimal HTTP server (Python's `http.server`) on `--port` (default 8590)
- Serve the output directory
- Print to console:
  ```
  ✓ Conversion complete.
    Title:    The Great Gatsby
    Author:   F. Scott Fitzgerald
    Chapters: 12
    Duration: 8h 42m
    Size:     497 MB
    Output:   ./output/F. Scott Fitzgerald/The Great Gatsby/

    ▶ Listen: http://localhost:8590/The%20Great%20Gatsby.m4b
    
    Press Ctrl+C to stop the server.
  ```
- The URL is playable directly in a browser (Chrome/Firefox play M4B natively) or any media player
- Server runs until interrupted — this is for manual QA only

---

**Validation checklist (manual, by listening + inspecting):**
- [ ] Audio plays in browser at the served URL
- [ ] Chapter markers appear and are navigable (in browser or VLC)
- [ ] Chapter titles are correct
- [ ] Cover art displays
- [ ] Audio quality is consistent across chapters (no artifacts, no sudden volume changes)
- [ ] Listen to at least 10 minutes spanning 2+ chapters
- [ ] Output directory structure matches what Audiobookshelf expects
- [ ] After validation: manually copy output dir to `/mnt/nas/media/audiobooks/` and confirm Audiobookshelf picks it up

---

**Dependencies (PoC only):**
```
ebooklib
beautifulsoup4
lxml
requests
pydub
Pillow
nltk
```
System: `ffmpeg`, `python3.11+`

**What this proves:**
- EPUB parsing and chapter detection work on a real book
- Kokoro produces acceptable audio quality at scale (full book, not just a test sentence)
- Sentence chunking stays within token limits without artifacts at chunk boundaries
- M4B assembly produces valid files with working chapter markers
- Output structure is Audiobookshelf-compatible
- The full pipeline runs end-to-end without manual intervention (aside from launching it)

---

### Phase 2: Core Pipeline

**Goal:** Modular, testable Python package. All conversion logic solid. Still CLI-only.

**Deliverables:**

**`calibre_reader.py`**
- Read Calibre `metadata.db` (SQLite)
- List all books with: id, title, author, series, series_index, cover path, format paths
- Filter to EPUB/KEPUB only
- Search by title/author
- Detect which books already have audiobook versions in the output directory

**`epub_extractor.py`**
- Parse EPUB 2 and 3 via ebooklib
- Handle KEPUB (strip Kobo `<span>` markup)
- 4-level chapter detection chain with fallback
- Clean text extraction: strip HTML, normalize whitespace, preserve paragraph breaks
- Handle edge cases: footnotes, endnotes, front matter, back matter (skip non-content sections)
- Output: ordered list of `(chapter_num, chapter_title, chapter_text)`

**`tts_client.py`**
- Sentence tokenization via nltk
- Token budget accumulator (≤450 tokens per chunk, never split mid-sentence)
- HTTP client for Kokoro-FastAPI with:
  - Configurable timeout (long chunks can take 10-30s)
  - Retry with exponential backoff (3 attempts)
  - Connection health check before starting a job
- Collect raw WAV bytes per chunk
- Concatenate chunks per chapter with 50ms crossfade via pydub

**`m4b_builder.py`**
- Accept list of chapter WAV files + metadata dict
- Encode to AAC 128kbps via ffmpeg
- Generate ffmetadata file with chapter markers (timestamps computed from actual audio duration)
- Embed: title, author, album, album_artist, year, genre ("Audiobook"), cover art
- Write to temp directory first, then atomic move to output path
- Validate output: file exists, non-zero size, ffprobe confirms chapter count

**`output_manager.py`**
- Create Audiobookshelf directory structure: `{Author}/{Series}/{Title}/` or `{Author}/{Title}/`
- Copy and resize cover art to JPEG, max 800x800
- Write `desc.txt` from Calibre book description
- Write `reader.txt` with voice identifier
- Handle filename sanitization (no `/`, `\`, `:`, etc.)
- Duplicate detection: skip if M4B already exists at target path

**`job_queue.py`**
- SQLite state machine: `pending → extracting → synthesizing → building → complete | failed`
- Track per-chapter progress within `synthesizing` state
- Resume: on restart, pick up jobs in `synthesizing` from last completed chapter
- Prevent duplicate jobs for same calibre_book_id
- Sequential processing (one job at a time — single GPU)

**`config.py`**
- Read from env vars with sane defaults
- All settings overridable

**`main.py`**
- Click CLI: `narrator list`, `narrator convert <id>`, `narrator sync-all`, `narrator status`

**`pyproject.toml` + uv**
- Project managed with `uv` (Python package manager)
- `pyproject.toml` defines all dependencies, scripts, and metadata
- Lockfile (`uv.lock`) committed for reproducible builds

**Dockerfile**
- Python 3.11 slim base
- Install system deps: ffmpeg, espeak-ng, curl (for healthcheck)
- Install uv, then `uv sync --frozen` to install from lockfile
- Copy application code
- Entrypoint: `uv run python -m narrator.main`

---

### Phase 3: Plug-and-Play Container

**Goal:** Drop into Docker Compose, `docker compose up`, open browser at `:8585`, it works. No CLI needed. Self-contained appliance like CWA or Audiobookshelf.

---

#### 3.1 Container First-Run Experience

On first start with an empty `narrator-data` volume:

1. **Database init:** create SQLite database with schema, populate `settings` table from environment variables
2. **Mount validation:**
   - `/calibre-library` exists and contains `metadata.db` → log success
   - `/calibre-library` missing or no `metadata.db` → log warning, show setup guidance in web UI dashboard
   - `/audiobooks` exists and is writable → log success
   - `/audiobooks` not writable → log error, show in web UI
3. **TTS connectivity:**
   - GET `{TTS_API_URL}/v1/audio/voices` → log available voices, cache voice list
   - Unreachable → log warning, show in web UI dashboard (container still starts, retries periodically)
4. **Audiobookshelf connectivity** (if `ABS_API_TOKEN` is set):
   - Test API access → log success/failure, show in settings
5. **Library scan:** enumerate all EPUB/KEPUB books from `metadata.db`, cross-reference against existing M4B files in `/audiobooks`
6. **Ready:** web UI available on `:{NARRATOR_PORT}`, dashboard shows library summary and system status

On subsequent starts: validate mounts, check connections, resume any interrupted jobs from last completed chapter.

#### 3.2 Web UI

Server-rendered HTML (Jinja2 templates) with HTMX for interactivity and Alpine.js for client-side state. No separate frontend build step, no Node.js, no SPA framework. Consistent with self-hosted tool conventions.

**Design Principles:**
- Clean, spacious, minimal. Generous whitespace. Book covers are the only visual richness — the UI around them stays quiet.
- Status communicated through subtle color only (small colored dot or thin left-border accent on rows). No emoji, no badges, no icons for status.
- Colors: green = converted, blue = in progress, amber = queued, neutral/gray = not converted, red = failed. Muted tones, not saturated.
- Typography-driven: clear hierarchy through font size and weight, not through decoration.
- No gratuitous cards, shadows, or rounded corners. Flat, functional, newspaper-like clarity.
- Tables favor generous row height and padding over information density.
- Actions are text buttons or plain links, not brightly colored pill buttons. Primary action per page gets a single understated solid button.

**Layout (all pages):**
- Top nav: wordmark "CWA-Narrator", text navigation links (Dashboard, Library, Queue, Settings, Logs), small colored dot for system health (no label — tooltip on hover)
- If a job is active, a thin progress bar spans the full top of the page beneath the nav (no text overlay — just the bar)

---

**Dashboard (`/`)**

- **Connections:** compact row of text-only status lines, one per service:
  - "Kokoro TTS — connected (26 voices)" or "Kokoro TTS — disconnected" (red text)
  - "Library — 312 books (278 EPUB)" 
  - "Output — writable, 45 audiobooks"
  - "Audiobookshelf — connected" or "not configured" (gray)
- **Active Conversion** (if any):
  - Cover (medium), title, author on one line
  - Thin progress bar below, "Chapter 5 of 12" right-aligned in small text
  - "Cancel" as a plain text link
- **Recent Activity:** simple list — date, title, "completed" or "failed" in the appropriate color. No cover thumbnails here. Last 10 entries.
- **Actions:** two plain text links at the bottom of the page: "Scan Library" · "Convert All Unprocessed"
  - "Convert All" requires a confirmation step before proceeding

---

**Library (`/library`)**

- **Default view:** table. No grid/card toggle — the table is the interface.
- **Table columns:** small cover thumbnail (40px), title, author, series, status
- **Status column:** a single colored dot (8px). Hover tooltip shows detail:
  - Green dot: "Converted Feb 8, 2026"
  - Blue dot: "Converting — Chapter 3 of 12"
  - Amber dot: "Queued (#3)"
  - No dot (empty): not converted
  - Red dot: "Failed — hover or click for details"
- **Above the table:**
  - Search input (full width, searches title + author, filters as you type)
  - Filter row: Author (dropdown), Series (dropdown), Status (dropdown: All / Not Converted / Converted / In Progress / Failed)
  - Sort: clickable column headers
- **Bulk actions:**
  - Checkbox column (leftmost). "Select all" checkbox in header.
  - When any rows selected, a quiet action bar appears above the table: "{N} selected — Convert · Clear selection"
  - "Convert" opens a minimal dialog: voice dropdown + "Queue" button
- **Pagination:** bottom of table, page numbers + per-page selector (25 / 50 / 100)
- **Row click:** navigates to book detail

---

**Book Detail (`/book/<id>`)**

- **Top section:** cover (left, large), metadata (right):
  - Title (large), Author, Series + index if applicable
  - Description text (collapsible if long)
  - Smaller metadata: publisher, year, ISBN, format, file size
- **Chapters section:**
  - Heading: "Chapters (12 detected)" or "Chapters (edited)"
  - Plain list: chapter number, title, word count. No snippets by default — expandable on click to show first 100 chars.
  - "Edit Chapters" text link → opens chapter editor (Phase 4)
- **Convert section:**
  - Voice dropdown (one line, inline with action)
  - "Preview 30s" text link (Phase 4) + "Convert" button (the one primary button on the page)
- **History section** (if previously converted):
  - Simple table: date, voice, duration, file size
  - "Re-convert" text link per row
- **If currently converting:**
  - Chapter list shows colored left-border per chapter: green (done), blue (active), gray (pending)
  - "Cancel" text link

---

**Queue (`/queue`)**

Three sections, visually separated by whitespace:

- **Now:**
  - Single row: cover (small), title, author, voice
  - Progress bar + "Chapter 5 of 12" 
  - "Cancel" text link
- **Up Next:**
  - Ordered list: position number, title, author, voice
  - Drag handles (subtle grip icon) for reorder. "Remove" text link per row.
  - "Pause Queue" / "Resume Queue" text link at section top
- **Done:**
  - Reverse-chronological list: title, author, voice, date, duration
  - Green left-border for completed, red for failed
  - Failed rows show error on expand. "Retry" text link.
  - "Clear" text link at section top

---

**Settings (`/settings`)**

Organized as a single long-scroll page with clear section headings separated by whitespace. No tabs, no accordions. All saves are immediate (no "Save" button — inputs auto-save on blur/change with subtle "Saved" confirmation text).

- **Voice & Audio**
  - Default voice (dropdown)
  - TTS speed (number input with 0.1 step, not a slider)
  - Audio bitrate (dropdown: 64 / 96 / 128 / 192 kbps)
  - Output path pattern (dropdown: `{author}/{title}`, `{author}/{series}/{title}`, `{title}`)
  - reader.txt template (text input)

- **Automation**
  - Auto-convert new books (toggle)
  - Scan interval in minutes (number input, visible only when auto-convert is on)
  - Quiet hours: start time, end time (time inputs, optional — leave blank to disable)
  - Delay between books (number input, seconds)

- **Connections**
  - Kokoro TTS URL (text input) + "Test" text link → inline result: "Connected, 26 voices, 12ms" or error
  - Audiobookshelf URL (text input) + API token (password input) + "Test" text link → inline result
  - Webhook URL (text input) + "Test" text link
  - Webhook toggles: on completion, on failure

- **Advanced**
  - Chunk token limit (number input)
  - Chapter detection mode (dropdown)
  - Crossfade duration ms (number input)
  - Max retries (number input)
  - Log level (dropdown)

- **Maintenance**
  - "Rescan Library" · "Rescan Audiobooks" · "Clear Preview Cache (12 MB)" — plain text links
  - "Export Settings" · "Import Settings" · "Export Job History (CSV)" — plain text links

---

**Statistics (`/stats`)**

- Top line: three numbers in large text: "312 books · 45 converted · 267 remaining"
- Total audio hours generated (single number, large)
- Conversion activity: minimal bar chart — books per week, last 12 weeks. Monochrome bars.
- Top authors: horizontal bars, top 10. Text labels, no decoration.
- Voice usage: simple percentage list (no pie chart — just "af_heart: 89% · am_fenrir: 11%")
- Average conversion time: single number

All charts monochrome or single-accent-color. No gradients, no 3D, no shadows.

---

**Logs (`/logs`)**

- Monospace text area, full width, auto-scrolling
- Above the log: level filters as small toggles (DEBUG · INFO · WARN · ERROR), text search input, job ID input
- "Download" · "Clear Display" as text links in the top-right
- Pauses auto-scroll when user scrolls up manually. "Resume" text link appears at bottom.

---

#### 3.3 REST API

Every web UI action is backed by a REST API. All endpoints return JSON. The API enables scripting, external integrations, and future mobile/CLI clients.

```
Health & System
  GET    /api/health                       → system health + component status
  GET    /api/version                      → version info

Library
  GET    /api/books                        → paginated book list
           ?search=query
           &author=name
           &series=name
           &status=not_converted|converted|in_progress|failed
           &sort=title|author|added|series
           &page=1&per_page=25
  GET    /api/books/:id                    → single book detail + detected chapters
  POST   /api/books/:id/convert            → queue conversion
           { "voice": "af_heart" }            (optional, uses default if omitted)
  POST   /api/books/:id/preview            → generate 30s preview, return audio URL
           { "voice": "af_heart" }
  POST   /api/books/convert-all            → queue all unconverted
           { "voice": "af_heart", "dry_run": false }
  POST   /api/books/convert-batch          → queue specific books
           { "book_ids": [1, 5, 12], "voice": "af_heart" }

Queue
  GET    /api/queue                        → full queue state (active, pending, completed, failed)
  POST   /api/queue/pause                  → pause after current chapter
  POST   /api/queue/resume                 → resume processing
  DELETE /api/queue/:job_id                → cancel/remove job
  POST   /api/queue/:job_id/retry          → retry failed job
  PATCH  /api/queue/reorder                → set queue order
           { "job_ids": [5, 3, 8] }

Jobs
  GET    /api/jobs                         → all jobs with filters
           ?status=complete|failed|pending|synthesizing
           &page=1&per_page=25
  GET    /api/jobs/:id                     → job detail with per-chapter progress

Voices
  GET    /api/voices                       → available voices from Kokoro (cached)
  POST   /api/voices/refresh               → force-refresh voice list from Kokoro

Settings
  GET    /api/settings                     → all current settings
  PATCH  /api/settings                     → update settings (partial update)
           { "default_voice": "af_bella", "audio_bitrate": "192k" }
  GET    /api/settings/export              → download settings JSON
  POST   /api/settings/import              → upload and apply settings JSON

System
  POST   /api/library/scan                 → trigger Calibre library rescan
  POST   /api/library/scan-audiobooks      → recheck existing audiobook output
  GET    /api/logs                         → recent logs
           ?level=error&search=text&job_id=42&limit=100
  GET    /api/logs/download                → full log file
  GET    /api/stats                        → statistics data for charts
```

#### 3.4 Background Services

The container runs a FastAPI/Starlette app with async background tasks (no external process supervisor needed):

**Job Worker (async task)**
- Pulls next `pending` job from queue, executes the full pipeline
- Updates job status and chapter progress in real-time (web UI polls or uses SSE)
- On completion: moves M4B to output dir, triggers ABS scan, sends webhook
- On failure: sets status to `failed` with error message, moves to next job
- On container restart: resumes interrupted `synthesizing` jobs from last completed chapter
- Respects quiet hours: if quiet hours active, completes current chapter then pauses
- Sequential only (single GPU)

**Library Watcher (async task, when `AUTO_CONVERT=true`)**
- Periodic scan of Calibre `metadata.db` (interval from settings)
- Compares against job history — only queues books never attempted
- Cross-references against `/audiobooks` — skips books with existing M4B files
- Logs detections: "[WATCHER] New book detected: 'Title' by Author — queuing for conversion"

**Health Monitor (async task)**
- Pings Kokoro-FastAPI every 60 seconds, updates connection status
- If Kokoro goes down mid-job: marks job as `failed` with clear error, pauses queue, retries connection periodically
- On Kokoro reconnect: logs recovery, resumes queue
- Health state feeds `/api/health` endpoint and UI status indicators

**Progress Broadcaster (SSE or polling)**
- `/api/queue/events` SSE endpoint: streams real-time job progress events to web UI
- Events: `job_started`, `chapter_started`, `chapter_completed`, `job_completed`, `job_failed`, `queue_paused`, `queue_resumed`
- Web UI dashboard and queue page subscribe for live updates without page refresh

#### 3.5 Logging

- Structured logging to stdout (captured by Docker via `docker logs cwa-narrator`)
- Format: `[2026-02-10 19:58:09] [INFO] [job:42] Converting "The Great Gatsby" — Chapter 3/12 — Chunk 7/15`
- Levels: DEBUG, INFO, WARNING, ERROR
- In-memory ring buffer (last 10,000 lines) serves the `/logs` web UI page
- Configurable via `LOG_LEVEL` env var and settings page

#### 3.6 Health Check

```json
GET /api/health
{
  "status": "healthy | degraded | unhealthy",
  "version": "1.0.0",
  "kokoro": {
    "connected": true,
    "url": "http://kokoro-tts:8880",
    "voices_available": 26
  },
  "library": {
    "path": "/calibre-library",
    "accessible": true,
    "total_books": 347,
    "epub_books": 312
  },
  "output": {
    "path": "/audiobooks",
    "writable": true,
    "existing_audiobooks": 45
  },
  "audiobookshelf": {
    "configured": true,
    "connected": true
  },
  "queue": {
    "active_job": "The Great Gatsby",
    "pending": 3,
    "completed": 42,
    "failed": 1
  }
}
```

Status logic:
- `healthy`: library readable, output writable, Kokoro connected
- `degraded`: Kokoro unreachable or ABS unreachable (container runs but can't convert)
- `unhealthy`: library inaccessible or output not writable

#### 3.7 Notifications

On job completion or failure, optionally POST to a webhook URL. Compatible with ntfy, Gotify, Apprise, Discord webhooks, Home Assistant, or any HTTP endpoint.

```
WEBHOOK_URL=https://ntfy.sh/my-audiobooks
WEBHOOK_ON_COMPLETE=true
WEBHOOK_ON_FAILURE=true
```

Payload:
```json
{
  "event": "job_complete | job_failed",
  "title": "The Great Gatsby",
  "author": "F. Scott Fitzgerald",
  "voice": "af_heart",
  "chapters": 12,
  "output_path": "/audiobooks/F. Scott Fitzgerald/The Great Gatsby/The Great Gatsby.m4b",
  "duration_seconds": 28800,
  "file_size_bytes": 518400000,
  "conversion_time_seconds": 3420,
  "error_message": null
}
```

---

### Phase 4: Polish & Advanced Features

**Goal:** Quality-of-life features that handle edge cases, improve long-term usability, and make the tool feel complete.

---

#### 4.1 Voice Preview System

**Problem:** Committing to a 60+ minute conversion before knowing how the voice sounds on your book is wasteful.

**Solution:**
- Book detail page: "Preview" button next to the voice selector
- Click generates a 30-second audio clip from the first substantial paragraph of chapter 1
- Audio player appears inline — play, pause, scrub
- User can switch voice in the dropdown and preview again
- Preview audio cached in `narrator-data/preview_cache/` keyed by `{book_id}_{voice}.wav`
- Cache auto-pruned: entries older than 7 days deleted on startup and daily
- Cache size shown on settings page with "Clear Preview Cache" button

#### 4.2 Series-Aware Processing

**Problem:** Books in a series need to be grouped and ordered correctly in Audiobookshelf.

**Solution:**
- Library page groups books by series (collapsible sections or filter)
- "Convert Entire Series" button on any book that belongs to a series
- Books within a series queued in `series_index` order
- Output directory uses series folder when present:
  ```
  /audiobooks/Brandon Sanderson/The Stormlight Archive/The Way of Kings/
  ```
- M4B metadata:
  - `album` = series name (e.g. "The Stormlight Archive")
  - `track` = series_index (e.g. 1)
  - `album_artist` = author
- Audiobookshelf recognizes this structure for automatic series grouping

#### 4.3 Chapter Editor

**Problem:** Auto chapter detection is wrong sometimes — merging two chapters, missing a split, including the table of contents or copyright as a "chapter."

**Solution:**
- Book detail page: "Edit Chapters" button opens an editor below the chapter list
- Editable chapter list with drag handles for reorder
- Per-chapter actions:
  - **Rename:** inline edit of chapter title
  - **Merge down:** combine this chapter with the next one
  - **Split:** click to expand chapter text, click at a split point (between paragraphs), or enter a text search string to split at
  - **Exclude:** toggle to skip this chapter entirely (e.g. copyright page, table of contents, acknowledgments)
  - **Move up / Move down**
- "Reset to Auto-Detected" button to discard edits
- Edits saved per-book in the database (`chapter_overrides` JSON column on jobs table)
- Re-conversion of a book uses saved chapter edits by default
- Visual indicator on book detail: "(chapters edited)" badge if manual overrides exist

#### 4.4 Scheduling & Resource Management

**Problem:** Sustained GPU usage generates heat and fan noise. You might not want conversions running overnight or during work calls.

**Quiet Hours:**
- Settings: start time and end time (time pickers)
- When quiet hours activate: current chapter finishes, then worker pauses
- When quiet hours end: worker resumes automatically
- Dashboard shows: "Paused (quiet hours until 07:00)" when applicable

**Delay Between Books:**
- Settings: configurable seconds (default 0)
- After completing a book, worker waits N seconds before starting the next
- Prevents sustained GPU thermal load on long batch runs

#### 4.5 Re-conversion & Version Management

**Problem:** You might want to re-convert a book after trying a different voice, or after a Kokoro model upgrade.

**Solution:**
- "Re-convert" button on completed books (in book detail and in completed jobs list)
- Re-convert dialog: choose new voice, confirm action
- Behavior options (radio buttons in dialog):
  - "Replace existing" — overwrites the M4B
  - "Keep both" — new file saved as `{Title} ({voice}).m4b`
- Job history table on book detail page: all past conversions with date, voice, duration, file size
- Each conversion is a separate job row — full audit trail

#### 4.6 Batch Operations

**Problem:** Converting hundreds of books one by one through the UI is tedious.

**Solution:**
- Library page: checkbox per book row
- "Select All on Page" checkbox in header
- "Select All Matching Current Filter" link (e.g. select all 200 unprocessed books at once)
- Batch action bar appears when selection > 0: "{N} books selected — Convert Selected | Clear Selection"
- "Convert Selected" opens dialog: voice picker + "Queue" button
- `sync-all` via API: `POST /api/books/convert-all` with optional `dry_run=true` that returns the list of books that would be queued without queuing them
- `convert-batch` via API: `POST /api/books/convert-batch` with explicit book ID list

#### 4.7 Statistics Page (`/stats`)

**Problem:** No visibility into conversion activity over time.

**Solution:**
- **Summary cards:** total library / converted / unconverted / conversion percentage
- **Total audio generated:** sum of all completed M4B durations (hours)
- **Conversion activity:** bar chart — books completed per week (last 12 weeks)
- **Top authors:** horizontal bar chart — most converted authors by book count
- **Voice usage:** pie/donut chart — distribution of voice selections across all jobs
- **Average conversion time:** per book (minutes) with min/max range
- **Storage used:** total M4B file size in output directory

Data sourced from the jobs table, computed on page load (no pre-aggregation needed at this scale).

#### 4.8 Settings Import/Export

**Problem:** Rebuilding configuration after a fresh install or migration.

**Solution:**
- Settings page: "Export Settings" → downloads `narrator-settings.json` containing all key/value pairs from the settings table
- Settings page: "Import Settings" → file upload, applies all settings, shows diff of what changed
- Job history export: "Export Job History" → downloads CSV with columns: title, author, series, voice, status, started, completed, duration, file_size
- Does NOT export/import the job queue or preview cache — only configuration

#### 4.9 Error Recovery & Resilience

**Chunk-level failure handling:**
- If a TTS chunk fails after max retries: mark that chapter as failed, log error, continue with remaining chapters
- Completed book with failed chapters: status `partial`, M4B built with successful chapters only, failed chapters replaced with 2 seconds of silence
- Chapter list in book detail shows which chapters failed
- "Retry Failed Chapters" button: regenerates only the failed chapters, rebuilds M4B

**Kokoro disconnect during conversion:**
- Worker detects failed connection, saves progress (current chapter + chunk index)
- Enters retry loop: check Kokoro every 30 seconds, log each attempt
- On reconnect: resumes from exact chunk where it left off
- After 10 minutes of failed reconnection: mark job as `failed`, move to next job, keep retrying Kokoro in background

**Disk space:**
- Before starting a job: estimate temp space needed (~1.5× expected M4B size based on word count heuristic)
- Check available disk on temp dir and output dir
- If insufficient: reject job with clear error, show in UI: "Insufficient disk space: need ~600MB, have 200MB available"

**Corrupt EPUB:**
- ebooklib parse failure: catch exception, mark job as `failed` with error "Unable to parse EPUB: {error detail}"
- Worker continues to next job — never crashes
- EPUB with no extractable text: fail with "No text content found in EPUB"
- EPUB with no detectable chapters: use fallback (split by word count), log warning

**Database integrity:**
- WAL mode enabled for concurrent reads during writes
- All job state transitions in transactions
- On startup: check database integrity, rebuild if corrupted (rare but possible on power loss)

#### 4.10 UX Polish

- **Dark mode / light mode:** toggle in nav bar, persisted in browser localStorage, respects system preference by default
- **Mobile responsive:** all pages usable on phone (check queue from the couch via Plappa's browser)
- **Toast notifications:** in-browser toasts for job events (started, completed, failed) using SSE
- **Favicon:** changes based on state:
  - Default: book icon
  - Converting: animated/pulsing variant
  - Failed: red badge
- **Page title:** includes live status:
  - Idle: `CWA-Narrator`
  - Active: `CWA-Narrator · Converting "Title" 3/12`
  - Paused: `CWA-Narrator · Paused`
- **Keyboard shortcuts** (library page):
  - `/` → focus search box
  - `j`/`k` → navigate rows
  - `x` → toggle selection
  - `Enter` → open book detail
- **Empty states:** friendly messages with guidance when no books, no jobs, no results
  - Library empty: "No EPUB books found. Make sure your Calibre library is mounted at /calibre-library."
  - Queue empty: "No conversions in progress. Browse your library to get started."
- **Loading states:** skeleton loaders on page navigation, spinner on action buttons

---

## Project File Structure

```
cwa-narrator/
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── narrator/
│   ├── __init__.py
│   ├── app.py                 # FastAPI app — serves API + web UI
│   ├── config.py              # Settings from env vars + database overrides
│   ├── worker.py              # Background job worker
│   ├── watcher.py             # Library auto-scan watcher
│   ├── health.py              # Health monitor + Kokoro connectivity
│   ├── notifications.py       # Webhook sender
│   ├── abs_client.py          # Audiobookshelf API client
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── calibre_reader.py  # Read metadata.db, list/search books
│   │   ├── epub_extractor.py  # EPUB/KEPUB → chapter-segmented text
│   │   ├── tts_client.py      # Kokoro API client + sentence chunking
│   │   ├── m4b_builder.py     # ffmpeg: WAV → AAC → M4B with chapters
│   │   └── output_manager.py  # Directory structure, cover, metadata files
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py        # SQLite connection management
│   │   ├── models.py          # Job and settings models
│   │   └── migrations.py      # Schema creation + future migrations
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes_books.py
│   │   ├── routes_queue.py
│   │   ├── routes_jobs.py
│   │   ├── routes_settings.py
│   │   ├── routes_system.py
│   │   └── routes_voices.py
│   │
│   └── web/
│       ├── templates/
│       │   ├── base.html          # Layout: nav, status bar, dark mode, toast container
│       │   ├── dashboard.html
│       │   ├── library.html
│       │   ├── book_detail.html
│       │   ├── chapter_editor.html
│       │   ├── queue.html
│       │   ├── settings.html
│       │   ├── stats.html
│       │   └── logs.html
│       └── static/
│           ├── css/
│           │   └── style.css      # Minimal custom styles, dark mode variables
│           ├── js/
│           │   ├── htmx.min.js    # HTMX for server-driven interactivity
│           │   ├── alpine.min.js  # Alpine.js for client-side state
│           │   └── app.js         # SSE subscription, toasts, keyboard shortcuts
│           └── img/
│               └── favicon.svg
```

---

## Environment Variables (Complete)

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `UTC` | Timezone |
| `PUID` | `1000` | User ID for file ownership |
| `PGID` | `1000` | Group ID for file ownership |
| `NARRATOR_PORT` | `8585` | Web UI / API port |
| `TTS_API_URL` | `http://kokoro-tts:8880` | Kokoro-FastAPI base URL |
| `CALIBRE_LIBRARY_PATH` | `/calibre-library` | Mount point for Calibre library |
| `AUDIOBOOK_OUTPUT_PATH` | `/audiobooks` | Mount point for audiobook output |
| `DEFAULT_VOICE` | `af_heart` | Default TTS voice ID |
| `TTS_SPEED` | `1.0` | Speech speed multiplier |
| `AUDIO_BITRATE` | `128k` | AAC encoding bitrate |
| `AUTO_CONVERT` | `false` | Auto-queue new books when detected |
| `AUTO_SCAN_INTERVAL` | `30` | Minutes between library scans (when AUTO_CONVERT=true) |
| `ABS_API_URL` | `` | Audiobookshelf API URL (optional) |
| `ABS_API_TOKEN` | `` | Audiobookshelf API token (optional) |
| `WEBHOOK_URL` | `` | Notification webhook URL (optional) |
| `WEBHOOK_ON_COMPLETE` | `true` | Send webhook on job completion |
| `WEBHOOK_ON_FAILURE` | `true` | Send webhook on job failure |
| `QUIET_HOURS_START` | `` | Pause conversions after this time (HH:MM) |
| `QUIET_HOURS_END` | `` | Resume conversions after this time (HH:MM) |
| `DELAY_BETWEEN_BOOKS` | `0` | Seconds to wait between book conversions |
| `LOG_LEVEL` | `info` | Logging level |
| `CHUNK_TOKEN_LIMIT` | `450` | Max tokens per TTS chunk |
| `CROSSFADE_MS` | `50` | Crossfade between audio chunks (ms) |
| `MAX_RETRIES` | `3` | Retry attempts per failed TTS chunk |

All settings overridable at runtime via web UI settings page (persisted to SQLite, takes precedence over env vars).

---

## What's Not in Scope

- Multi-voice narration (dialogue detection) — future project
- PDF support — excluded by design
- Voice cloning — Kokoro 82M doesn't support it
- Cloud TTS fallback — local only
- User authentication — single-user, trusts the local network (like CWA)
