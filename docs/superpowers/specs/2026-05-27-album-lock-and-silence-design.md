# Album lock + silence gating

## Background

Two related failure modes have been observed during full-album vinyl playback:

1. **Sparse tracks get skipped on Last.fm.** *Live At The Copacabana Palace* — "Dona Olimpia" (B2, 171 s, 65 208 hashes — lowest density on the album) was detected only 5 times across ~50 listen frames, only 3 of those above `MIN_PROMOTE_SCORE=10`, and those 3 were too far apart for the 3-frame stability buffer. Sequential promotion didn't help because the album's `track_number` metadata was incomplete at the time. The user's expectation is that the *next* track on the same album is far more probable than a random match, and the matcher should reflect that.

2. **Spurious matches when no record is playing.** When the needle lifts or the record ends, the mic captures room tone / surface noise / runout groove. The fingerprinter still produces hashes and the matcher occasionally finds weak alignments — e.g. an Angelo Badalamenti / Twin Peaks track briefly promoted to "now playing" mid-session (log line ~2471), then was cancelled. These false promotions pollute the now-playing UI and risk spurious scrobbles.

The fix is to give the server **context**: which album is on the turntable right now, and whether the current audio is actually music.

## Goals

- Substantially reduce spurious "now playing" promotions from silence / surface noise.
- Promote sparse on-album tracks reliably when an album is in progress.
- Handle vinyl-specific patterns: side flips, brief between-track silence, full-album playthroughs.
- Stay robust against incomplete `track_number` metadata (the original Dona Olimpia root cause).
- No client-side changes; no DB schema changes.

## Non-goals

- No manual album-selection UI. Auto-lock from the first confident match.
- No per-album threshold tuning. One set of constants applies everywhere.
- No changes to the fingerprinting algorithm or matcher scoring math.
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

Rationale for two gates: RMS catches dead silence cheaply (saves fingerprinting cost); hash-density catches the harder case of vinyl crackle / runout groove that has real audio energy but no stable spectral peaks. Surface noise can still occasionally produce enough hashes to slip through both gates — the album-lock + cross-album-release logic below is the second line of defence.

### 2. Album lock state

Four new fields on `NowPlayingService`:

```python
self._locked_album_id: int | None = None
self._silence_streak: int = 0
self._session_played: set[int] = set()
self._album_layout_cache: dict[int, AlbumLayout] = {}
```

The lock is set inside `_promote()`: whenever a candidate is promoted, if `candidate.album_id != self._locked_album_id`, the lock moves to the new album and `_session_played` is reset to `{candidate.track_id}`. If the lock already matches, `candidate.track_id` is added to `_session_played`. The very first promotion of a session establishes the lock.

**Session-played tracking is owned by `NowPlayingService` itself, not by `lastfm.py`.** It is updated on `_promote()` so the album logic works whether Last.fm is enabled, disabled, or fails. Last.fm and Roon remain pure subscribers — neither calls into `NowPlayingService` to record state. (Spurious brief promotes would mark a track played, but the silence gates and lock should make those rare; the cost is only that a wrongly-marked track loses its side-flip boost on the next pass, which is benign.)

The lock is cleared by the release triggers in §4 and by the cleanup hooks in §7.

### 3. Score boosting

A new method `NowPlayingService.apply_boosts(candidates)` returns a re-sorted list of candidates with adjusted scores plus a parallel list of boost metadata for logging. Called from the listen handler between `match_hashes` and `feed`.

Boost factors:

| Tier | Boost | Condition |
|---|---|---|
| Expected next track | **×2.5** | locked AND candidate matches the expected-next predicate (below) |
| On locked album | **×1.5** | locked AND `candidate.album_id == _locked_album_id` |
| Off-album / unlocked | **×1.0** | everything else |

**Score type.** `MatchCandidate.score` and `NowPlayingResponse.score` are typed `int`, so boosting cannot produce floats. The boosted score is `math.ceil(raw_score * boost_factor)` — preserves relative ranking, keeps the type contract, and never decreases a score. `apply_boosts` produces new `MatchCandidate` instances via `model_copy(update={"score": boosted_int})`. Raw score and boost factor are returned in a parallel `list[BoostInfo]` (a small dataclass with `raw_score: int`, `boost: float`) so the listen handler can log both:

```
Listen: Azymuth - Dona Olimpia (score:13 raw:5 boost:×2.5, conf:1.25, 425ms)
```

**Expected-next-track predicate** (`_is_expected_next(candidate)`):

