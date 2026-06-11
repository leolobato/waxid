# Track Promotion v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the album-lock/boost system with the simpler v2 "context" model so a new record locks within seconds instead of 2–3 tracks.

**Architecture:** The album context becomes implicit — it is simply `(current or last_played)` — and powers matcher hints, a sequential shortcut, and a sticky preference for the current track. Score boosting, side-flip machinery, session tracking, and explicit lock state are deleted. Promotion uses one raw-score bar (≥ 6, = `min_count`) with 2-of-3 stability counted across the *full* candidate list per frame, a sequential shortcut at raw ≥ 4, and a challenger guard (must outscore the current track; ×1.5 confidence margin for cross-album or no-context promotes). Context expires after 15 frames (~45s) without an on-album candidate at raw ≥ 6.

**Spec:** `docs/track-promotion-flow-v2.md` (including the 🔧 review amendments). Reference for current behavior: `docs/track-promotion-flow.md`.

**Tech Stack:** Python 3.12+, FastAPI, pytest (`asyncio_mode = "auto"`), SQLite. All commands run from `server/`.

**Verification command:** `cd server && pytest tests/` — must pass at the end of every task.

---

## Cheat sheet: v2 semantics

| Rule | Value |
|---|---|
| Frame buffer | last 3 frames; each frame = `dict[track_id, MatchCandidate]` of ALL candidates with raw score ≥ 6 |
| Stability promote | same track in ≥ 2 of 3 frames; among stable tracks the highest score wins; must pass challenger guard |
| Challenger guard | winner must outscore current's recent best (any album); cross-album or no-context additionally needs ≥ 1.5× the best other-album score in the buffer AND ≥ 1.5× current's recent best |
| Sequential shortcut | top raw candidate is the etn+1 track (side non-null) of `current or last_played`, score ≥ 4 → instant promote. Only evaluated when the current track is NOT alive in this frame (score ≥ 4) |
| Maintain | current track at raw ≥ 4 in the frame resets `miss_count`; 6 consecutive misses drop to listening |
| Evidence | context-album candidate at raw ≥ 6 resets `_no_evidence_streak`; silent frames and hint-junk frames increment it; at 15 (and nothing playing) `last_played` is cleared |
| Matcher | returns EVERY track ≥ `min_count` (no `max_results` truncation); hint injection unchanged |

---

### Task 1: Matcher returns every track ≥ min_count

The state machine will count every credible candidate per frame, so the matcher must stop truncating to the top 5.

**Files:**
- Modify: `server/app/matcher.py:173-181`
- Modify: `server/app/config.py:21` (remove `max_results`)
- Test: `server/tests/test_matcher.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_matcher.py`:

```python
def test_all_tracks_above_min_count_are_returned(db_with_track):
    """v2: the state machine counts every credible track per frame, so the
    matcher must not truncate results to a top-N."""
    db, track_id = db_with_track
    extra_ids = []
    for n in range(6):
        album_id, _ = db.insert_album(artist=f"X{n}", name=f"Album{n}")
        tid = db.insert_track(album_id=album_id, artist=f"X{n}",
                              album=f"Album{n}", track=f"Song{n}")
        # Same hash values at a distinct constant offset per track, so each
        # track accumulates ~20 aligned votes (well above min_count).
        db.insert_hashes([(1000 + i, tid, i * 5 + (n + 1) * 1000)
                          for i in range(10, 30)])
        extra_ids.append(tid)
    query_hashes = [(1000 + i, i * 5 - 50) for i in range(10, 30)]
    results = match_hashes(query_hashes, db)
    returned = {r["track_id"] for r in results}
    assert returned == {track_id, *extra_ids}, f"got only {len(returned)} tracks"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && pytest tests/test_matcher.py::test_all_tracks_above_min_count_are_returned -v`
Expected: FAIL — only 5 track_ids returned (the `max_results` cut).

- [ ] **Step 3: Remove the truncation**

In `server/app/matcher.py`, replace:

```python
    sorted_tracks = sorted(track_best.items(), key=lambda x: x[1][0], reverse=True)

    top_slice = sorted_tracks[:CONFIG.max_results]
    top_ids = {tid for tid, _ in top_slice}
    surviving_hints = [
        (tid, entry) for tid, entry in sorted_tracks[CONFIG.max_results:]
        if tid in hint_entries and tid not in top_ids
    ]
    final_tracks = top_slice + surviving_hints
```

with:

```python
    sorted_tracks = sorted(track_best.items(), key=lambda x: x[1][0], reverse=True)
    # v2: no top-N truncation — the state machine counts every credible
    # candidate per frame, and hinted tracks ride along at their raw votes.
    final_tracks = sorted_tracks
```

In `server/app/config.py`, delete the line:

```python
    max_results: int = 5
```

- [ ] **Step 4: Run matcher tests**

Run: `cd server && pytest tests/test_matcher.py -v`
Expected: all PASS (4 tests).

- [ ] **Step 5: Run the full suite to catch stragglers**

