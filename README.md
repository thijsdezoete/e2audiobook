# Narrator

**Automatically convert your ebook library into audiobooks using local AI text-to-speech.**

Narrator is a self-contained Docker appliance that reads your Calibre library (or a folder of EPUBs), converts them to chaptered M4B audiobooks using [Kokoro TTS](https://github.com/remsky/kokoro-fastapi), and drops them into an output directory — ready for Audiobookshelf, Plex, or any audiobook player.

`docker compose up`, open `localhost:8585`, done.

---

## How It Works

```
Calibre Library ──► EPUB Parsing ──► Chapter Detection ──► Kokoro TTS ──► M4B Builder ──► Audiobooks/
   or EPUB folder      (ebooklib)     (TOC/heading/regex)    (local AI)    (ffmpeg+chapters)
```

1. **Scans** your Calibre `metadata.db` or a flat folder of `.epub` files
2. **Extracts** chapters using a 4-level detection chain (TOC → headings → regex → fixed-split)
3. **Synthesizes** speech chapter-by-chapter via Kokoro's OpenAI-compatible API
4. **Builds** a single `.m4b` file with chapter markers, cover art, and metadata
5. **Outputs** organized as `Author/Series/Title/Title.m4b` — drop-in for any audiobook server

---

## Quick Start

### Prerequisites

- Docker and Docker Compose
- A Kokoro TTS instance (GPU recommended, CPU works but is slower)

### 1. Clone and configure

```bash
git clone https://github.com/youruser/narrator.git
cd narrator
```

Create a `.env` file (or set environment variables):

```env
TTS_API_URL=http://kokoro-tts:8880    # Your Kokoro TTS endpoint
CALIBRE_LIBRARY_PATH=/path/to/calibre  # Calibre library or folder of EPUBs
AUDIOBOOK_OUTPUT_PATH=/path/to/output   # Where audiobooks get written
DEFAULT_VOICE=af_heart                  # Kokoro voice ID
```

### 2. Start

```bash
docker compose up -d
```

Open **http://localhost:8585** — the dashboard shows connection status, your library, and conversion queue.

### 3. Convert

- **Web UI:** Browse your library, select books, click Convert
- **Auto mode:** Set `AUTO_CONVERT=true` to automatically convert new books as they appear
- **CLI:** `docker exec narrator uv run narrator convert <book_id>`

---

## Features

### Web Dashboard
- **Library browser** with search, author/status filtering, and bulk selection
- **Book detail** pages with chapter preview and voice selection
- **Queue management** — pause, resume, cancel, retry, reorder
- **Live progress** via Server-Sent Events (no polling, no WebSocket complexity)
- **Settings** page with auto-save — every config option adjustable at runtime
- **Logs** viewer with level filtering and search
- **Stats** page with conversion history and top authors
- **Dark mode** toggle

### Conversion Engine
- **Smart chapter detection** — 4-level fallback: TOC entries → HTML headings → regex patterns → fixed-size splits
- **Front matter filtering** — automatically skips copyright pages, dedications, table of contents
- **Kokoro TTS** with automatic retries, warmup, VRAM rest intervals, and reconnection
- **Chaptered M4B** output with embedded cover art, metadata, and chapter markers
- **Series-aware** output paths: `Author/Series Name/01 - Book Title/`

### Automation
- **Auto-convert** — watches library for new books, queues them automatically
- **Quiet hours** — pause conversions overnight (e.g., `22:00` to `07:00`)
- **Delay between books** — configurable cooldown to manage GPU thermals
- **Webhook notifications** — POST JSON on job completion/failure (compatible with ntfy, Gotify, Discord, etc.)
- **Audiobookshelf integration** — trigger library scan after each conversion

### REST API
Full API at `/api/` — every action available in the UI is also available programmatically:

| Endpoint | Description |
|---|---|
| `GET /api/health` | System health (TTS, library, output, worker) |
| `GET /api/books` | Paginated, filterable book list |
| `GET /api/books/:id` | Book detail with detected chapters |
| `POST /api/books/:id/convert` | Queue a single book |
| `POST /api/books/convert-all` | Queue all unconverted books |
| `GET /api/queue` | Queue state (active/pending/done/failed) |
| `POST /api/queue/pause` | Pause the queue |
| `DELETE /api/queue/:id` | Cancel a queued job |
| `POST /api/queue/:id/retry` | Retry a failed job |
| `GET /api/queue/events` | SSE stream for live progress |
| `GET /api/jobs` | Paginated job history |
| `GET /api/voices` | Available Kokoro voices (cached) |
| `GET /api/settings` | All settings |
| `PATCH /api/settings` | Update settings |
| `GET /api/stats` | Conversion statistics |
| `GET /api/logs` | Application logs with filtering |
| `POST /api/scan` | Trigger library scan |

---

## Configuration

All settings can be configured via environment variables and overridden at runtime through the web UI. UI changes persist in the database and survive restarts.

| Variable | Default | Description |
|---|---|---|
| `TTS_API_URL` | `http://kokoro-tts:8880` | Kokoro TTS server URL |
| `CALIBRE_LIBRARY_PATH` | `/calibre-library` | Path to Calibre library or EPUB folder |
| `AUDIOBOOK_OUTPUT_PATH` | `/audiobooks` | Output directory for converted audiobooks |
| `DEFAULT_VOICE` | `af_heart` | Default Kokoro voice ID |
| `NARRATOR_PORT` | `8585` | Web UI port |
| `LOG_LEVEL` | `info` | Log level (debug, info, warning, error) |
| `TTS_SPEED` | `1.0` | TTS speech speed multiplier |
| `AUTO_CONVERT` | `false` | Auto-convert new books when detected |
| `AUTO_SCAN_INTERVAL` | `300` | Library scan interval in seconds |
| `QUIET_HOURS_START` | | Start of quiet hours (HH:MM) |
| `QUIET_HOURS_END` | | End of quiet hours (HH:MM) |
| `DELAY_BETWEEN_BOOKS` | `0` | Seconds to wait between conversions |
| `WEBHOOK_URL` | | URL for completion/failure notifications |
| `WEBHOOK_ON_COMPLETE` | `true` | Send webhook on job completion |
| `WEBHOOK_ON_FAILURE` | `true` | Send webhook on job failure |
| `ABS_API_URL` | | Audiobookshelf server URL |
| `ABS_API_TOKEN` | | Audiobookshelf API token |

---

## Architecture

```
narrator/
├── app.py                  # FastAPI application + lifespan
├── worker.py               # Background job worker (async)
├── health.py               # Health monitor (Kokoro, mounts)
├── watcher.py              # Library auto-scanner
├── notifications.py        # Webhook sender
├── abs_client.py           # Audiobookshelf integration
├── config.py               # Two-layer settings (env + SQLite)
├── job_queue.py            # Job queue (SQLite-backed)
├── main.py                 # Click CLI (secondary entry point)
├── api/
│   ├── routes_books.py     # Library endpoints
│   ├── routes_queue.py     # Queue + SSE endpoints
│   ├── routes_jobs.py      # Job history
│   ├── routes_voices.py    # Kokoro voice list
│   ├── routes_settings.py  # Settings CRUD
│   └── routes_system.py    # Health, logs, stats, scan
├── core/
│   ├── epub_extractor.py   # EPUB parsing + chapter detection
│   ├── tts_client.py       # Kokoro TTS client with retry
│   ├── m4b_builder.py      # ffmpeg M4B assembly
│   ├── output_manager.py   # Output directory organization
│   ├── calibre_reader.py   # Calibre metadata.db reader
│   └── folder_reader.py    # Flat folder EPUB reader
├── db/
│   ├── database.py         # SQLite connection (WAL mode)
│   ├── migrations.py       # Schema migrations
│   └── models.py           # Job/status dataclasses
└── web/
    ├── templates/           # Jinja2 + HTMX templates
    └── static/              # CSS, JS (htmx, alpine.js)
```

**Key design decisions:**
- **SQLite + WAL mode** — no external database needed; concurrent reads are fine for this workload
- **Synchronous core, async wrapper** — TTS and ffmpeg calls are synchronous, wrapped in `asyncio.to_thread()` for the async web server
- **Server-rendered UI** — Jinja2 templates with HTMX for interactivity; no JavaScript build step, no SPA framework
- **Two-layer config** — environment variables set defaults; the web UI persists overrides to SQLite

---

## Library Sources

Narrator supports two library modes (auto-detected):

**Calibre Library** — Point `CALIBRE_LIBRARY_PATH` at your Calibre library directory. Narrator reads `metadata.db` directly (read-only) to get book metadata, series info, and EPUB paths.

**Folder of EPUBs** — If no `metadata.db` is found, Narrator falls back to scanning the directory for `.epub` files and extracting metadata from each file. Works with any flat or nested folder structure.

---

## CLI

The Click CLI is available as a secondary interface for scripting and debugging:

```bash
# List books
docker exec narrator uv run narrator list
docker exec narrator uv run narrator list --search "Postman"

# Convert a specific book
docker exec narrator uv run narrator convert 42
docker exec narrator uv run narrator convert 42 --voice bf_emma

# Queue all unconverted books
docker exec narrator uv run narrator sync-all

# Check queue status
docker exec narrator uv run narrator status
```

---

## Tech Stack

| Component | Technology |
|---|---|
| Runtime | Python 3.13 |
| Web framework | FastAPI + Uvicorn |
| Templates | Jinja2 + HTMX + Alpine.js |
| TTS engine | Kokoro (OpenAI-compatible API) |
| Audio processing | ffmpeg + pydub |
| EPUB parsing | ebooklib + BeautifulSoup |
| Database | SQLite (WAL mode) |
| NLP | NLTK (sentence tokenization) |
| Package manager | uv |
| Container | Docker (Python 3.13-slim) |

---

## License

MIT