Let `T` be the reference track — `self._current` if status is `playing`, else `self._last_played`. If `T` is `None`, no track is expected.

Each track gets an `effective_track_number` derived from the album layout cache (§3a). The predicate uses `effective_track_number` everywhere, falling back gracefully when stored `track_number` is missing.

- **Default case** (no side flip): expected next = the track on `T.album_id` with `effective_track_number == T.effective_track_number + 1`.
- **Side-flip case**: if `T` is the last track of its side (`T.effective_track_number` is the maximum among tracks on `T.side` for `T.album_id`), AND `T.side` is non-null, AND `_silence_streak >= SILENCE_FRAMES_FOR_FLIP` (start at **4 frames** ≈ 12 s — enough time to flip a record), expected next = *every* track on `T.album_id` whose `side` is non-null AND different from `T.side` AND whose `track_id` is not in `_session_played`. Bonus / no-side tracks never get the side-flip boost (they still get the on-album ×1.5).

`_is_sequential_track` (the existing fast-promote-on-first-match path) is updated to use the same `effective_track_number`, so sequential promotion works even when stored `track_number` is missing on either side. This is the direct fix for the original Dona Olimpia root cause.

### 3a. Album layout cache and `effective_track_number`

A per-album layout is computed lazily the first time `apply_boosts` or `_is_sequential_track` needs it for that album, and cached in `_album_layout_cache`:

```python
@dataclass
class AlbumLayout:
    by_track_id: dict[int, AlbumTrackEntry]
    sides: dict[str | None, list[AlbumTrackEntry]]  # side → ordered tracks

@dataclass
class AlbumTrackEntry:
    track_id: int
    album_id: int
    side: str | None
    position: str | None
    track_number: int | None
    effective_track_number: int   # always non-null
```

**Deriving `effective_track_number`:**

1. Fetch all tracks for the album from the DB.
2. Sort them by a composite key:
   - Primary: `side` (None last; otherwise lexicographic — `"A" < "B" < ...`).
   - Secondary: `track_number` if set, else parsed numeric suffix of `position` (e.g. `"A3"` → `3`), else 0.
   - Tertiary: `track_id` as a stable tiebreaker.
3. Assign `effective_track_number = 1, 2, 3, …` in that order.

`effective_track_number` is the *album-wide* sequence used by `_is_expected_next` and `_is_sequential_track`. The "last track of its side" check uses the `sides` map. Tracks with `side=None` (e.g. the Outtake bonus) are ordered last and treated as their own side bucket — the side-flip logic naturally skips them.

**Cache invalidation.** A new method `clear_album_cache(album_id: int | None = None)` drops the entry (or the whole cache if `None`). It is called from:

- `PUT /albums/{album_id}` and `PUT /tracks/{track_id}` handlers.
- `DELETE /albums/{album_id}` and `DELETE /tracks/{track_id}` handlers (these also trigger the `_on_album_deleted` / `_on_track_deleted` cleanup in §7).
- Any Discogs metadata application path.

### 4. Matcher hint extension

The matcher's `hint_track_id: int | None` parameter is replaced with `hint_track_ids: Iterable[int] | None`. The injection logic stays the same per track: any hinted track that received votes but fell below `CONFIG.min_count` is re-introduced into the candidate list with its accumulated score.

The listen handler builds the hint set:

```python
hints: set[int] = set()
cur = now_playing.current_track_id()
if cur is not None:
    hints.add(cur)
hints.update(now_playing.expected_next_track_ids())
```

`expected_next_track_ids()` is a new public method on `NowPlayingService` that returns the candidate track IDs the expected-next predicate (§3) would boost — the single sequential-next track in the default case, or the set of unplayed other-side tracks in the side-flip case. Empty set when no lock is held or no reference track exists.

This is the critical fix the previous draft missed: **the boost layer can only re-rank candidates the matcher surfaces**, so the expected-next tracks must be exempted from the `min_count` cut. Hinting is bounded (current + a small number of expected-next tracks per album), so the cost is negligible.

### 5. Lock release

Two triggers, either fires:

**Trigger A — sustained silence.** Each feed checks `_silence_streak >= SILENCE_FRAMES_FOR_RELEASE` (start at **20 frames** ≈ 60 s). When the threshold is crossed:

- Clear `_locked_album_id`.
- Clear `_session_played`.
- Clear `_last_played` (so sequential / expected-next logic is dormant until a new lock).