Run: `cd server && pytest tests/`
Expected: PASS. If anything references `CONFIG.max_results`, remove that reference (a `grep -rn "max_results" server/` should come back empty).

- [ ] **Step 6: Commit**

```bash
git add server/app/matcher.py server/app/config.py server/tests/test_matcher.py
git commit -m "Return every track above min_count from the matcher

- the v2 state machine counts all credible candidates per frame, so \`match_hashes\` no longer truncates to \`max_results\`
- hinted tracks no longer need special truncation-survival handling
- drop the now-unused \`max_results\` config field"
```

---

### Task 2: Core promotion rewrite — raw scores, full-frame stability, challenger guard

This is the heart of v2: delete the boost layer, hold full candidate frames in the buffer, promote on raw scores with the challenger guard, and drop the maintain early-return.

**Files:**
- Modify: `server/app/state.py` (major rewrite of the promotion path)
- Modify: `server/app/main.py:684-698` (`_process_audio`: no more `apply_boosts`)
- Test: `server/tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_state.py`:

```python
class TestFullFrameStability:
    @pytest.mark.asyncio
    async def test_challenger_not_on_top_still_accumulates_stability(self, service):
        """Junk can occupy rank 1 with shifting track_ids; the true track
        accumulates 2-of-3 from rank 2 and promotes (needs a clear lead:
        >= 1.5x the best other-album score, since there is no context)."""
        challenger = make_candidate(track_id=7, album_id=2, score=35)
        junk1 = make_candidate(track_id=50, album_id=9, score=21)
        junk2 = make_candidate(track_id=51, album_id=9, score=21)
        await service.feed([junk1, challenger])
        await service.feed([junk2, challenger])
        assert service.get_state().status == "playing"
        assert service.get_state().track_id == 7

    @pytest.mark.asyncio
    async def test_no_context_promote_needs_clear_lead(self, service):
        """Without album context, a barely-stable track does not promote
        while the field is close (anti-spurious guard)."""
        challenger = make_candidate(track_id=7, album_id=2, score=20)
        junk1 = make_candidate(track_id=51, album_id=9, score=18)
        junk2 = make_candidate(track_id=52, album_id=9, score=18)
        await service.feed([junk1, challenger])
        await service.feed([junk2, challenger])
        # 20 < 18 * 1.5 — guard blocks the promote.
        assert service.get_state().status == "listening"


class TestChallengerGuard:
    @pytest.mark.asyncio
    async def test_stable_challenger_dethrones_weak_current(self, service):
        """A wrongly-promoted track sustained by weak scores no longer squats:
        a stable, clearly-stronger cross-album challenger takes over."""
        wrong = make_candidate(track_id=1, album_id=1, score=20)
        await service.feed([wrong])
        await service.feed([wrong])
        assert service.get_state().track_id == 1
        cur_weak = make_candidate(track_id=1, album_id=1, score=6)
        right = make_candidate(track_id=9, album_id=3, score=40)
        await service.feed([cur_weak, right])
        await service.feed([cur_weak, right])
        assert service.get_state().track_id == 9

    @pytest.mark.asyncio
    async def test_cross_album_challenger_blocked_without_margin(self, service):
        """A cross-album rival that does not clearly beat the current track
        (>= 1.5x) cannot steal the now-playing slot."""
        cur = make_candidate(track_id=1, album_id=1, score=20)
        await service.feed([cur])
        await service.feed([cur])
        rival = make_candidate(track_id=9, album_id=3, score=25)
        await service.feed([cur, rival])
        await service.feed([cur, rival])
        # 25 < 20 * 1.5 — current track stays.
        assert service.get_state().track_id == 1

    @pytest.mark.asyncio
    async def test_same_album_neighbor_below_current_does_not_steal(self, service):
        """An album-mate cross-matching at a low score never outranks the
        current track in the stability winner selection."""
        cur = make_candidate(track_id=1, album_id=1, score=50)
        await service.feed([cur])
        await service.feed([cur])
        neighbor = make_candidate(track_id=2, album_id=1, score=7)
        await service.feed([cur, neighbor])
        await service.feed([cur, neighbor])
        assert service.get_state().track_id == 1

    @pytest.mark.asyncio
    async def test_same_album_switch_when_outscoring(self, service):
        """Track transition without layout metadata: the next track rises
        above the fading current one and takes over without a margin."""
        cur = make_candidate(track_id=1, album_id=1, score=20)
        await service.feed([cur])
        await service.feed([cur])
        fading = make_candidate(track_id=1, album_id=1, score=6)
        rising = make_candidate(track_id=2, album_id=1, score=30)
        await service.feed([fading, rising])
        await service.feed([fading, rising])
        assert service.get_state().track_id == 2
```

Replace the whole `TestDonaOlimpiaRegression` class (currently at the bottom of the file, using `apply_boosts`) with:

