# Phase 1: Proof of Concept -- Progress

## Status: Functional, field-tested with 2 books

---

## Completed Books

| Book | Author | Chapters | Duration | Size | Status |
|------|--------|----------|----------|------|--------|
| Win Bigly | Scott Adams | 24 of 111 | 2h 36m | 115.9 MB | Partial (Kokoro crashes stopped early) |
| Amusing Ourselves to Death | Neil Postman | 13 of 13 | 5h 49m | 258.7 MB | Complete |

Output structure verified Audiobookshelf-compatible:
```
output/{Author}/{Title}/
  {Title}.m4b     -- chapter markers, cover art, metadata
  cover.jpg       -- resized to 800x800
  desc.txt        -- HTML-stripped description
  reader.txt      -- "AI Narration (af_heart)"
```

---

## Spec Checklist

### Step 1: Parse EPUB

| Requirement | Status | Notes |
|-------------|--------|-------|
| Open EPUB with ebooklib | Done | |
| Extract metadata (title, author, language, publisher, date, description) | Done | |
| Extract cover image | Done | Checks for external cover.jpg next to EPUB first, then EPUB metadata, then first image |
| TOC-based chapter detection | Done | Handles relative paths (`../OEBPS/file.html`), fragment anchors (`file.html#id`), nested sections |
| Heading-based chapter detection | Done | h1/h2 scanning |
| Regex chapter detection | Done | "Chapter N" / "PART N" patterns |
| Fixed split fallback | Done | ~5000 word chunks at paragraph boundaries |
| Strip HTML with BeautifulSoup | Done | `get_text(separator='\n')` |
| Normalize whitespace | Done | Collapse 3+ newlines to 2 |
| Skip chapters < 50 words | Done | |
| Handle KEPUB (strip koboSpan) | Done | |
| Print chapter summary | Done | |

**Beyond spec:**
- Front/back matter detection: title-based (SKIP_TITLES regex) + content-based (FRONT_MATTER_SIGNATURES, `_looks_like_toc()`)
- Drop cap handling: unwrap dropcap-class elements, rejoin `^([A-Z])\n([a-z])` patterns
- Title stripping from body text: word-based matching ignores formatting differences between TOC and in-text titles
- External cover.jpg priority over embedded cover

### Step 2: Chunk Text for TTS

| Requirement | Status | Notes |
|-------------|--------|-------|
| Sentence tokenization via nltk | Done | `punkt_tab` |
| Token estimate: `len(sentence) / 3.5` | Done | |
| Accumulate to token limit | Done | Reduced from 450 to 250 (Kokoro VRAM on GTX 1650) |
| Never split mid-sentence | Done | |
| Long sentence splitting at comma/semicolon/word boundary | Done | |

**Beyond spec:**
- TOKEN_FLOOR (80): minimum tokens before flushing a chunk, prevents short chunks causing weird TTS stops
- Trailing small chunk merging: if last chunk < TOKEN_FLOOR, append to previous
- Chapter title sent as separate TTS chunk for natural pause between title and body

### Step 3: Synthesize Audio