**Trigger B — different album wins stability.** Inside `_evaluate_stability`, when a candidate is about to be promoted via the stability buffer (the 2-of-3 path), check whether `candidate.album_id != self._locked_album_id`. If so, clear the lock *before* calling `_promote()` (which then re-establishes the lock on the new album, per §2). Because candidates have already been boosted, an off-album candidate winning the buffer means it genuinely beat the boosted on-album candidates — exactly the "user swapped records mid-session" case.

The sequential-promotion path (`_is_sequential_track`) does not need a cross-album check; sequential by definition only fires for the same album as `_last_played`, so it cannot promote an off-album track.

### 6. Integration with existing state machine

All existing logic stays:

- `_evaluate_stability` 2-of-3 buffer with `BUFFER_SIZE=3`, `REQUIRED_MATCHES=2`.
- `MIN_PROMOTE_SCORE=10`, `MIN_MAINTAIN_SCORE=4`.
- `GRACE_MISSES=6`, idle timeouts (10 s listening, 120 s playing).
- Maintain path early-return when current track is in candidates at score ≥ 4.

The boost layer changes only the scores `feed()` sees. The silence gates change only whether `feed()` is called with empty candidates. The lock state is read by `apply_boosts()` and the cross-album check; it is written only in `_promote()` and the cleanup paths.

### 7. Lock and session cleanup

Lock + session state is cleared whenever it would otherwise become stale:

- **Idle countdown fires** (`_idle_countdown`): in addition to today's `status = idle` and `_current = None` reset, also clear `_locked_album_id`, `_session_played`, `_silence_streak`. This closes the window where the user simply walked away — no further feeds, status drops to idle, but the lock would otherwise persist into the next session.
- **Album deleted** (`DELETE /albums/{album_id}`): a new `on_album_deleted(album_id)` method clears the lock and session if `_locked_album_id == album_id`, drops the album from the layout cache.
- **Track deleted** (`DELETE /tracks/{track_id}`): a new `on_track_deleted(track_id, album_id)` method removes the track from `_session_played` and invalidates the cached layout for `album_id` (since `effective_track_number` will shift).
- **Discogs metadata applied** to an album: invalidate that album's layout cache via `clear_album_cache(album_id)`; do not clear the lock.
- **Album / track edits** (`PUT`): invalidate the cache for the affected album; do not clear the lock.

### 8. Tunable constants

In `server/app/state.py`, alongside existing constants:

```python
SILENCE_RMS_DBFS = -40.0
HASH_MIN_COUNT = 150
BOOST_ON_ALBUM = 1.5
BOOST_EXPECTED_NEXT = 2.5
SILENCE_FRAMES_FOR_FLIP = 4
SILENCE_FRAMES_FOR_RELEASE = 20
```

These are starting values. The listen log shows raw score, boost factor, and silence reasons, so retuning from real-world play sessions is straightforward without code changes beyond the constants.

## Code touch points

- **`server/app/main.py`** — RMS gate + hash-density gate + `apply_boosts` call in the listen handler; build the hint set from `current_track_id()` + `expected_next_track_ids()` and pass to `match_hashes`. Update the `Listen:` log line to include raw score and boost factor. Wire `clear_album_cache` / `on_album_deleted` / `on_track_deleted` into the album/track CRUD endpoints and the Discogs metadata application path.
- **`server/app/state.py`** — add `_locked_album_id`, `_silence_streak`, `_session_played`, `_album_layout_cache`; add methods `note_silence()`, `apply_boosts()`, `expected_next_track_ids()`, `clear_album_cache()`, `on_album_deleted()`, `on_track_deleted()`, `_is_expected_next()`, `_album_layout()`; update `_promote()` to set lock and update `_session_played`; update `_evaluate_stability()` for cross-album release; update `_is_sequential_track()` to use `effective_track_number`; update `_idle_countdown` to clear lock state; reset silence streak in `feed()` when candidates are non-empty.
- **`server/app/matcher.py`** — change `hint_track_id: int | None` to `hint_track_ids: Iterable[int] | None`; iterate the hint set when re-injecting below-threshold tracks. Logging updated to show the hint set.
- **`server/app/lastfm.py`** — *no changes.* Last.fm remains a pure subscriber; remove any planned `mark_played` coupling.

No changes to:

- `server/app/fingerprint.py` (RMS computation lives in `main.py` — it operates on decoded PCM, which is part of the listen-handler concern)
- `server/app/db.py`
- `server/app/models.py` (raw score and boost ride in a sidecar dataclass, not on the model)
- `server/app/roon.py`
- Android client
- Web UI
- DB schema