```python
class TestDonaOlimpiaRegression:
    """A sparse next-track promotes on its very first frame at raw score 5
    (>= MIN_SEQUENTIAL_SCORE) — no boost machinery needed in v2."""

    @pytest.mark.asyncio
    async def test_sparse_expected_next_promotes_on_first_frame(self):
        tracks = [
            {"track_id": 5, "album_id": 126, "side": "B", "position": "B1", "track_number": 5},  # Jazz Carnival
            {"track_id": 6, "album_id": 126, "side": "B", "position": "B2", "track_number": 6},  # Dona Olimpia
        ]
        svc = NowPlayingService(get_tracks_for_album=lambda _aid: tracks)
        try:
            jazz = make_candidate(track_id=5, album_id=126, track_number=5,
                                  side="B", position="B1", score=20)
            await svc.feed([jazz])
            await svc.feed([jazz])
            assert svc.get_state().track_id == 5
            # Drop to listening so the next promote must use the shortcut.
            svc._status = "listening"
            svc._last_played = jazz
            svc._current = None
            svc._buffer.clear()

            dona = make_candidate(track_id=6, album_id=126, track_number=6,
                                  side="B", position="B2", score=5)
            await svc.feed([dona])
            assert svc.get_state().status == "playing"
            assert svc.get_state().track_id == 6
        finally:
            svc.shutdown()
```

In `TestSequentialTrackShortcut.test_shortcut_requires_min_score`, change the weak candidate's score from `5` to `3` (the shortcut bar drops from 10-boosted to raw 4):

```python
    @pytest.mark.asyncio
    async def test_shortcut_requires_min_score(self, sequential_service):
        c1 = make_candidate(track_id=1, track_number=1)
        await sequential_service.feed([c1])
        await sequential_service.feed([c1])

        c2 = make_candidate(track_id=2, track_number=2, score=3)
        await sequential_service.feed([c2])
        assert sequential_service.get_state().track_id == 1
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `cd server && pytest tests/test_state.py -k "FullFrameStability or ChallengerGuard or DonaOlimpia" -v`
Expected: FAIL (e.g. `test_stable_challenger_dethrones_weak_current` keeps track 1 because of the maintain early-return; the Dona test fails because raw 5 < `MIN_PROMOTE_SCORE` 10).

- [ ] **Step 3: Rewrite the state machine core**

In `server/app/state.py`:

**3a.** Replace the constants block (keep `GRACE_MISSES`, idle timeouts, `SILENCE_RMS_DBFS`, `HASH_MIN_COUNT`, `BUFFER_SIZE`, `REQUIRED_MATCHES` as-is):

```python
MIN_PROMOTE_SCORE = 6        # = matcher min_count; one bar for everyone
MIN_SEQUENTIAL_SCORE = 4     # hinted sequential-next shortcut
MIN_MAINTAIN_SCORE = 4
CROSS_ALBUM_MARGIN = 1.5
BUFFER_SIZE = 3
REQUIRED_MATCHES = 2
GRACE_MISSES = 6
IDLE_TIMEOUT_LISTENING_S = 10.0
IDLE_TIMEOUT_PLAYING_S = 120.0
SILENCE_RMS_DBFS = -40.0
HASH_MIN_COUNT = 150
```

Delete `MIN_PROMOTE_SCORE = 10`, `BOOST_ON_ALBUM`, `BOOST_EXPECTED_NEXT`, `SILENCE_FRAMES_FOR_FLIP`. Keep `SILENCE_FRAMES_FOR_RELEASE` and the `_silence_streak` machinery for now (removed in Task 3). Delete the `BoostInfo` dataclass and the `import math` line.

**3b.** In `__init__`, change the buffer type and drop `_pending_candidates`:

```python
        self._buffer: list[dict[int, MatchCandidate]] = []
```

Delete `self._pending_candidates: dict[int, MatchCandidate] = {}` (and every other reference to `_pending_candidates` in the file — `_promote` and `_idle_countdown`).

**3c.** Replace `feed()` entirely:

```python
    async def feed(self, candidates: list[MatchCandidate], recorded_at: float | None = None) -> None:
        self._last_feed_time = time.time()
        self._restart_idle_timer()
        self._check_silence_release()

        old_status = self._status
        old_track_id = self._current.track_id if self._current else None

        if self._status == "idle":
            self._status = "listening"

        frame = {c.track_id: c for c in candidates if c.score >= MIN_PROMOTE_SCORE}
        self._buffer.append(frame)
        if len(self._buffer) > BUFFER_SIZE:
            self._buffer.pop(0)

        self._advance(candidates, recorded_at)
        self._check_track_ended()

        new_status = self._status
        new_track_id = self._current.track_id if self._current else None
        if old_status != new_status or old_track_id != new_track_id:
            await self._notify()