| Requirement | Status | Notes |
|-------------|--------|-------|
| POST to `/v1/audio/speech` with WAV response | Done | |
| Retry with backoff on HTTP error | Done | 5 attempts at 5/10/20/40/60s (increased from spec's 3 at 2/4/8s) |
| Print progress per chunk | Done | `Chapter N/M -- Chunk I/J: Title` |
| Concatenate chunks with 50ms crossfade | Done | pydub |
| Print total audio duration | Done | |

**Beyond spec:**
- TTS health check + warmup before starting (`_wait_for_tts`): health check on `/v1/audio/voices`, then synthesize ~50-word warmup text, 3 warmup attempts
- Smart crash recovery: on TTS failure, call `_wait_for_tts()` instead of blind sleep
- VRAM rest interval: pause 5s every 10 chunks to let GPU breathe
- 1s cooldown between requests (TTS_COOLDOWN)
- Chapter title spoken in title case (not ALL CAPS)
- WAV caching in debug mode: skip already-generated chapters on resume
- `--start-chapter N` flag to skip ahead
- `--build-only` flag to skip synthesis entirely

### Step 4: Build M4B

| Requirement | Status | Notes |
|-------------|--------|-------|
| Encode WAVs to AAC 128k via ffmpeg | Done | |
| Create concat file | Done | |
| Concatenate with `ffmpeg -f concat` | Done | |
| Generate ffmetadata with chapter markers | Done | TIMEBASE=1/1000, timestamps from ffprobe |
| Embed metadata + cover art | Done | title, artist, album, genre, date, cover as attached_pic |
| `movflags +faststart` | Done | |
| Validate: file exists, non-zero, chapter count | Done | |
| Print file size, duration, chapter count | Done | |

**Beyond spec:**
- Aggressive intermediate cleanup: delete WAVs after encoding, M4As after concat, combined after mux (non-debug mode)
- Per-book build cache directory: `output/_build/{title}/`

### Step 5: Output & Serve

| Requirement | Status | Notes |
|-------------|--------|-------|
| Create `{Author}/{Title}/` directory | Done | |
| Move M4B into directory | Done | |
| Copy + resize cover to max 800x800 JPEG | Done | Pillow LANCZOS |
| Write desc.txt from description | Done | HTML stripped via BeautifulSoup |
| Write reader.txt with voice ID | Done | |
| Sanitize filenames | Done | Replace `/\:*?"<>|` with `_` |
| Start HTTP server on `--port` | Done | `http.server` on 0.0.0.0 |
| Print summary + listen URL | Done | |

### Arguments (from spec)

| Argument | Status | Default |
|----------|--------|---------|
| `epub_path` (positional) | Done | -- |
| `--tts-url` | Done | env `TTS_URL` or `http://192.168.2.38:11880` |
| `--voice` | Done | `af_heart` |
| `--output` | Done | `./output` |
| `--serve` / `--no-serve` | Done | `true` |
| `--port` | Done | `8590` |

**Additional arguments (not in spec):**
| Argument | Purpose |
|----------|---------|
| `--debug` | Keep intermediate files in `output/_build/{title}/` |
| `--start-chapter N` | Resume from chapter N |
| `--build-only` | Skip synthesis, build M4B from cached WAVs |

### Infrastructure

| Item | Status | Notes |
|------|--------|-------|
| `pyproject.toml` | Done | cwa-narrator, Python 3.13, ruff config |
| `uv.lock` | Done | Generated inside Docker container |
| `Dockerfile` | Done | python:3.13-slim, static ffmpeg (700MB vs 960MB), uv, nltk punkt_tab |
| `docker-compose.yml` | Done | kokoro-tts (GPU profile), poc service |
| `STANDARDS.md` | Done | Ruff, no comments, no emojis, KISS, Docker-first |
| `.gitignore` | Done | |
| Ruff lint passing | Done | |

---

## Known Issues

### Kokoro VRAM crashes (GTX 1650, 4GB)
The Kokoro server crashes silently (SIGSEGV / exit 139) under sustained load. Mitigations in place:
- TOKEN_LIMIT reduced from 450 to 250
- 1s cooldown between requests
- 5s rest every 10 chunks
- Warmup request after health check
- Smart recovery: `_wait_for_tts()` on failure instead of blind retry
- Server needs manual restart after crash (no `restart: unless-stopped` in dev)

Crashes still happen. The GTX 1650 is at the edge of what Kokoro can sustain. The production server (Dokploy) may have a more capable GPU.

### Win Bigly incomplete
The first test book only produced 24 of 111 chapters before Kokoro crashes and early testing interrupted the run. Needs a fresh re-synthesis with current code. The book has many very short "chapters" (some < 100 words) which are really subsections within larger chapters -- this is correct per the EPUB's TOC structure.

---

## Deviations from Spec

| Spec says | We did | Reason |
|-----------|--------|--------|
| Token limit 450 | 250 | GTX 1650 VRAM crashes |
| Retry 3x at 2/4/8s | 5x at 5/10/20/40/60s | Kokoro needs longer recovery time |
| `http://localhost:11880` default | `http://192.168.2.38:11880` | TTS on separate server (NAS), not localhost |
| Python 3.11+ | Python 3.13 | Latest slim image, required `audioop-lts` workaround |
| `ffmpeg` via apt | Static binaries from `mwader/static-ffmpeg:7.1` | 260MB smaller Docker image |

---

## What's Next (still Phase 1)

1. **Re-run Win Bigly** with current code (proper TOC detection, warmup, crash recovery)
2. **QA listening test** on Amusing Ourselves to Death M4B (chapter markers, title pauses, audio quality)
3. **Copy to Audiobookshelf** and verify it picks up the library structure
4. **Validation checklist from spec:**
   - [ ] Audio plays in browser at served URL
   - [ ] Chapter markers appear and are navigable
   - [ ] Chapter titles are correct
   - [ ] Cover art displays
   - [ ] Audio quality consistent across chapters
   - [ ] Listen to 10+ minutes spanning 2+ chapters
   - [ ] Output directory matches Audiobookshelf expectations
   - [ ] Manually copy to `/mnt/nas/media/audiobooks/` and confirm ABS picks it up

---

## File Summary

```
poc.py              895 lines    Main script (all 5 pipeline steps)
pyproject.toml       22 lines    Project config + ruff
Dockerfile           11 lines    Docker build (python:3.13-slim + static ffmpeg + uv)
docker-compose.yml   28 lines    kokoro-tts (GPU profile) + poc service
STANDARDS.md         15 lines    Coding standards
.gitignore           15 lines    Python + output + audio files
uv.lock            auto          Dependency lockfile
```
