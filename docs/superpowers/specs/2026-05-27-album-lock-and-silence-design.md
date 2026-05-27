# Album lock + silence gating

## Background

Two related failure modes have been observed during full-album vinyl playback:

1. **Sparse tracks get skipped on Last.fm.** *Live At The Copacabana Palace* — "Dona Olimpia" (B2, 171 s, 65 208 hashes — lowest density on the album) was detected only 5 times across ~50 listen frames, only 3 of those above `MIN_PROMOTE_SCORE=10`, and those 3 were too far apart for the 3-frame stability buffer. Sequential promotion didn't help because the album's `track_number` metadata was incomplete at the time. The user's expectation is that the *next* track on the same album is far more probable than a random match, and the matcher should reflect that.

2. **Spurious matches when no record is playing.** When the needle lifts or the record ends, the mic captures room tone / surface noise / runout groove. The fingerprinter still produces hashes and the matcher occasionally finds weak alignments — e.g. an Angelo Badalamenti / Twin Peaks track briefly promoted to "now playing" mid-session (log line ~2471), then was cancelled. These false promotions pollute the now-playing UI and risk spurious scrobbles.

The fix is to give the server **context**: which album is on the turntable right now, and whether the current audio is actually music.

## Goals

- Eliminate spurious "now playing" promotions from silence / surface noise.
- Promote sparse on-album tracks reliably when an album is in progress.
- Handle vinyl-specific patterns: side flips, brief between-track silence, full-album playthroughs.
- No client-side changes; no DB schema changes.

## Non-goals

- No manual album-selection UI. Auto-lock from the first confident match.
- No per-album threshold tuning. One set of constants applies everywhere.
- No changes to the fingerprinting algorithm or matcher scoring internals.
- No retroactive backfill of missing `track_number` metadata. (Tracked separately.)

## Design

### 1. Silence detection in `/listen`

Two cheap gates wrap the existing fingerprint → match pipeline inside the listen handler in `server/app/main.py`.

**Gate 1 — RMS energy (pre-fingerprint).** Compute RMS of the decoded PCM. If `rms_dbfs < SILENCE_RMS_DBFS` (start at **-40 dBFS** — below typical vinyl surface noise but above dead room tone), skip `fingerprint_audio` entirely. Feed `[]` to `NowPlayingService.feed()`. Log:

```
Listen: silence (rms=-52.3 dBFS)
```

**Gate 2 — Hash density (post-fingerprint).** After `fingerprint_audio` runs, if `len(query_hashes) < HASH_MIN_COUNT` (start at **150** for a 3-second chunk; current music frames produce 600–900 hashes after stoplist), discard candidates without calling the matcher. Feed `[]`. Log:

```
Listen: low hash density (hashes=87)
```

Both gates also call `NowPlayingService.note_silence()`, which increments the silence streak counter. Any non-silent feed resets the streak to 0.

Rationale for two gates: RMS catches dead silence cheaply (saves fingerprinting cost); hash-density catches the harder case of vinyl crackle / runout groove that has real audio energy but no stable spectral peaks.

### 2. Album lock state

Three new fields on `NowPlayingService`:

```python
self._locked_album_id: int | None = None
self._silence_streak: int = 0
self._session_scrobbled: set[int] = set()
```

The lock is set inside `_promote()`: whenever a candidate is promoted, if `candidate.album_id != self._locked_album_id`, the lock moves to the new album and `_session_scrobbled` is cleared. The very first promotion of a session establishes the lock.

The lock is cleared by the release triggers in §4.

`_session_scrobbled` is updated via a new `mark_played(track_id)` method that the Last.fm scrobbler calls after a successful scrobble.

### 3. Score boosting

A new method `NowPlayingService.apply_boosts(candidates)` returns a re-sorted copy of the candidates with adjusted scores. Called from the listen handler between `match_hashes` and `feed`.

Boost factors:

| Tier | Boost | Condition |
|---|---|---|
| Expected next track | **×2.5** | locked AND candidate matches the expected-next predicate (below) |
| On locked album | **×1.5** | locked AND `candidate.album_id == _locked_album_id` |
| Off-album / unlocked | **×1.0** | everything else |

`apply_boosts` returns a new list of `MatchCandidate` instances (via `model_copy(update={"score": boosted})`) sorted by boosted score descending. The boosted `score` is what `_evaluate_stability`, `_is_sequential_track`, and the promote/maintain thresholds consume — no further changes inside `feed()` are needed. The raw score and applied boost factor are returned in a parallel `list[tuple[int, float]]` so the listen handler can log both:

```
Listen: Azymuth - Dona Olimpia (score:25 raw:10 boost:×2.5, conf:1.25, 425ms)
```

**Expected-next-track predicate** (`_is_expected_next(candidate)`):

Let `T` be the reference track — `self._current` if status is `playing`, else `self._last_played`. If `T` is `None` or has no `track_number`, no track is "expected next".

- **Default case** (no side flip): expected next = the track on `T.album_id` with `track_number == T.track_number + 1`.
- **Side-flip case**: if `T` is the last track of its side (i.e., `T.track_number` is the maximum `track_number` among tracks on `T.side` for `T.album_id`), AND `_silence_streak >= SILENCE_FRAMES_FOR_FLIP` (start at **4 frames** ≈ 12 s — enough time to flip a record), expected next = *every* track on `T.album_id` whose side is different from `T.side` AND whose `track_id` is not in `_session_scrobbled`.

The "last track of side" check requires looking up the album's side layout. Two options:

1. Cache album side layouts in `NowPlayingService` on first reference, invalidated when an album is edited.
2. Query the DB on each `apply_boosts` call.

Option 1 is the choice — album metadata changes rarely; cache invalidation is via a `clear_album_cache(album_id)` method called from the album/track update endpoints. The cache is `dict[int, dict[str, list[Track]]]` keyed by `album_id → side → tracks-on-that-side`.

### 4. Lock release

Two triggers, either fires:

**Trigger A — sustained silence.** Each feed checks `_silence_streak >= SILENCE_FRAMES_FOR_RELEASE` (start at **20 frames** ≈ 60 s). When the threshold is crossed:

- Clear `_locked_album_id`.
- Clear `_session_scrobbled`.
- Clear `_last_played` (so sequential / expected-next logic is dormant until a new lock).
- The existing `_idle_countdown` continues to drive the `idle` status transition independently.

**Trigger B — different album wins stability.** Inside `_evaluate_stability`, when a candidate is about to be promoted via the stability buffer (the 2-of-3 path), check whether `candidate.album_id != self._locked_album_id`. If so, clear the lock *before* calling `_promote()` (which then re-establishes the lock on the new album). Because the candidates have already been boosted, an off-album candidate winning the buffer means it genuinely beat the boosted on-album candidates — exactly the "user swapped records mid-session" case.

The sequential-promotion path (`_is_sequential_track`) does not need a cross-album check; sequential by definition only fires for the same album as `_last_played`, so it cannot promote an off-album track.

### 5. Integration with existing state machine

All existing logic stays:

- `_evaluate_stability` 2-of-3 buffer with `BUFFER_SIZE=3`, `REQUIRED_MATCHES=2`.
- `MIN_PROMOTE_SCORE=10`, `MIN_MAINTAIN_SCORE=4`.
- Sequential promotion via `_is_sequential_track` (unchanged).
- `GRACE_MISSES=6`, idle timeouts (10 s listening, 120 s playing).
- Maintain path early-return when current track is in candidates at score ≥ 4.

The boost layer changes only the scores `feed()` sees. The silence gates change only whether `feed()` is called with empty candidates. The lock state is read by `apply_boosts()` and the cross-album check; it is written only in `_promote()` and the release triggers.

### 6. Tunable constants

In `server/app/state.py`, alongside existing constants:

```python
SILENCE_RMS_DBFS = -40.0
HASH_MIN_COUNT = 150
BOOST_ON_ALBUM = 1.5
BOOST_EXPECTED_NEXT = 2.5
SILENCE_FRAMES_FOR_FLIP = 4
SILENCE_FRAMES_FOR_RELEASE = 20
```

These are starting values. The listen log shows raw score, boost, and silence reasons, so retuning from real-world play sessions is straightforward without code changes beyond the constants.

## Code touch points

- **`server/app/main.py`** — RMS gate, hash-density gate, `apply_boosts` call in the listen handler. Update the `Listen:` log line to include raw score and boost factor.
- **`server/app/state.py`** — add `_locked_album_id`, `_silence_streak`, `_session_scrobbled`, `_album_side_cache`; add methods `note_silence()`, `apply_boosts()`, `mark_played()`, `clear_album_cache(album_id)`, `_is_expected_next(candidate)`; update `_promote()` to set lock and reset session-scrobbled when album changes; update `_evaluate_stability()` for cross-album release; reset silence streak in `feed()` when candidates are non-empty.
- **`server/app/lastfm.py`** — call `now_playing.mark_played(track_id)` after a successful `track.scrobble`.
- **`server/app/main.py`** (album / track update endpoints) — call `now_playing.clear_album_cache(album_id)` when album or track metadata changes.

No changes to:

- `server/app/fingerprint.py`
- `server/app/matcher.py`
- `server/app/db.py`
- `server/app/models.py` (raw score is carried via sidecar dict in `state.py`, not a model field)
- Android client
- Web UI
- DB schema

## Testing

New cases in `server/tests/test_state.py`:

- `test_silence_increments_streak` — `feed([])` after `note_silence()` increments `_silence_streak`; non-silent feed resets it.
- `test_lock_set_on_first_promote` — first promotion sets `_locked_album_id`.
- `test_lock_change_clears_session_scrobbled` — promote A1 from album X, mark_played, promote B1 from album Y → session_scrobbled cleared.
- `test_apply_boosts_off_album_unchanged` — no lock → all boosts are 1.0.
- `test_apply_boosts_on_album` — locked → on-album candidate scores ×1.5.
- `test_apply_boosts_expected_next_sequential` — locked, last_played = T, candidate = T.track_number+1 same album → ×2.5.
- `test_apply_boosts_side_flip` — last_played = last track of side B, silence streak ≥ flip threshold, candidate = unplayed track on side A → ×2.5.
- `test_apply_boosts_side_flip_requires_silence` — same setup but silence streak below threshold → only the ×1.5 on-album boost applies.
- `test_apply_boosts_side_flip_excludes_already_scrobbled` — already-played side-A tracks get only ×1.5, not ×2.5.
- `test_lock_release_on_sustained_silence` — `_silence_streak` crosses release threshold → lock cleared, last_played cleared.
- `test_lock_release_on_cross_album_stability` — stability buffer fills with off-album candidate (boosted scores still in favor) → lock moves to the new album.
- `test_sparse_track_with_expected_next_boost` — recreate the Dona Olimpia scenario: locked album, last_played = Jazz Carnival (B1, #5), candidate = Dona Olimpia (B2, #6) at raw score 5 → boosted to 12.5 ≥ MIN_PROMOTE_SCORE and promoted via sequential.

New cases in `server/tests/test_api.py` (or `test_main.py`, wherever the listen handler is currently tested):

- `test_listen_silent_audio_skips_fingerprint` — POST /listen with a low-RMS WAV → `fingerprint_audio` not called, NowPlayingService fed `[]`.
- `test_listen_low_hash_density_discards_candidates` — synthetic WAV that produces few hashes → matcher not called, NowPlayingService fed `[]`.

## Rollout

- Feature lives entirely server-side; no version coordination with the Android client.
- Default constants are conservative (silence thresholds err on the side of "this is music"; boosts are modest). After a few play sessions, retune from the logs.
- The Dona Olimpia regression test gives a clear pass/fail signal that the sparse-track fix works.

## Open questions

None at this point; all decisions were made during brainstorming.