```

**3d.** Replace `_evaluate_stability` with the new promotion engine (delete `_top_candidate` too — it is no longer used):

```python
    def _advance(self, candidates: list[MatchCandidate], recorded_at: float | None) -> None:
        """One decision pass per frame: keep / promote / count a miss."""
        cur_match = (
            self._find_candidate(candidates, self._current.track_id)
            if self._current is not None else None
        )
        current_alive = (
            self._status == "playing"
            and cur_match is not None
            and cur_match.score >= MIN_MAINTAIN_SCORE
        )
        if current_alive:
            self._miss_count = 0

        winner = self._stable_winner()
        if winner is not None:
            if self._current is not None and winner.track_id == self._current.track_id:
                self._miss_count = 0
                return
            if self._passes_challenger_guard(winner):
                self._promote(winner, recorded_at)
                return

        if current_alive:
            return

        # Sequential shortcut: only when the current track is absent/weak,
        # so a strong current frame can't be stolen by a single cross-match.
        top = candidates[0] if candidates else None
        if (
            top is not None
            and top.score >= MIN_SEQUENTIAL_SCORE
            and self._is_sequential_track(top)
        ):
            self._promote(top, recorded_at)
            return

        if self._status == "playing":
            self._miss_count += 1
            if self._miss_count >= GRACE_MISSES:
                self._drop_current()

    def _stable_winner(self) -> MatchCandidate | None:
        """Highest-scoring track that appears in >= REQUIRED_MATCHES of the
        buffered frames. Counts every candidate per frame, not just rank 1."""
        counts: dict[int, int] = {}
        latest: dict[int, MatchCandidate] = {}
        for frame in self._buffer:  # oldest -> newest
            for tid, cand in frame.items():
                counts[tid] = counts.get(tid, 0) + 1
                latest[tid] = cand
        stable = [latest[tid] for tid, n in counts.items() if n >= REQUIRED_MATCHES]
        if not stable:
            return None
        return max(stable, key=lambda c: c.score)

    def _recent_best_score(self, track_id: int) -> int:
        best = 0
        for frame in self._buffer:
            cand = frame.get(track_id)
            if cand is not None and cand.score > best:
                best = cand.score
        return best

    def _passes_challenger_guard(self, winner: MatchCandidate) -> bool:
        """Stickiness as a score preference: any challenger must outscore the
        current track's recent best; cross-album or no-context challengers
        additionally need a CROSS_ALBUM_MARGIN lead over the field."""
        cur_best = (
            self._recent_best_score(self._current.track_id)
            if self._current is not None else 0
        )
        if winner.score <= cur_best:
            return False
        ref = self._current or self._last_played
        ctx_album = ref.album_id if ref is not None else None
        if ctx_album is not None and winner.album_id == ctx_album:
            return True
        # Cross-album or no context: must clearly beat the field.
        runner_up = 0
        for frame in self._buffer:
            for cand in frame.values():
                if cand.album_id != winner.album_id and cand.score > runner_up:
                    runner_up = cand.score
        if runner_up and winner.score < runner_up * CROSS_ALBUM_MARGIN:
            return False
        if cur_best and winner.score < cur_best * CROSS_ALBUM_MARGIN:
            return False
        return True

    def _drop_current(self) -> None:
        self._last_played = self._current
        self._status = "listening"
        self._current = None
        self._anchor_time = None
        self._anchor_offset = None
        self._miss_count = 0
```

**3e.** Simplify `_promote` (keep the lock bookkeeping lines for now — they go away in Task 4):

```python
    def _promote(self, candidate: MatchCandidate, recorded_at: float | None = None) -> None:
        if candidate.album_id != self._locked_album_id:
            self._locked_album_id = candidate.album_id
            self._session_played = {candidate.track_id}
        else:
            self._session_played.add(candidate.track_id)
        self._current = candidate
        self._anchor_time = time.time()
        offset = candidate.offset_s or 0.0
        if recorded_at is not None:
            # Compensate for pipeline delay: the audio was captured at
            # recorded_at, so it's (now - recorded_at) seconds old.
            offset += time.time() - recorded_at
        self._anchor_offset = offset
        self._status = "playing"
        self._miss_count = 0
        self._buffer.clear()
```

**3f.** Add the bonus-track exclusion to `_is_sequential_track` (candidate must have a side):

```python
    def _is_sequential_track(self, candidate: MatchCandidate) -> bool:
        """Check if candidate is the next track on the same album.
        Uses _current if playing, or _last_played in a between-tracks gap.
        effective_track_number is album-wide, so this crosses side
        boundaries naturally (B1 follows the last track of side A)."""
        ref = self._current or self._last_played
        if ref is None:
            return False
        if candidate.album_id != ref.album_id:
            return False
        layout = self._album_layout(ref.album_id)
        ref_entry = layout.by_track_id.get(ref.track_id)
        cand_entry = layout.by_track_id.get(candidate.track_id)
        if ref_entry is None or cand_entry is None:
            return False
        if cand_entry.side is None:
            return False  # bonus tracks aren't on the vinyl sequence
        return cand_entry.effective_track_number == ref_entry.effective_track_number + 1