## Testing

New cases in `server/tests/test_state.py`:

- `test_silence_increments_streak` — `note_silence()` then `feed([])` increments `_silence_streak`; a non-silent feed resets it.
- `test_lock_set_on_first_promote` — first promotion sets `_locked_album_id` and records the track in `_session_played`.
- `test_lock_change_resets_session_played` — promote a track from album X, then promote a track from album Y → `_session_played == {Y_track_id}`.
- `test_apply_boosts_off_album_unchanged` — no lock → all returned scores equal their raw values.
- `test_apply_boosts_on_album` — locked → on-album candidate's adjusted score equals `ceil(raw * 1.5)` and stays `int`.
- `test_apply_boosts_expected_next_sequential` — locked, `_last_played = T`, candidate = `effective_track_number + 1` same album → `ceil(raw * 2.5)`.
- `test_apply_boosts_side_flip` — `_last_played` = last track of side B, `_silence_streak >= SILENCE_FRAMES_FOR_FLIP`, candidate = unplayed track on side A → ×2.5.
- `test_apply_boosts_side_flip_requires_silence` — same setup but silence streak below threshold → only ×1.5.
- `test_apply_boosts_side_flip_excludes_already_played` — already-played side-A tracks get only ×1.5, not ×2.5.
- `test_apply_boosts_score_is_int` — boosted score with `raw=3, boost=2.5` returns `8` (ceil), never `7.5`.
- `test_effective_track_number_falls_back_to_position` — album where some tracks have `track_number=None` but `position` set → all tracks get sequential `effective_track_number` covering the full album.
- `test_sequential_promote_works_with_missing_track_number` — the exact Dona Olimpia regression: ref track has `track_number=None` but `position="B1"`, candidate has `track_number=None` but `position="B2"` → `_is_sequential_track` returns True and promote fires on first frame.
- `test_expected_next_track_ids_default` — returns the single sequential next track id.
- `test_expected_next_track_ids_side_flip` — returns all unplayed other-side track ids when silence streak ≥ flip threshold.
- `test_lock_release_on_sustained_silence` — `_silence_streak` reaches release threshold → lock, session, last_played all cleared.
- `test_lock_release_on_cross_album_stability` — stability buffer fills with off-album candidate (boosted) → lock moves to the new album, `_session_played` reset.
- `test_idle_countdown_clears_lock` — `_idle_countdown` firing clears `_locked_album_id`, `_session_played`, `_silence_streak`.
- `test_on_album_deleted_clears_lock_if_locked` — deleting the locked album clears the lock and drops the cache entry; deleting a different album does not.
- `test_on_track_deleted_invalidates_layout_and_session` — deleting a track removes it from `_session_played` and the layout cache.
- `test_sparse_track_with_expected_next_boost_end_to_end` — locked album, last_played = Jazz Carnival (B1), matcher returns Dona Olimpia (B2) at raw score 5 because of the matcher hint, `apply_boosts` lifts it to `ceil(5 * 2.5) = 13`, promote fires via sequential.

New cases in `server/tests/test_matcher.py`:

- `test_hint_track_ids_injects_each_below_threshold_track` — multiple hinted tracks each below `min_count` are all surfaced in the result list with their accumulated scores.
- `test_hint_track_ids_none_behaves_like_old_hint_track_id_none` — back-compat for the no-hint case.

New cases in `server/tests/test_api.py` (or wherever the listen handler is currently tested):

- `test_listen_silent_audio_skips_fingerprint` — POST /listen with a low-RMS WAV → `fingerprint_audio` not called, NowPlayingService fed `[]`, silence streak incremented.
- `test_listen_low_hash_density_discards_candidates` — synthetic WAV that produces few hashes → matcher not called, NowPlayingService fed `[]`, silence streak incremented.
- `test_listen_passes_expected_next_hints_to_matcher` — when a track is locked + playing, the matcher receives a hint set containing the current track and the expected next.

## Rollout

- Feature lives entirely server-side; no version coordination with the Android client.
- Default constants are conservative (silence thresholds err on the side of "this is music"; boosts are modest). After a few play sessions, retune from the logs.
- The Dona Olimpia regression test and the position-fallback test together give a clear pass/fail signal that both the sparse-track and the missing-metadata cases are addressed.

## Open questions

None at this point; all decisions were made during brainstorming and review.
