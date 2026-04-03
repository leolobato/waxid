# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WaxID is a local "Shazam for vinyl" — it identifies vinyl records playing on a turntable by matching microphone audio against a personal music catalog. Three components: a Python/FastAPI matching server, ingestion scripts, and client apps (Android tablet + legacy macOS).

## Build & Run Commands

### Server

```bash
cd server && ./run_local.sh          # Local dev (auto-reload on :8457)
cd server && docker-compose up --build  # Docker

cd server && pytest tests/           # All tests
cd server && pytest tests/test_state.py  # Single test file
cd server && pytest tests/test_api.py -k test_match  # Single test by name
```

Requires Python 3.12+, ffmpeg. Database stored at `server/data/fingerprints.db` (SQLite, WAL mode). Pytest configured with `asyncio_mode = "auto"` in `pyproject.toml`.

### Android

```bash
cd client/android
./gradlew assembleDebug          # Build
./gradlew installDebug           # Install to connected device
./gradlew test                   # Unit tests
adb install -r app/build/outputs/apk/debug/app-debug.apk  # Install via adb
```

Kotlin 2.0+, Compose Material 3, targets SDK 35 (min 29). Java 17 target.

### macOS Client (legacy)

```bash
cd client/macos
xcodegen generate                # Generate .xcodeproj from project.yml
```

SwiftUI, macOS 14.0+. Largely superseded by the Android client.

### Ingestion Scripts

```bash
cd scripts
python ingest.py /path/to/album --server http://localhost:8457
python batch_inject.py export.csv --server http://localhost:8457
```

## Architecture

### Audio Fingerprinting Pipeline

Audio → resample to 11025 Hz → STFT (512 FFT, 256 hop) → high-pass filter (0.98 pole) → spectral peak detection → landmark pairing (fanout=3) → 22-bit hashes stored in SQLite.

Matching uses offset voting: query hashes are looked up, time-offset histograms are built per track, and tracks exceeding `min_count=15` votes are returned ranked by score.

Key tuning parameters are in `server/app/config.py` (`FingerprintConfig` dataclass).

### Server Structure

- `server/app/main.py` — FastAPI routes and app lifecycle
- `server/app/fingerprint.py` — Audio preprocessing and landmark extraction (audfprint algorithm)
- `server/app/matcher.py` — Hash lookup and offset voting
- `server/app/db.py` — SQLite schema, queries, and migrations
- `server/app/state.py` — Now-playing state management (stability buffer, grace period, idle timeout)
- `server/app/models.py` — Pydantic models for all API request/response types
- `server/app/discogs.py` — Discogs API integration for metadata (3-second rate limit)

### Web UI

Single-page app at `server/web/` served at `/`. Alpine.js for reactivity, vanilla CSS.

- `index.html` — Four views: now-playing, library, album-detail, upload
- `js/app.js` — `waxidApp()` Alpine data function with SSE connection, CRUD, file upload
- `css/style.css` — Dark theme, responsive at 768px breakpoint

The now-playing view connects to `/now-playing/stream` (SSE) for real-time updates. Three states: idle, listening, playing. The web UI detects the Android client via `WaxID-Android` user agent string and shows additional controls (listen toggle, logout).

### Server API (port 8457)

- `POST /ingest`, `POST /ingest/bulk` — Add tracks (audio + metadata)
- `POST /match` — One-shot audio matching (returns results, does NOT update now-playing)
- `POST /listen` — Continuous listening (async, returns 202, feeds into now-playing state)
- `GET /now-playing` — Current playback state
- `GET /now-playing/stream` — SSE stream of now-playing updates
- CRUD endpoints for `/albums` and `/tracks`

### Android Client

Jetpack Compose app, no XML layouts. Two states: unconfigured (setup screen with server URL input) and configured (full-screen WebView showing the server's web UI).

**Services:**
- `ListeningService` — Foreground service that captures audio via `AudioCaptureManager` (10-second circular buffer, mono 16-bit PCM). `MatchClient` sends WAV chunks to `/listen` every 3 seconds.
- `ControlService` — Local Ktor HTTP server on port 8458 with `POST /start`, `POST /stop`, `GET /status` for remote control.

**WebView bridge:** The WebView injects a `WaxID` JavaScript interface with `startListening()`, `stopListening()`, `openSettings()`, `isListening()` methods. Custom user agent includes `WaxID-Android/1.0`.

**Configuration:** `Config.kt` loads defaults from `assets/config.properties`, runtime overrides via SharedPreferences. The `isConfigured` flag tracks whether the user has set a server URL.

**Roon integration:** `NowPlayingService` watches `MatchClient.state` and pushes to Roon API. Configured via SharedPreferences (`roon_url`, `roon_enabled`).

### Data Flow

1. **Ingest**: Audio files → `ingest.py`/`batch_inject.py` → `POST /ingest` → fingerprint → hashes stored in SQLite
2. **Listen**: Client records audio → `POST /listen` (fire-and-forget, 202) → server fingerprints → hash lookup → offset voting → now-playing state updated → SSE to web UI
3. **Now-playing state**: Server-side `NowPlayingService` uses a 3-frame stability buffer (2-of-3 match required), 6-miss grace period before dropping, 10s idle timeout when listening

### Database

SQLite with three tables: `albums` (artist/name/year/discogs_url/cover), `tracks` (album_id/artist/track/side/position), `hashes` (hash/track_id/t_frame). Indexed on `hashes(hash, track_id, t_frame)`. Optimized with WAL mode, memory-mapped I/O (3GB), and large cache (256MB).