```

**3g.** Replace `expected_next_track_ids` (sequential only, no lock dependency, no side-flip) and delete `_is_expected_next`, `_side_flip_targets`, `_is_last_track_of_side`, and `apply_boosts`:

```python
    def expected_next_track_ids(self) -> set[int]:
        """Track IDs the matcher should hint as the expected next track —
        the etn+1 track(s) of the context track. Album-wide numbering means
        this crosses side boundaries (covers the side-flip case for
        in-order play)."""
        ref = self._current or self._last_played
        if ref is None:
            return set()
        layout = self._album_layout(ref.album_id)
        ref_entry = layout.by_track_id.get(ref.track_id)
        if ref_entry is None:
            return set()
        target = ref_entry.effective_track_number + 1
        return {
            entry.track_id
            for entry in layout.by_track_id.values()
            if entry.side is not None and entry.effective_track_number == target
        }
```

**3h.** In `_idle_countdown`, delete the `self._pending_candidates.clear()` line (field no longer exists). Leave the rest as-is for now.

- [ ] **Step 4: Update the listen handler**

In `server/app/main.py` `_process_audio`, replace:

```python
        raw_candidates = [MatchCandidate(**r) for r in raw_results]
        candidates, boost_infos = now_playing.apply_boosts(raw_candidates)
        now_playing.note_signal()
        elapsed_ms = (time.time() - start) * 1000
        if candidates:
            top = candidates[0]
            top_info = boost_infos[0]
            logger.info(
                "Listen: %s - %s (score:%s raw:%s boost:x%.2f, conf:%s, %.0fms)",
                top.artist, top.track, top.score, top_info.raw_score,
                top_info.boost, top.confidence, elapsed_ms,
            )
```

with:

```python
        candidates = [MatchCandidate(**r) for r in raw_results]
        now_playing.note_signal()
        elapsed_ms = (time.time() - start) * 1000
        if candidates:
            top = candidates[0]
            logger.info(
                "Listen: %s - %s (score:%s, conf:%s, %.0fms)",
                top.artist, top.track, top.score, top.confidence, elapsed_ms,
            )
```

- [ ] **Step 5: Delete obsolete boost/side-flip tests**

In `server/tests/test_state.py`, delete entirely:
- `class TestApplyBoosts` (all of it — note the expected-next tests `test_expected_next_track_ids_default`, `test_expected_next_track_ids_empty_when_unlocked`, and `test_expected_next_excludes_bonus_track_with_no_side` live at the bottom of this class; they are re-added below in v2 form)
- `class TestSideFlipExpectedNext`

Re-add the three expected-next tests that still apply, as a new class (note: no `_locked_album_id`, no `_is_expected_next`):

```python
class TestExpectedNextTrackIds:
    def _layout_svc(self, tracks):
        return NowPlayingService(get_tracks_for_album=lambda _aid: tracks)

    def test_expected_next_track_ids_default(self):
        tracks = [
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
        ]
        svc = self._layout_svc(tracks)
        try:
            svc._last_played = make_candidate(track_id=1, album_id=10, track_number=1)
            assert svc.expected_next_track_ids() == {2}
        finally:
            svc.shutdown()

    def test_expected_next_track_ids_empty_without_context(self):
        svc = self._layout_svc([])
        try:
            assert svc.expected_next_track_ids() == set()
        finally:
            svc.shutdown()

    def test_expected_next_excludes_bonus_track_with_no_side(self):
        """A side-less bonus track at etn ref+1 is not 'next' — it isn't on
        the vinyl sequence."""
        tracks = [
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 2, "album_id": 10, "side": None, "position": None, "track_number": 2},
        ]
        svc = self._layout_svc(tracks)
        try:
            svc._last_played = make_candidate(track_id=1, album_id=10, track_number=1,
                                              side="A", position="A1")
            assert svc.expected_next_track_ids() == set()
        finally:
            svc.shutdown()
```

Also in `TestAlbumLock.test_lock_change_resets_session_played` and `test_same_album_promote_adds_to_session_played`, delete the `service._pending_candidates.clear()` lines (field gone; the classes themselves are removed in Task 4). Same for `TestCrossAlbumRelease.test_lock_moves_when_off_album_wins_stability`.

- [ ] **Step 6: Run the new tests, then the full state suite**

Run: `cd server && pytest tests/test_state.py -k "FullFrameStability or ChallengerGuard or DonaOlimpia or Sequential or ExpectedNext" -v`
Expected: PASS.

Run: `cd server && pytest tests/test_state.py -v`
Expected: PASS except possibly silence/lock tests — those must still pass at this point (the silence and lock machinery is untouched until Tasks 3–4). Investigate any failure.

- [ ] **Step 7: Run the full suite**

Run: `cd server && pytest tests/`
Expected: PASS (the api listen tests exercise the new handler path).

- [ ] **Step 8: Commit**

```bash
git add server/app/state.py server/app/main.py server/tests/test_state.py
git commit -m "Promote on raw scores with full-frame stability

- drop score boosting: one promote bar at raw >= 6 (= min_count) for every candidate, raw >= 4 for the hinted sequential-next shortcut
- the stability buffer now holds every credible candidate per frame, so a new record accumulates 2-of-3 even when stale junk holds rank 1
- replace the maintain early-return with a challenger guard: a stable challenger that outscores the current track takes over
  (x1.5 margin for cross-album or no-context promotes), so a wrongly-promoted track can no longer squat for its full duration
