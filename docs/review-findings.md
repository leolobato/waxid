# Algorithm Review Findings

Review of the fingerprinting pipeline, matcher, and now-playing state machine
(v1.4.0, 2026-07-01). Scope: `server/app/fingerprint.py`, `matcher.py`,
`state.py`, `db.py`, `main.py` listen loop, and the Roon/Last.fm consumers,
cross-checked against `tests/test_state.py` to avoid flagging intended behavior.

**Verdict:** the algorithm is a faithful, well-tested audfprint implementation
and the matcher's vectorized offset voting is solid. One real bug (a race that
breaks `finished_track`), two functional gaps (no anchor re-sync, a cache
invalidation hole), and several concrete wins for match quality and resource
usage.

## Bugs

### 1. `finished_track` is consumed by the wrong subscriber (race)

`subscribe()` clears `self._finished = None` right after `get_state()`
(`server/app/state.py:216`). The Roon notifier and Last.fm scrobbler are *also*
subscribers on the same condition — `notify_all` wakes every waiter, and
whichever resumes first consumes the one-shot. Since Roon/Last.fm subscribe at
startup (before any browser), they typically win, and an SSE client sees
`finished_track: null`.

Nothing in the web UI or Android consumes the field yet, so the bug is latent —
but the v1.4.0 "report track completion" feature is effectively broken the
moment Roon or Last.fm is enabled. Inconsistently, polling `GET /now-playing`
returns the field repeatedly *without* clearing it.

**Fix:** per-subscriber delivery — a monotonic sequence number each subscriber
tracks, or per-subscriber queues — instead of a shared field cleared by the
first reader.

### 2. Playback anchor is never re-synced

`_promote` sets `_anchor_time`/`_anchor_offset` once; after that, when the
stable winner *is* the current track, `_advance` returns immediately
(`server/app/state.py:331-333`) and the fresh `offset_s` on every confirming
frame is discarded.

Consequences:

- Repositioning the needle within the same track leaves `elapsed_s` wrong
  indefinitely (Roon seek position, track-end detection).
- Turntable speed drift accumulates, so `_check_track_ended` can fire early on
  a still-playing track. The track then re-promotes 2 frames later and
  `_mark_finished` fires twice for one physical play. Scrobbling is safe (the
  scrobbler dedupes via `_last_scrobbled_track_id`), but `finished_track`
  consumers would double-count.

**Fix:** when the current track confirms with a decent score, compare the
measured `offset_s` against the predicted elapsed and re-anchor if they diverge
by more than a few seconds. This also handles within-track needle drops.

### 3. Album layout cache not invalidated on ingest

`update_track`, `apply_discogs`, and the delete paths all call
`clear_album_cache`, but `/ingest` and `/ingest/bulk` don't
(`server/app/main.py:453`, `main.py:468`). Ingesting more tracks into an album
while it's playing leaves `expected_next_track_ids()` and side progress on the
stale layout until restart. One-line fix at each ingest site.

### Doc/coupling nits

- CLAUDE.md says `min_count=15`; the actual default is 6
  (`server/app/config.py:18`).
- `MIN_PROMOTE_SCORE = 6  # = matcher min_count` (`server/app/state.py:9`)
  silently breaks if `WAXID_MIN_COUNT` is set — derive one from the other.
- The `array("q")` comment in `server/app/db.py:219` says "unsigned"; `q` is
  signed (harmless).

## Better matching (fingerprint quality)

### No threshold spreading in `find_peaks`

audfprint raises the threshold envelope with a Gaussian *around* each accepted
peak; here `threshold[freq] = val` touches only the exact bin
(`server/app/fingerprint.py:74`). Two local maxima 2 bins apart both survive,
producing near-duplicate landmarks — a bigger hash table and more collision
noise in offset voting. The backward prune only catches the
weaker-earlier/stronger-later case within ±3 bins.

Spreading the threshold over ±3–4 bins with a decaying profile is probably the
single best quality/size improvement available.

### Multiplicative prune margin on log values

The backward prune's `val * 1.5` (`server/app/fingerprint.py:90`) multiplies a
*log*-magnitude — "1.5× the log" is a much harsher bar for strong peaks than
weak ones. It works (accepted peaks are always positive since the threshold
floor is 0), but an additive margin in log domain (`val + log(1.5)`) is what
"1.5× stronger" actually means.

### Leave the state-machine thresholds alone

Promote 6, maintain/sequential 4, cross-album margin 1.5× look well-tuned and
are heavily tested.

## Less resource usage (ordered by impact)

1. **3.3× redundant fingerprinting.** The client posts its full 10s buffer
   every 3s, so the server STFTs/peak-picks every audio second ~3.3 times. The
   overlap buys match robustness, but even trimming to an 8s window cuts
   steady-state CPU ~20% for free.
2. **Client sends native-rate audio (44.1/48 kHz)** —
   `client/android/.../AudioCaptureManager.kt:71` — so every chunk is ~880 KB
   (~2.4 Mbps) and the server runs `librosa.resample` on 10s of audio every 3s.
   Decimating to 11025 Hz on-device cuts payload 4× and removes the server
   resample entirely.
3. **`find_peaks` is pure Python** — ~110k inner iterations per 10s chunk,
   ~26M for a 40-minute album ingest. The column loop must stay sequential
   (threshold decay), but the frequency loop vectorizes cleanly:
   `mask = (f[1:-1] > f[:-2]) & (f[1:-1] > f[2:]) & (f[1:-1] > thr[1:-1])`,
   then top-5 by value. Expect 10–30× on the hottest function. Same idea for
   `compute_spectrogram`: replace the per-row `lfilter` loop with one
   `lfilter(b, a, sgram, axis=1)` call.
4. **Stoplist goes stale.** Built once at startup (`server/app/main.py:102`),
   so hashes that become too common after a big ingest aren't filtered until
   restart. Rebuild in the background after bulk ingest.
5. **Optional: in-RAM hash index.** For a personal catalog (~10–20M hashes ≈
   120–240 MB as int32 triplets), loading the hash table into hash-sorted numpy
   arrays at startup and using `searchsorted` removes SQLite (and the per-row
   Python streaming in `lookup_hashes_flat`) from the hot path entirely. Only
   worth it if match latency actually becomes a problem — the voting math is
   already the fast part.

## Expected-next logic

The hint mechanism is sound: hinted tracks ride along below `min_count`, the
`MIN_PROMOTE_SCORE` filter keeps hint junk out of the stability buffer, and the
evidence-streak expiry is carefully tested. Two refinements:

- **The sequential shortcut only inspects `candidates[0]`**
  (`server/app/state.py:345`). If a random cross-album track outscores the true
  next track by one vote during the between-track gap, the shortcut is lost even
  though the hinted next track qualifies — falling back to the slower 2-of-3
  stability path. Scanning candidates for *any* sequential track ≥
  `MIN_SEQUENTIAL_SCORE` (perhaps within some factor of the top score) would
  make side flips and track transitions snappier.
- **`confidence` can be inflated by hint junk.** It's computed as top/second
  (`server/app/matcher.py:179-182`), where "second" can be a hint-injected
  1-vote entry. Display-only; compute it against the best *non-hinted*
  runner-up.

## Suggested priority

1. Fix the `finished_track` race, anchor re-sync, and ingest cache invalidation.
2. Vectorize `find_peaks` + single-call `lfilter` (cheap, big CPU win).
3. Add threshold spreading (quality + DB size).
4. Client-side downsampling and/or smaller listen window.