- bonus (side-less) tracks are excluded from the sequential shortcut"
```

---

### Task 3: Evidence streak and context expiry

Replace the silence-streak release with the unified no-evidence streak: silent frames AND frames where the context album shows nothing at raw ≥ 6 count toward expiry; hint-injected junk does not reset it.

**Files:**
- Modify: `server/app/state.py`
- Modify: `server/app/main.py:659-698` (remove `note_silence`/`note_signal` calls)
- Test: `server/tests/test_state.py`, `server/tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_state.py`, replace `class TestSilenceStreak` and `class TestLockRelease.test_lock_released_after_sustained_silence` with:

```python
def _drop_to_listening(svc, last_candidate):
    """Force the between-tracks state: context = last_played."""
    svc._status = "listening"
    svc._last_played = last_candidate
    svc._current = None
    svc._buffer.clear()


class TestNoEvidenceExpiry:
    @pytest.mark.asyncio
    async def test_hinted_junk_below_min_count_does_not_reset_streak(self, service):
        """Hint-injected candidates can appear on 1-3 junk votes; they must
        not keep a stale context alive (adversarial-review finding #1)."""
        played = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([played])
        await service.feed([played])
        _drop_to_listening(service, played)
        junk = make_candidate(track_id=2, album_id=10, score=3)
        for _ in range(14):
            await service.feed([junk])
        assert service._last_played is not None
        await service.feed([junk])  # 15th consecutive no-evidence frame
        assert service._last_played is None

    @pytest.mark.asyncio
    async def test_on_album_evidence_resets_streak(self, service):
        played = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([played])
        await service.feed([played])
        _drop_to_listening(service, played)
        for _ in range(10):
            await service.feed([])
        assert service._no_evidence_streak == 10
        real = make_candidate(track_id=3, album_id=10, score=6)
        await service.feed([real])
        assert service._no_evidence_streak == 0
        assert service._last_played is not None

    @pytest.mark.asyncio
    async def test_silent_frames_count_toward_expiry(self, service):
        played = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([played])
        await service.feed([played])
        _drop_to_listening(service, played)
        for _ in range(15):
            await service.feed([])
        assert service._last_played is None

    @pytest.mark.asyncio
    async def test_streak_stays_zero_without_context(self, service):
        for _ in range(5):
            await service.feed([])
        assert service._no_evidence_streak == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd server && pytest tests/test_state.py -k NoEvidenceExpiry -v`
Expected: FAIL with `AttributeError: ... has no attribute '_no_evidence_streak'`.

- [ ] **Step 3: Implement the evidence streak**

In `server/app/state.py`:

**3a.** Constants — delete `SILENCE_FRAMES_FOR_RELEASE = 20`, add:

```python
MIN_EVIDENCE_SCORE = 6           # context-album raw score that counts as evidence
NO_EVIDENCE_FRAMES_FOR_RELEASE = 15   # ~45s at one frame per ~3s
```

**3b.** In `__init__`, replace `self._silence_streak: int = 0` with `self._no_evidence_streak: int = 0`.

**3c.** Delete `note_silence()`, `note_signal()`, and `_check_silence_release()`. Add:

```python
    def _update_evidence(self, candidates: list[MatchCandidate]) -> None:
        """Count frames with no credible sign of the context album; expire
        the context after NO_EVIDENCE_FRAMES_FOR_RELEASE. Hint-injected
        junk below MIN_EVIDENCE_SCORE does not count as evidence."""
        ref = self._current or self._last_played
        if ref is None:
            self._no_evidence_streak = 0
            return
        if any(c.album_id == ref.album_id and c.score >= MIN_EVIDENCE_SCORE
               for c in candidates):
            self._no_evidence_streak = 0
            return
        self._no_evidence_streak += 1
        if (
            self._no_evidence_streak >= NO_EVIDENCE_FRAMES_FOR_RELEASE
            and self._current is None
        ):
            self._last_played = None
            self._no_evidence_streak = 0
```

**3d.** In `feed()`, replace the `self._check_silence_release()` call with `self._update_evidence(candidates)` (keep it in the same position, before the frame is buffered).

**3e.** In `_promote`, add `self._no_evidence_streak = 0` (a promote is the strongest evidence).

**3f.** In `_idle_countdown`, replace `self._silence_streak = 0` with `self._no_evidence_streak = 0`.

- [ ] **Step 4: Update the listen handler**

In `server/app/main.py` `_process_audio`, the two silence-gate branches become plain feeds (delete the `note_silence()` lines):

```python
        rms_dbfs = await asyncio.to_thread(compute_rms_dbfs, audio_bytes)
        if rms_dbfs < SILENCE_RMS_DBFS:
            await now_playing.feed([], recorded_at=recorded_at)
            logger.info("Listen: silence (rms=%.1f dBFS)", rms_dbfs)
            return
        query_hashes = await asyncio.to_thread(fingerprint_audio, audio_bytes)
        if len(query_hashes) < HASH_MIN_COUNT:
            await now_playing.feed([], recorded_at=recorded_at)
            logger.info("Listen: low hash density (hashes=%d)", len(query_hashes))
            return
```

Also delete the `now_playing.note_signal()` line added in Task 2's handler snippet.

- [ ] **Step 5: Update the api silence-gate tests**

In `server/tests/test_api.py`, the silence-gate tests assert `_silence_streak` increments. The streak only counts when context exists now, so seed a context and assert on `_no_evidence_streak`. In each of the two gate tests (`test_listen_silent_audio_skips_fingerprint` and `test_listen_low_hash_density_discards_candidates` — exact names may differ; find them by their `_silence_streak` references), apply this pattern:

```python
    from app.models import MatchCandidate
    svc = app_main.now_playing
    svc._current = None
    svc._status = "listening"
    svc._last_played = MatchCandidate(
        track_id=1, artist="A", album="Al", album_id=10, track="T1",
        track_number=1, year=2020, side="A", position="A1", score=20,
        confidence=2.0, offset_s=0.0, duration_s=180.0,
        discogs_url=None, cover_url=None,
    )
    streak_before = svc._no_evidence_streak
    # ... existing POST /listen ...
    assert svc._no_evidence_streak > streak_before
```

In `TestLockRelease.test_idle_countdown_clears_lock` (in `test_state.py`), replace the `assert service._silence_streak == 0` line with `assert service._no_evidence_streak == 0` (the lock asserts are removed in Task 4).

- [ ] **Step 6: Run the full suite**

Run: `cd server && pytest tests/`
Expected: PASS. A `grep -rn "silence_streak\|note_silence\|note_signal\|SILENCE_FRAMES" server/app/` must come back empty.

- [ ] **Step 7: Commit**

```bash
git add server/app/state.py server/app/main.py server/tests/test_state.py server/tests/test_api.py
git commit -m "Expire album context after 15 frames without evidence

- replace the silence-streak release with a unified no-evidence streak: silent frames and frames where the context album shows
  nothing at raw >= 6 both count toward expiry (~45s), so talking or handling noise during a record change no longer resets the clock
- hint-injected junk below min_count does not count as evidence, closing the stale-context loophole from the adversarial review
- drop note_silence()/note_signal(); feed() owns all evidence accounting"
```

---

### Task 4: Remove the explicit lock state

Context is now fully implicit in `(current or last_played)` — delete `_locked_album_id` and `_session_played`.

**Files:**
- Modify: `server/app/state.py`
- Test: `server/tests/test_state.py`, `server/tests/test_api.py`

- [ ] **Step 1: Update the tests first**

In `server/tests/test_state.py`:

1. Delete `class TestAlbumLock` entirely.
2. Replace `class TestCrossAlbumRelease` with:

```python
class TestCrossAlbumTakeover:
    @pytest.mark.asyncio
    async def test_context_follows_cross_album_promote(self, service):
        """A record swap: the new album promotes directly via stability and
        becomes the new context — no explicit release step needed."""
        old = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([old])
        await service.feed([old])
        _drop_to_listening(service, old)
        new = make_candidate(track_id=99, album_id=20, score=40)
        await service.feed([new])
        await service.feed([new])
        assert service.get_state().track_id == 99
        assert service.expected_next_track_ids() == set()  # context = album 20 (no layout)
```

3. In what remains of `TestLockRelease`, keep only the idle test, renamed:

```python
class TestIdleClearsContext:
    @pytest.mark.asyncio
    async def test_idle_countdown_clears_context(self, service):
        await service.feed([make_candidate(track_id=1, album_id=10, score=20)])
        await service.feed([make_candidate(track_id=1, album_id=10, score=20)])
        service._status = "listening"
        await service._idle_countdown(0.01)
        assert service._current is None
        assert service._last_played is None
        assert service._no_evidence_streak == 0
        assert service.get_state().status == "idle"
```

4. In `class TestDeletionHooks`:
   - `test_on_album_deleted_clears_lock_if_locked` → rename to `test_on_album_deleted_drops_layout_cache`; delete the `_locked_album_id`/`_session_played` lines; keep `svc._album_layout(10)` + `svc.on_album_deleted(10)` + `assert 10 not in svc._album_layout_cache`.
   - `test_on_album_deleted_other_album_leaves_lock` → delete (nothing left to assert).
   - `test_on_track_deleted_invalidates_layout_and_removes_from_session` → rename to `test_on_track_deleted_invalidates_layout`; delete the `_session_played` lines; keep the cache-invalidation assert.
   - The three current/last_played-dropping tests stay unchanged.

In `server/tests/test_api.py`:
   - `test_listen_passes_expected_next_hints_to_matcher`: delete the `svc._locked_album_id = 10` line (context now comes from `svc._current`).
   - `test_album_delete_clears_lock` → rename to `test_album_delete_clears_now_playing_context` and replace the lock setup/asserts:

```python
def test_album_delete_clears_now_playing_context(client):
    from app import main as app_main
    from app.models import MatchCandidate
    db = app_main.get_db()
    album_id, _ = db.insert_album(artist="A", name="Al", year=2020)
    track_id = db.insert_track(album_id, "A", "Al", "T1", track_number=1)
    db.insert_hashes([(1, track_id, 0)])

    svc = app_main.now_playing
    cur = MatchCandidate(
        track_id=track_id, artist="A", album="Al", album_id=album_id, track="T1",
        track_number=1, year=2020, side="A", position="A1", score=20,
        confidence=2.0, offset_s=0.0, duration_s=180.0,
        discogs_url=None, cover_url=None,
    )
    svc._current = cur
    svc._last_played = cur
    svc._status = "playing"
    svc._album_layout(album_id)  # populate cache

    resp = client.delete(f"/albums/{album_id}")
    assert resp.status_code in (200, 204)
    assert svc._current is None
    assert svc._last_played is None
    assert svc._status == "listening"
    assert album_id not in svc._album_layout_cache
```

- [ ] **Step 2: Run to verify the updated tests fail**

Run: `cd server && pytest tests/test_state.py -k "CrossAlbumTakeover or IdleClearsContext or DeletionHooks" -v`
Expected: mostly PASS already (behavior exists); any failure here is informative. The point of this task is deletion, so proceed.

- [ ] **Step 3: Delete the lock state**

In `server/app/state.py`:

1. `__init__`: delete `self._locked_album_id: int | None = None` and `self._session_played: set[int] = set()`.
2. `_promote`: delete the lock block, leaving:

```python
    def _promote(self, candidate: MatchCandidate, recorded_at: float | None = None) -> None:
        self._current = candidate
        self._anchor_time = time.time()
        offset = candidate.offset_s or 0.0
        if recorded_at is not None:
            # Compensate for pipeline delay: the audio was captured at
            # recorded_at, so it's (now - recorded_at) seconds old.
            offset += time.time() - recorded_at
        self._anchor_offset = offset
        self._status = "playing"
        self._miss_count = 0
        self._no_evidence_streak = 0
        self._buffer.clear()
```

3. `on_album_deleted`: delete the `if self._locked_album_id == album_id:` block (keep cache clearing and the current/last_played drops).
4. `on_track_deleted`: delete the `self._session_played.discard(track_id)` line.
5. `_idle_countdown`: delete the `self._locked_album_id = None` and `self._session_played = set()` lines.

- [ ] **Step 4: Run the full suite**

Run: `cd server && pytest tests/`
Expected: PASS. `grep -rn "locked_album\|session_played" server/` must come back empty.

- [ ] **Step 5: Commit**

```bash
git add server/app/state.py server/tests/test_state.py server/tests/test_api.py
git commit -m "Make album context implicit in the reference track

- delete _locked_album_id and _session_played: the context album is simply (current or last_played).album_id
- a record swap is now just a cross-album promote; context follows automatically and expires via the no-evidence streak or idle
- deletion hooks and idle cleanup shrink accordingly"
```

---

### Task 5: Final verification and doc sync

**Files:**
- Modify: `docs/track-promotion-flow-v2.md`
- Verify: whole `server/` tree

- [ ] **Step 1: Full suite + leftover scan**

Run: `cd server && pytest tests/`
Expected: ALL PASS.

Run: `grep -rn "apply_boosts\|BoostInfo\|BOOST_\|side_flip\|_is_expected_next\|max_results\|silence_streak\|locked_album\|session_played" server/app/ server/tests/`
Expected: no output. Remove any straggler found.

- [ ] **Step 2: Sanity-check the server boots**

Run: `cd server && python -c "from app.main import app; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Sync the v2 design doc**

In `docs/track-promotion-flow-v2.md`:
1. Add under the title: `> Implemented 2026-06-XX on branch feat/album-lock-and-silence.` (use the actual date).
2. Update the challenger-guard wording (chart node `CH` and the "Review amendments" section) to match the implemented rule: *every* challenger must outscore the current track's recent best; cross-album or no-context challengers additionally need the ×1.5 margin over the field and over the current track. (Implementation detail discovered during planning: without the same-album outscore rule, a low-score album-mate could steal from a strong current track, since stability no longer implies outranking.)

- [ ] **Step 4: Commit**

```bash
git add docs/track-promotion-flow-v2.md
git commit -m "Mark track-promotion v2 design as implemented

- sync the challenger-guard wording with the implemented rule: any challenger must outscore the current track's recent best;
  cross-album or no-context promotes additionally need the x1.5 margin"
```

---

## Post-implementation notes (not part of the tasks)

- Real-world tuning knobs after a few play sessions: `MIN_PROMOTE_SCORE` (6), `MIN_SEQUENTIAL_SCORE` (4), `CROSS_ALBUM_MARGIN` (1.5), `NO_EVIDENCE_FRAMES_FOR_RELEASE` (15). The `Listen:` log line shows raw score and confidence per frame.
- The web UI, Roon, and Last.fm are pure subscribers of `NowPlayingResponse` — no changes needed.
- The Android client is untouched (server-only change).
