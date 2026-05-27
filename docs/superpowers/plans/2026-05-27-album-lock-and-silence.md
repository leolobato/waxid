# Album Lock + Silence Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop spurious "now playing" promotions from silence/surface noise, and reliably promote sparse on-album tracks while playing full albums in order.

**Architecture:** Server-side only. Two silence gates in `/listen` (RMS + hash-density). `NowPlayingService` gains an album lock, a session-played set, an album-layout cache with an `effective_track_number` ordering, and a `apply_boosts` step that re-ranks candidates by ×1 / ×1.5 / ×2.5 based on lock state and expected-next-track. The matcher's hint mechanism becomes a set and survives `max_results` truncation so sparse expected-next tracks reach the boost layer.

**Tech Stack:** Python 3.12, FastAPI, pytest (asyncio mode), SQLite, numpy. No client-side or schema changes.

**Spec:** `docs/superpowers/specs/2026-05-27-album-lock-and-silence-design.md`

**Branch:** `feat/album-lock-and-silence` (already created and active)

---

## File map

| File | Action | Responsibility |
|---|---|---|
| `server/app/matcher.py` | modify | `hint_track_ids` plural, hints survive `max_results` |
| `server/app/state.py` | modify | lock state, layout cache, `effective_track_number`, `apply_boosts`, `note_silence/note_signal`, cleanup hooks |
| `server/app/main.py` | modify | silence gates, hint set wiring, `apply_boosts` call, log update, CRUD cleanup hooks, inject `get_tracks_for_album` |
| `server/tests/test_matcher.py` | modify | new hint-set tests |
| `server/tests/test_state.py` | modify | new lock/boost/silence/layout tests |
| `server/tests/test_api.py` | modify | new listen-handler tests |

Existing `Database.get_tracks_for_album(album_id)` is already present (`server/app/db.py:237`) — no DB changes required.

---

## Phase A — Matcher hint set

### Task A1: Matcher accepts `hint_track_ids: Iterable[int] | None`

**Files:**
- Modify: `server/app/matcher.py:13-26`, `server/app/matcher.py:121`, `server/app/matcher.py:142`, `server/app/matcher.py:154-155`
- Test: `server/tests/test_matcher.py` (add new test)

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_matcher.py`:

```python
def test_hint_track_ids_injects_each_below_threshold_track(monkeypatch, tmp_path):
    """Multiple hinted tracks below CONFIG.min_count are all re-injected."""
    from app.matcher import match_hashes
    from app.db import Database
    from app.config import CONFIG

    db = Database(str(tmp_path / "fp.db"))
    try:
        album_id = db.insert_album(artist="A", name="Al", year=2020)
        t1 = db.insert_track(album_id, "A", "Al", "T1", track_number=1)
        t2 = db.insert_track(album_id, "A", "Al", "T2", track_number=2)
        t3 = db.insert_track(album_id, "A", "Al", "T3", track_number=3)
        # Insert just a handful of hashes per track (well below min_count).
        for tid in (t1, t2, t3):
            for f in range(3):
                db.insert_hashes(tid, [(1000 + f, f)])

        # Query with the same hashes; without hints, none would clear min_count.
        query = [(1000 + f, f) for f in range(3)]

        no_hint = match_hashes(query, db, stoplist=None, hint_track_ids=None)
        assert no_hint == []

        with_hints = match_hashes(query, db, stoplist=None, hint_track_ids=[t1, t2, t3])
        returned_ids = {r["track_id"] for r in with_hints}
        assert {t1, t2, t3}.issubset(returned_ids), with_hints
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd server && pytest tests/test_matcher.py::test_hint_track_ids_injects_each_below_threshold_track -v
```

Expected: FAIL — either `TypeError: unexpected keyword argument 'hint_track_ids'` or the assertion on returned IDs.

- [ ] **Step 3: Update `match_hashes` signature and re-injection loop**

Replace `server/app/matcher.py:13-26` (function signature + docstring) with:

```python
def match_hashes(
    query_hashes: list[tuple[int, int]], db: Database,
    stoplist: set[int] | None = None,
    hint_track_ids: Iterable[int] | None = None,
) -> list[dict]:
    """Match query hashes against the database using offset voting.
    Args:
        query_hashes: list of (hash_value, query_frame_time)
        db: Database instance
        stoplist: set of hash values to ignore (too common to be discriminative)
        hint_track_ids: track IDs that should always be considered, even if
            their score doesn't clear min_count. Used by the listen loop to
            keep the currently-playing track AND the expected-next track(s)
            visible to the state machine on weak frames.
    Returns:
        List of match results sorted by score descending.
    """
```

At the top of `server/app/matcher.py`, add `Iterable` to the typing import (or import it):

```python
from typing import Iterable
```

Replace `server/app/matcher.py:121` (the `hint_entry` declaration) with:

```python
    hint_entries: dict[int, tuple[int, int]] = {}  # tid -> (score, offset) for each hinted track
    hint_set: set[int] = set(hint_track_ids) if hint_track_ids else set()
```

Replace `server/app/matcher.py:142` (the `if tid == hint_track_id:` block) with:

```python
        if tid in hint_set:
            hint_entries[tid] = (int(windowed[best_idx_unfiltered]), int(s_offsets[best_idx_unfiltered]))
```

Replace `server/app/matcher.py:154-155` (the single-hint injection) with:

```python
    # Inject any hinted track that got votes but didn't clear min_count.
    for hint_tid, hint_entry in hint_entries.items():
        if hint_tid not in track_best:
            track_best[hint_tid] = hint_entry
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd server && pytest tests/test_matcher.py::test_hint_track_ids_injects_each_below_threshold_track -v
```

Expected: PASS.

- [ ] **Step 5: Run the full matcher test file to confirm no regressions**

```bash
cd server && pytest tests/test_matcher.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add server/app/matcher.py server/tests/test_matcher.py
git commit -m "$(cat <<'EOF'
Accept a set of hint track ids in the matcher

- Replace `hint_track_id` with `hint_track_ids: Iterable[int] | None`
- Re-inject every below-min_count hinted track that received votes
EOF
)"
```

---

### Task A2: Hinted tracks survive `max_results` truncation

**Files:**
- Modify: `server/app/matcher.py:170-180` (the sorted_tracks slice + result-building loop)
- Test: `server/tests/test_matcher.py` (add new test)

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_matcher.py`:

```python
def test_hint_track_ids_survive_max_results_truncation(tmp_path):
    """A hinted track that didn't make the top max_results slice is still returned."""
    from app.matcher import match_hashes
    from app.db import Database
    from app.config import CONFIG

    db = Database(str(tmp_path / "fp.db"))
    try:
        album_id = db.insert_album(artist="A", name="Al", year=2020)
        # Create CONFIG.max_results + 1 tracks. Give the first max_results
        # strong hashes so they fill the top slice; the last track gets a
        # single hash (below min_count) and is the one we hint.
        strong_tracks: list[int] = []
        for i in range(CONFIG.max_results):
            tid = db.insert_track(album_id, "A", "Al", f"S{i}", track_number=i)
            strong_tracks.append(tid)
            for f in range(CONFIG.min_count + 2):
                db.insert_hashes(tid, [(2000 + f, f)])
        sparse_tid = db.insert_track(album_id, "A", "Al", "Sparse", track_number=CONFIG.max_results)
        db.insert_hashes(sparse_tid, [(9999, 0)])

        query = [(2000 + f, f) for f in range(CONFIG.min_count + 2)] + [(9999, 0)]

        results = match_hashes(query, db, stoplist=None, hint_track_ids=[sparse_tid])
        returned_ids = [r["track_id"] for r in results]
        assert sparse_tid in returned_ids, returned_ids
        # The strong tracks should still be present (the hint appends, doesn't replace).
        assert len([t for t in strong_tracks if t in returned_ids]) >= 1
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd server && pytest tests/test_matcher.py::test_hint_track_ids_survive_max_results_truncation -v
```

Expected: FAIL — `sparse_tid` is not in `returned_ids` because the top-`max_results` slice excluded it.

- [ ] **Step 3: Make hinted tracks survive truncation**

Replace `server/app/matcher.py:173-176` (the `for i, (track_id, ...) in enumerate(sorted_tracks[:CONFIG.max_results])` line) with:

```python
    top_slice = sorted_tracks[:CONFIG.max_results]
    top_ids = {tid for tid, _ in top_slice}
    surviving_hints = [
        (tid, entry) for tid, entry in sorted_tracks[CONFIG.max_results:]
        if tid in hint_entries and tid not in top_ids
    ]
    final_tracks = top_slice + surviving_hints

    results = []
    for i, (track_id, (score, offset_frames)) in enumerate(final_tracks):
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd server && pytest tests/test_matcher.py::test_hint_track_ids_survive_max_results_truncation -v
```

Expected: PASS.

- [ ] **Step 5: Run the full matcher test file**

```bash
cd server && pytest tests/test_matcher.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add server/app/matcher.py server/tests/test_matcher.py
git commit -m "$(cat <<'EOF'
Keep hinted tracks past the max_results slice

- Append hinted tracks that fell out of the top slice
- Preserves the sparse-track signal for downstream boosting
EOF
)"
```

---

### Task A3: Update the single existing caller of `hint_track_id`

**Files:**
- Modify: `server/app/main.py:660-661`

- [ ] **Step 1: Update the caller**

Replace `server/app/main.py:660-661` (the lines `hint_track_id = now_playing.current_track_id()` and the `match_hashes` call) with:

```python
        hint_track_ids: set[int] = set()
        cur = now_playing.current_track_id()
        if cur is not None:
            hint_track_ids.add(cur)
        # expected_next_track_ids() arrives in Task C3; until then the only
        # hint is the currently-playing track, identical to today's behaviour.
        results = await asyncio.to_thread(match_hashes, query_hashes, get_db(), _stoplist, hint_track_ids)
```

- [ ] **Step 2: Run the full server test suite**

```bash
cd server && pytest -v
```

Expected: all tests pass (no behavioural change yet).

- [ ] **Step 3: Commit**

```bash
git add server/app/main.py
git commit -m "$(cat <<'EOF'
Pass current track id via the matcher hint set

- Switch the listen handler to the new hint_track_ids signature
- No behavioural change yet; expected-next tracks join in Phase C
EOF
)"
```

---

## Phase B — NowPlayingService scaffolding

### Task B1: Constructor takes `get_tracks_for_album` callable

**Files:**
- Modify: `server/app/state.py:16-29` (class header + `__init__`)
- Modify: `server/app/main.py:127`
- Test: `server/tests/test_state.py` (no new test — refactor; existing tests must pass with default callable)

- [ ] **Step 1: Update `NowPlayingService.__init__`**

Replace `server/app/state.py:16-29` (the `class NowPlayingService:` block down through the end of `__init__`) with:

```python
class NowPlayingService:
    def __init__(
        self,
        get_tracks_for_album: Callable[[int], list[dict]] | None = None,
    ):
        self._get_tracks_for_album = get_tracks_for_album or (lambda _album_id: [])
        self._buffer: list[tuple[int, int, int | None, int] | None] = []
        self._pending_candidates: dict[int, MatchCandidate] = {}
        self._current: MatchCandidate | None = None
        self._last_played: MatchCandidate | None = None
        self._anchor_time: float | None = None
        self._anchor_offset: float | None = None
        self._status: str = "idle"
        self._idle_task: asyncio.Task | None = None
        self._condition = asyncio.Condition()
        self._ready_event = asyncio.Event()
        self._last_feed_time: float | None = None
        self._miss_count: int = 0
```

At the top of `server/app/state.py`, add `Callable` to the typing import:

```python
from typing import AsyncGenerator, Callable
```

- [ ] **Step 2: Wire the callable in `main.py`**

Replace `server/app/main.py:127` (`now_playing = NowPlayingService()`) with:

```python
now_playing = NowPlayingService(
    get_tracks_for_album=lambda album_id: get_db().get_tracks_for_album(album_id),
)
```

- [ ] **Step 3: Run the full server test suite**

```bash
cd server && pytest -v
```

Expected: all tests pass — the default-callable preserves the no-arg behaviour the existing tests rely on.

- [ ] **Step 4: Commit**

```bash
git add server/app/state.py server/app/main.py
git commit -m "$(cat <<'EOF'
Inject the album-track lookup into NowPlayingService

- Constructor accepts a `get_tracks_for_album` callable, default no-op
- Wires the real DB lookup in main.py without importing get_db from state.py
EOF
)"
```

---

### Task B2: Album layout cache with `effective_track_number`

**Files:**
- Modify: `server/app/state.py` — add dataclasses, `_album_layout_cache`, `_album_layout()`, `clear_album_cache()`, `on_album_deleted()`, `on_track_deleted()`
- Test: `server/tests/test_state.py` (add layout tests)

- [ ] **Step 1: Write the failing tests**

Append a new test class to `server/tests/test_state.py`:

```python
class TestAlbumLayout:
    def _layout_svc(self, tracks):
        """NowPlayingService wired to return the given tracks for album_id=10."""
        def fake_get(album_id):
            assert album_id == 10
            return tracks
        return NowPlayingService(get_tracks_for_album=fake_get)

    def test_effective_track_number_orders_by_side_and_position(self):
        tracks = [
            {"track_id": 100, "album_id": 10, "side": "B", "position": "B2", "track_number": 6},
            {"track_id": 101, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 102, "album_id": 10, "side": "B", "position": "B1", "track_number": 5},
            {"track_id": 103, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
        ]
        svc = self._layout_svc(tracks)
        try:
            layout = svc._album_layout(10)
            ordered = sorted(layout.by_track_id.values(),
                             key=lambda t: t.effective_track_number)
            assert [t.track_id for t in ordered] == [101, 103, 102, 100]
            assert [t.effective_track_number for t in ordered] == [1, 2, 3, 4]
        finally:
            svc.shutdown()

    def test_effective_track_number_falls_back_to_position(self):
        # All track_number are None; ordering must still match positions.
        tracks = [
            {"track_id": 200, "album_id": 10, "side": "B", "position": "B1", "track_number": None},
            {"track_id": 201, "album_id": 10, "side": "B", "position": "B2", "track_number": None},
            {"track_id": 202, "album_id": 10, "side": "A", "position": "A1", "track_number": None},
        ]
        svc = self._layout_svc(tracks)
        try:
            layout = svc._album_layout(10)
            ordered = sorted(layout.by_track_id.values(),
                             key=lambda t: t.effective_track_number)
            assert [t.track_id for t in ordered] == [202, 200, 201]
        finally:
            svc.shutdown()

    def test_effective_track_number_mixed_metadata_orders_by_position(self):
        """Spec §3a regression: B1 with track_number=5 must precede B2 with only position."""
        tracks = [
            {"track_id": 300, "album_id": 10, "side": "B", "position": "B2", "track_number": None},
            {"track_id": 301, "album_id": 10, "side": "B", "position": "B1", "track_number": 5},
        ]
        svc = self._layout_svc(tracks)
        try:
            layout = svc._album_layout(10)
            ordered = sorted(layout.by_track_id.values(),
                             key=lambda t: t.effective_track_number)
            assert [t.track_id for t in ordered] == [301, 300]
        finally:
            svc.shutdown()

    def test_clear_album_cache_drops_entry(self):
        tracks = [{"track_id": 400, "album_id": 10, "side": "A", "position": "A1", "track_number": 1}]
        svc = self._layout_svc(tracks)
        try:
            svc._album_layout(10)
            assert 10 in svc._album_layout_cache
            svc.clear_album_cache(10)
            assert 10 not in svc._album_layout_cache
        finally:
            svc.shutdown()

    def test_clear_album_cache_none_clears_all(self):
        tracks = [{"track_id": 400, "album_id": 10, "side": "A", "position": "A1", "track_number": 1}]
        svc = self._layout_svc(tracks)
        try:
            svc._album_layout(10)
            svc.clear_album_cache(None)
            assert svc._album_layout_cache == {}
        finally:
            svc.shutdown()
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
cd server && pytest tests/test_state.py::TestAlbumLayout -v
```

Expected: FAIL — `_album_layout`, `_album_layout_cache`, `clear_album_cache` do not exist.

- [ ] **Step 3: Add the dataclasses, cache, and lookup**

Add near the top of `server/app/state.py`, after the constants block (around line 14):

```python
import re
from dataclasses import dataclass


@dataclass
class AlbumTrackEntry:
    track_id: int
    album_id: int
    side: str | None
    position: str | None
    track_number: int | None
    effective_track_number: int


@dataclass
class AlbumLayout:
    by_track_id: dict[int, AlbumTrackEntry]
    sides: dict[str | None, list[AlbumTrackEntry]]


_POSITION_NUMBER_RE = re.compile(r"(\d+)\s*$")


def _parse_position_number(position: str | None) -> int | None:
    if not position:
        return None
    m = _POSITION_NUMBER_RE.search(position)
    return int(m.group(1)) if m else None


def _secondary_sort_key(track: dict) -> int:
    pos_num = _parse_position_number(track.get("position"))
    if pos_num is not None:
        return pos_num
    tn = track.get("track_number")
    return int(tn) if tn is not None else 0
```

Inside `NowPlayingService.__init__`, after the existing fields, add:

```python
        self._album_layout_cache: dict[int, AlbumLayout] = {}
```

Add new methods to `NowPlayingService` (place them right before `_top_candidate` at line 141):

```python
    def _album_layout(self, album_id: int) -> AlbumLayout:
        cached = self._album_layout_cache.get(album_id)
        if cached is not None:
            return cached

        rows = self._get_tracks_for_album(album_id)

        def sort_key(track: dict):
            side = track.get("side")
            # None side sorts last via a sentinel.
            side_key = (1, "") if side is None else (0, side)
            return (side_key, _secondary_sort_key(track), int(track["track_id"]))

        ordered = sorted(rows, key=sort_key)

        by_track_id: dict[int, AlbumTrackEntry] = {}
        sides: dict[str | None, list[AlbumTrackEntry]] = {}
        for i, row in enumerate(ordered, start=1):
            entry = AlbumTrackEntry(
                track_id=int(row["track_id"]),
                album_id=int(row["album_id"]),
                side=row.get("side"),
                position=row.get("position"),
                track_number=row.get("track_number"),
                effective_track_number=i,
            )
            by_track_id[entry.track_id] = entry
            sides.setdefault(entry.side, []).append(entry)

        layout = AlbumLayout(by_track_id=by_track_id, sides=sides)
        self._album_layout_cache[album_id] = layout
        return layout

    def clear_album_cache(self, album_id: int | None = None) -> None:
        if album_id is None:
            self._album_layout_cache.clear()
        else:
            self._album_layout_cache.pop(album_id, None)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd server && pytest tests/test_state.py::TestAlbumLayout -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite for regressions**

```bash
cd server && pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add server/app/state.py server/tests/test_state.py
git commit -m "$(cat <<'EOF'
Cache per-album track layout with effective ordering

- Derive effective_track_number from (side, parsed position, track_number, track_id)
- Position is preferred over track_number because it is side-local and monotonic
- Cache is keyed by album_id and invalidated explicitly via clear_album_cache
EOF
)"
```

---

### Task B3: Update `_is_sequential_track` to use `effective_track_number`

**Files:**
- Modify: `server/app/state.py:150-161` (`_is_sequential_track`)
- Test: `server/tests/test_state.py` (add the missing-track_number regression test)

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_state.py`:

```python
class TestSequentialWithEffectiveOrder:
    def _svc_for_album(self, tracks):
        return NowPlayingService(get_tracks_for_album=lambda _aid: tracks)

    def test_sequential_promote_works_with_missing_track_number(self):
        """The Dona Olimpia regression: both ref and candidate lack track_number."""
        tracks = [
            {"track_id": 1, "album_id": 10, "side": "B", "position": "B1", "track_number": None},
            {"track_id": 2, "album_id": 10, "side": "B", "position": "B2", "track_number": None},
        ]
        svc = self._svc_for_album(tracks)
        try:
            ref = make_candidate(track_id=1, album_id=10, track_number=None)
            cand = make_candidate(track_id=2, album_id=10, track_number=None, score=10)
            # Simulate ref as last_played.
            svc._last_played = ref
            assert svc._is_sequential_track(cand) is True
        finally:
            svc.shutdown()

    def test_sequential_promote_still_works_when_track_number_present(self):
        tracks = [
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
        ]
        svc = self._svc_for_album(tracks)
        try:
            ref = make_candidate(track_id=1, album_id=10, track_number=1)
            cand = make_candidate(track_id=2, album_id=10, track_number=2, score=10)
            svc._last_played = ref
            assert svc._is_sequential_track(cand) is True
        finally:
            svc.shutdown()

    def test_sequential_returns_false_across_albums(self):
        tracks_a = [
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
        ]
        # Note: layout cache for album 10 only; album 20 returns empty.
        svc = NowPlayingService(get_tracks_for_album=lambda aid: tracks_a if aid == 10 else [])
        try:
            ref = make_candidate(track_id=1, album_id=10, track_number=1)
            cand = make_candidate(track_id=99, album_id=20, track_number=1, score=10)
            svc._last_played = ref
            assert svc._is_sequential_track(cand) is False
        finally:
            svc.shutdown()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd server && pytest tests/test_state.py::TestSequentialWithEffectiveOrder -v
```

Expected: FAIL on `test_sequential_promote_works_with_missing_track_number` (current `_is_sequential_track` returns False when track_number is None).

- [ ] **Step 3: Update `_is_sequential_track`**

Replace `server/app/state.py:150-161` (the whole method) with:

```python
    def _is_sequential_track(self, candidate: MatchCandidate) -> bool:
        """Check if candidate is the next track on the same album.
        Uses _current if playing, or _last_played if we're in a between-tracks gap.
        Falls back to effective_track_number derived from position when raw
        track_number is missing on either side."""
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
        return cand_entry.effective_track_number == ref_entry.effective_track_number + 1
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd server && pytest tests/test_state.py::TestSequentialWithEffectiveOrder -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite**

```bash
cd server && pytest -v
```

Expected: all tests pass. Existing sequential tests still work because when track_number is set and position is consistent, the effective ordering matches.

- [ ] **Step 6: Commit**

```bash
git add server/app/state.py server/tests/test_state.py
git commit -m "$(cat <<'EOF'
Use effective_track_number for sequential promotion

- Sequential check consults the album layout cache so it survives
  tracks with missing track_number, falling back to position order
- Direct fix for the Dona Olimpia metadata-gap failure
EOF
)"
```

---

## Phase C — Lock, silence, boosting

### Task C1: Silence streak with `note_silence` / `note_signal`

**Files:**
- Modify: `server/app/state.py` — add `_silence_streak`, `note_silence()`, `note_signal()`
- Test: `server/tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_state.py`:

```python
class TestSilenceStreak:
    def test_note_silence_increments_streak(self, service):
        assert service._silence_streak == 0
        service.note_silence()
        service.note_silence()
        assert service._silence_streak == 2

    def test_note_signal_resets_streak(self, service):
        service.note_silence()
        service.note_silence()
        service.note_signal()
        assert service._silence_streak == 0

    def test_feed_is_silence_agnostic(self, service):
        """feed() does not touch _silence_streak — that's the handler's job."""
        service.note_silence()
        service.note_silence()
        asyncio.get_event_loop().run_until_complete(service.feed([]))
        assert service._silence_streak == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd server && pytest tests/test_state.py::TestSilenceStreak -v
```

Expected: FAIL — `_silence_streak`, `note_silence`, `note_signal` don't exist.

- [ ] **Step 3: Add the fields and methods**

Inside `NowPlayingService.__init__`, after the other fields, add:

```python
        self._silence_streak: int = 0
```

Add these methods to `NowPlayingService` (place them near `current_track_id` at line 134):

```python
    def note_silence(self) -> None:
        """Called by the listen handler when a chunk was deemed silent
        (RMS gate or hash-density gate). feed() stays silence-agnostic."""
        self._silence_streak += 1

    def note_signal(self) -> None:
        """Called by the listen handler when a chunk passed both silence gates,
        regardless of whether the matcher returned candidates."""
        self._silence_streak = 0
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd server && pytest tests/test_state.py::TestSilenceStreak -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/app/state.py server/tests/test_state.py
git commit -m "$(cat <<'EOF'
Track silence streak explicitly outside feed()

- Add note_silence() / note_signal() so the listen handler controls
  streak transitions and feed() stays silence-agnostic
- Closes the "unmatched music inherits silence streak" hole
EOF
)"
```

---

### Task C2: Album lock state in `_promote`

**Files:**
- Modify: `server/app/state.py:163-175` (`_promote`)
- Test: `server/tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_state.py`:

```python
class TestAlbumLock:
    def test_lock_set_on_first_promote(self, service):
        cand = make_candidate(track_id=1, album_id=10, score=20)
        asyncio.get_event_loop().run_until_complete(service.feed([cand]))
        assert service._locked_album_id == 10
        assert service._session_played == {1}

    def test_lock_change_resets_session_played(self, service):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(service.feed([make_candidate(track_id=1, album_id=10, score=20)]))
        assert service._session_played == {1}
        # Force a promotion from a different album by clearing the current
        # state and feeding a strong cross-album candidate.
        service._current = None
        service._last_played = None
        service._buffer.clear()
        service._pending_candidates.clear()
        loop.run_until_complete(service.feed([make_candidate(track_id=99, album_id=20, score=20)]))
        assert service._locked_album_id == 20
        assert service._session_played == {99}

    def test_same_album_promote_adds_to_session_played(self, service):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(service.feed([make_candidate(track_id=1, album_id=10, score=20)]))
        # Promote a different track from the same album via a fresh stability run.
        service._current = None
        service._last_played = None
        service._buffer.clear()
        service._pending_candidates.clear()
        loop.run_until_complete(service.feed([make_candidate(track_id=2, album_id=10, score=20)]))
        loop.run_until_complete(service.feed([make_candidate(track_id=2, album_id=10, score=20)]))
        assert service._locked_album_id == 10
        assert service._session_played == {1, 2}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd server && pytest tests/test_state.py::TestAlbumLock -v
```

Expected: FAIL — `_locked_album_id`, `_session_played` don't exist.

- [ ] **Step 3: Add lock fields and update `_promote`**

Inside `NowPlayingService.__init__`, add:

```python
        self._locked_album_id: int | None = None
        self._session_played: set[int] = set()
```

Replace `server/app/state.py:163-175` (`_promote`) with:

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
            offset += time.time() - recorded_at
        self._anchor_offset = offset
        self._status = "playing"
        self._miss_count = 0
        self._buffer.clear()
        self._pending_candidates.clear()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd server && pytest tests/test_state.py::TestAlbumLock -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite**

```bash
cd server && pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add server/app/state.py server/tests/test_state.py
git commit -m "$(cat <<'EOF'
Set album lock and session_played on promotion

- _promote() establishes or updates the album lock
- Promotions on a new album reset session_played to {new_track}
- Same-album promotions add to the existing set
EOF
)"
```

---

### Task C3: `apply_boosts` and `expected_next_track_ids` — sequential case

**Files:**
- Modify: `server/app/state.py` — add boost constants, `BoostInfo` dataclass, `apply_boosts`, `_is_expected_next`, `expected_next_track_ids`
- Test: `server/tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_state.py`:

```python
class TestApplyBoosts:
    def _layout_svc(self, tracks):
        return NowPlayingService(get_tracks_for_album=lambda _aid: tracks)

    def test_unlocked_returns_scores_unchanged(self):
        svc = self._layout_svc([])
        try:
            c = make_candidate(track_id=1, album_id=10, score=7)
            boosted, infos = svc.apply_boosts([c])
            assert boosted[0].score == 7
            assert infos[0].raw_score == 7
            assert infos[0].boost == 1.0
        finally:
            svc.shutdown()

    def test_on_album_candidate_gets_1_5(self):
        tracks = [{"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1}]
        svc = self._layout_svc(tracks)
        try:
            svc._locked_album_id = 10
            c = make_candidate(track_id=1, album_id=10, score=7)
            boosted, infos = svc.apply_boosts([c])
            import math
            assert boosted[0].score == math.ceil(7 * 1.5)
            assert infos[0].raw_score == 7
            assert infos[0].boost == 1.5
        finally:
            svc.shutdown()

    def test_off_album_when_locked_unchanged(self):
        tracks = [{"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1}]
        svc = self._layout_svc(tracks)
        try:
            svc._locked_album_id = 10
            c = make_candidate(track_id=99, album_id=20, score=7)
            boosted, infos = svc.apply_boosts([c])
            assert boosted[0].score == 7
            assert infos[0].boost == 1.0
        finally:
            svc.shutdown()

    def test_expected_next_sequential_gets_2_5(self):
        tracks = [
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
        ]
        svc = self._layout_svc(tracks)
        try:
            svc._locked_album_id = 10
            svc._last_played = make_candidate(track_id=1, album_id=10, track_number=1)
            c = make_candidate(track_id=2, album_id=10, track_number=2, score=5)
            boosted, infos = svc.apply_boosts([c])
            import math
            assert boosted[0].score == math.ceil(5 * 2.5)
            assert infos[0].boost == 2.5
        finally:
            svc.shutdown()

    def test_boosted_score_is_int(self):
        tracks = [{"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1}]
        svc = self._layout_svc(tracks)
        try:
            svc._locked_album_id = 10
            c = make_candidate(track_id=1, album_id=10, score=3)
            boosted, _ = svc.apply_boosts([c])
            # ceil(3 * 1.5) = 5, must be int (not 4.5 or 4)
            assert boosted[0].score == 5
            assert isinstance(boosted[0].score, int)
        finally:
            svc.shutdown()

    def test_apply_boosts_resorts_by_boosted_score(self):
        tracks = [
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
        ]
        svc = self._layout_svc(tracks)
        try:
            svc._locked_album_id = 10
            svc._last_played = make_candidate(track_id=1, album_id=10, track_number=1)
            off = make_candidate(track_id=99, album_id=20, score=8)
            nxt = make_candidate(track_id=2, album_id=10, track_number=2, score=5)
            boosted, _ = svc.apply_boosts([off, nxt])
            # expected-next: 5 * 2.5 = 13 beats off-album 8
            assert boosted[0].track_id == 2
            assert boosted[1].track_id == 99
        finally:
            svc.shutdown()

    def test_expected_next_track_ids_default(self):
        tracks = [
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
        ]
        svc = self._layout_svc(tracks)
        try:
            svc._locked_album_id = 10
            svc._last_played = make_candidate(track_id=1, album_id=10, track_number=1)
            assert svc.expected_next_track_ids() == {2}
        finally:
            svc.shutdown()

    def test_expected_next_track_ids_empty_when_unlocked(self):
        svc = self._layout_svc([])
        try:
            assert svc.expected_next_track_ids() == set()
        finally:
            svc.shutdown()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd server && pytest tests/test_state.py::TestApplyBoosts -v
```

Expected: FAIL — `apply_boosts` and `expected_next_track_ids` don't exist.

- [ ] **Step 3: Add the constants, dataclass, and methods**

Add to the constants block at the top of `server/app/state.py` (after `IDLE_TIMEOUT_PLAYING_S`):

```python
BOOST_ON_ALBUM = 1.5
BOOST_EXPECTED_NEXT = 2.5
SILENCE_FRAMES_FOR_FLIP = 4
SILENCE_FRAMES_FOR_RELEASE = 20
```

Add `import math` near the top of the file.

Add the `BoostInfo` dataclass next to the other dataclasses:

```python
@dataclass
class BoostInfo:
    raw_score: int
    boost: float
```

Add these methods to `NowPlayingService` (near `_is_sequential_track`):

```python
    def _is_expected_next(self, candidate: MatchCandidate) -> bool:
        """True if `candidate` is the expected next track on the locked album.
        Sequential case only — side-flip case lands in Task C4."""
        if self._locked_album_id is None:
            return False
        if candidate.album_id != self._locked_album_id:
            return False
        ref = self._current or self._last_played
        if ref is None or ref.album_id != self._locked_album_id:
            return False
        layout = self._album_layout(self._locked_album_id)
        ref_entry = layout.by_track_id.get(ref.track_id)
        cand_entry = layout.by_track_id.get(candidate.track_id)
        if ref_entry is None or cand_entry is None:
            return False
        return cand_entry.effective_track_number == ref_entry.effective_track_number + 1

    def expected_next_track_ids(self) -> set[int]:
        """Track IDs the matcher should hint for the expected-next case.
        Sequential case only — side-flip case lands in Task C4."""
        if self._locked_album_id is None:
            return set()
        ref = self._current or self._last_played
        if ref is None or ref.album_id != self._locked_album_id:
            return set()
        layout = self._album_layout(self._locked_album_id)
        ref_entry = layout.by_track_id.get(ref.track_id)
        if ref_entry is None:
            return set()
        target = ref_entry.effective_track_number + 1
        return {
            entry.track_id
            for entry in layout.by_track_id.values()
            if entry.effective_track_number == target
        }

    def apply_boosts(
        self, candidates: list[MatchCandidate]
    ) -> tuple[list[MatchCandidate], list[BoostInfo]]:
        """Re-rank candidates by lock-aware boosts.

        Returns (boosted_candidates, boost_infos) sorted by boosted score desc.
        boost_infos is index-aligned with boosted_candidates and carries the
        raw score plus the boost factor for logging.
        """
        annotated: list[tuple[MatchCandidate, int, float]] = []
        for c in candidates:
            if self._locked_album_id is None:
                boost = 1.0
            elif self._is_expected_next(c):
                boost = BOOST_EXPECTED_NEXT
            elif c.album_id == self._locked_album_id:
                boost = BOOST_ON_ALBUM
            else:
                boost = 1.0
            new_score = math.ceil(c.score * boost)
            boosted_c = c.model_copy(update={"score": new_score})
            annotated.append((boosted_c, c.score, boost))

        annotated.sort(key=lambda t: t[0].score, reverse=True)

        boosted = [t[0] for t in annotated]
        infos = [BoostInfo(raw_score=t[1], boost=t[2]) for t in annotated]
        return boosted, infos
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd server && pytest tests/test_state.py::TestApplyBoosts -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/app/state.py server/tests/test_state.py
git commit -m "$(cat <<'EOF'
Boost on-album and expected-next candidates by lock state

- apply_boosts re-ranks with x1.5 on-album and x2.5 for the next track
- Boosted score uses math.ceil to keep the int contract on MatchCandidate
- expected_next_track_ids exposes the same predicate to the matcher hint
EOF
)"
```

---

### Task C4: Side-flip awareness in `_is_expected_next` / `expected_next_track_ids`

**Files:**
- Modify: `server/app/state.py` — extend the two methods
- Test: `server/tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_state.py`:

```python
class TestSideFlipExpectedNext:
    def _two_side_svc(self):
        tracks = [
            {"track_id": 10, "album_id": 50, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 11, "album_id": 50, "side": "A", "position": "A2", "track_number": 2},
            {"track_id": 20, "album_id": 50, "side": "B", "position": "B1", "track_number": 3},
            {"track_id": 21, "album_id": 50, "side": "B", "position": "B2", "track_number": 4},
            {"track_id": 30, "album_id": 50, "side": None, "position": None, "track_number": 5},
        ]
        return NowPlayingService(get_tracks_for_album=lambda _aid: tracks)

    def test_side_flip_boosts_other_side_after_silence(self):
        svc = self._two_side_svc()
        try:
            svc._locked_album_id = 50
            svc._session_played = {20, 21}  # finished side B
            svc._last_played = make_candidate(track_id=21, album_id=50, side="B",
                                              position="B2", track_number=4)
            svc._silence_streak = 5  # >= SILENCE_FRAMES_FOR_FLIP (4)
            # Candidate from side A
            cand_a = make_candidate(track_id=10, album_id=50, side="A",
                                    position="A1", track_number=1, score=5)
            assert svc._is_expected_next(cand_a) is True
            assert svc.expected_next_track_ids() == {10, 11}
        finally:
            svc.shutdown()

    def test_side_flip_requires_silence(self):
        svc = self._two_side_svc()
        try:
            svc._locked_album_id = 50
            svc._session_played = {20, 21}
            svc._last_played = make_candidate(track_id=21, album_id=50, side="B",
                                              position="B2", track_number=4)
            svc._silence_streak = 2  # below SILENCE_FRAMES_FOR_FLIP
            cand_a = make_candidate(track_id=10, album_id=50, side="A",
                                    position="A1", track_number=1, score=5)
            # Not the sequential next (there's no track #5 on side B),
            # and silence hasn't elapsed → expected-next must be False.
            assert svc._is_expected_next(cand_a) is False
            assert svc.expected_next_track_ids() == set()
        finally:
            svc.shutdown()

    def test_side_flip_excludes_already_played(self):
        svc = self._two_side_svc()
        try:
            svc._locked_album_id = 50
            svc._session_played = {20, 21, 10}  # 10 already played
            svc._last_played = make_candidate(track_id=21, album_id=50, side="B",
                                              position="B2", track_number=4)
            svc._silence_streak = 5
            cand_played = make_candidate(track_id=10, album_id=50, side="A",
                                         position="A1", track_number=1, score=5)
            cand_unplayed = make_candidate(track_id=11, album_id=50, side="A",
                                           position="A2", track_number=2, score=5)
            assert svc._is_expected_next(cand_played) is False
            assert svc._is_expected_next(cand_unplayed) is True
            assert svc.expected_next_track_ids() == {11}
        finally:
            svc.shutdown()

    def test_side_flip_excludes_no_side_tracks(self):
        svc = self._two_side_svc()
        try:
            svc._locked_album_id = 50
            svc._session_played = {20, 21}
            svc._last_played = make_candidate(track_id=21, album_id=50, side="B",
                                              position="B2", track_number=4)
            svc._silence_streak = 5
            cand_no_side = make_candidate(track_id=30, album_id=50, side=None,
                                          position=None, track_number=5, score=5)
            assert svc._is_expected_next(cand_no_side) is False
            assert 30 not in svc.expected_next_track_ids()
        finally:
            svc.shutdown()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd server && pytest tests/test_state.py::TestSideFlipExpectedNext -v
```

Expected: FAIL — current implementation only handles sequential.

- [ ] **Step 3: Extend the two methods with side-flip logic**

Replace the existing `_is_expected_next` and `expected_next_track_ids` in `server/app/state.py` with:

```python
    def _is_last_track_of_side(self, layout: AlbumLayout, entry: AlbumTrackEntry) -> bool:
        if entry.side is None:
            return False
        side_tracks = layout.sides.get(entry.side, [])
        if not side_tracks:
            return False
        max_etn = max(t.effective_track_number for t in side_tracks)
        return entry.effective_track_number == max_etn

    def _side_flip_targets(self, layout: AlbumLayout, ref_entry: AlbumTrackEntry) -> set[int]:
        """Unplayed tracks on sides other than ref.side, only when ref is
        the last track of its side AND silence has lasted long enough."""
        if ref_entry.side is None:
            return set()
        if not self._is_last_track_of_side(layout, ref_entry):
            return set()
        if self._silence_streak < SILENCE_FRAMES_FOR_FLIP:
            return set()
        return {
            entry.track_id
            for entry in layout.by_track_id.values()
            if entry.side is not None
            and entry.side != ref_entry.side
            and entry.track_id not in self._session_played
        }

    def _is_expected_next(self, candidate: MatchCandidate) -> bool:
        if self._locked_album_id is None:
            return False
        if candidate.album_id != self._locked_album_id:
            return False
        ref = self._current or self._last_played
        if ref is None or ref.album_id != self._locked_album_id:
            return False
        layout = self._album_layout(self._locked_album_id)
        ref_entry = layout.by_track_id.get(ref.track_id)
        cand_entry = layout.by_track_id.get(candidate.track_id)
        if ref_entry is None or cand_entry is None:
            return False
        # Sequential
        if cand_entry.effective_track_number == ref_entry.effective_track_number + 1:
            return True
        # Side-flip
        return cand_entry.track_id in self._side_flip_targets(layout, ref_entry)

    def expected_next_track_ids(self) -> set[int]:
        if self._locked_album_id is None:
            return set()
        ref = self._current or self._last_played
        if ref is None or ref.album_id != self._locked_album_id:
            return set()
        layout = self._album_layout(self._locked_album_id)
        ref_entry = layout.by_track_id.get(ref.track_id)
        if ref_entry is None:
            return set()
        target = ref_entry.effective_track_number + 1
        sequential = {
            entry.track_id
            for entry in layout.by_track_id.values()
            if entry.effective_track_number == target
        }
        return sequential | self._side_flip_targets(layout, ref_entry)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd server && pytest tests/test_state.py::TestSideFlipExpectedNext tests/test_state.py::TestApplyBoosts -v
```

Expected: PASS (all of them — the sequential cases must still pass).

- [ ] **Step 5: Commit**

```bash
git add server/app/state.py server/tests/test_state.py
git commit -m "$(cat <<'EOF'
Add side-flip awareness to expected-next predicate

- Last track of a side + silence streak >= SILENCE_FRAMES_FOR_FLIP
  promotes every unplayed track on the other side(s) to expected-next
- Bonus tracks with side=None never qualify for the side-flip boost
EOF
)"
```

---

### Task C5: Cross-album release in `_evaluate_stability`

**Files:**
- Modify: `server/app/state.py:177-205` (`_evaluate_stability`)
- Test: `server/tests/test_state.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_state.py`:

```python
class TestCrossAlbumRelease:
    def test_lock_moves_when_off_album_wins_stability(self, service):
        loop = asyncio.get_event_loop()
        # Lock on album 10.
        loop.run_until_complete(service.feed([make_candidate(track_id=1, album_id=10, score=20)]))
        assert service._locked_album_id == 10
        # Drop back to listening so stability buffer starts fresh.
        service._current = None
        service._status = "listening"
        service._buffer.clear()
        service._pending_candidates.clear()
        # Two strong frames for an off-album candidate trigger stability promotion.
        loop.run_until_complete(service.feed([make_candidate(track_id=99, album_id=20, score=20)]))
        loop.run_until_complete(service.feed([make_candidate(track_id=99, album_id=20, score=20)]))
        assert service._locked_album_id == 20
        assert service._session_played == {99}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd server && pytest tests/test_state.py::TestCrossAlbumRelease -v
```

Expected: depending on the existing stability flow, either FAIL or PASS — `_promote()` already does the lock-move from Task C2, so this test mainly verifies the integration. If it already passes, leave it as a regression test and skip to Step 5. If it fails (e.g., promotion doesn't fire), fix `_evaluate_stability`.

- [ ] **Step 3: Confirm `_promote` handles cross-album implicitly**

Re-read `_promote` from Task C2: it resets `_session_played` whenever `candidate.album_id != self._locked_album_id`. So `_evaluate_stability` doesn't need explicit cross-album logic — promotion via the buffer naturally moves the lock. No code change required.

- [ ] **Step 4: Run the full test suite**

```bash
cd server && pytest -v
```

Expected: all tests pass, including `test_lock_moves_when_off_album_wins_stability`.

- [ ] **Step 5: Commit (regression test only)**

```bash
git add server/tests/test_state.py
git commit -m "$(cat <<'EOF'
Add regression test for cross-album lock takeover

- Verifies that an off-album candidate winning the stability buffer
  moves the lock to the new album via the _promote path
EOF
)"
```

---

### Task C6: Lock release on sustained silence and idle countdown

**Files:**
- Modify: `server/app/state.py` — extend `feed()` (or a helper called from feed) with the silence-release check; extend `_idle_countdown` to clear lock state
- Test: `server/tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_state.py`:

```python
class TestLockRelease:
    def test_lock_released_after_sustained_silence(self, service):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(service.feed([make_candidate(track_id=1, album_id=10, score=20)]))
        assert service._locked_album_id == 10
        # Silence streak reaches the release threshold.
        for _ in range(20):
            service.note_silence()
        loop.run_until_complete(service.feed([]))
        assert service._locked_album_id is None
        assert service._session_played == set()
        assert service._last_played is None

    def test_idle_countdown_clears_lock(self, service):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(service.feed([make_candidate(track_id=1, album_id=10, score=20)]))
        assert service._locked_album_id == 10
        # Drive the idle path synchronously.
        service._status = "listening"
        loop.run_until_complete(service._idle_countdown(0.01))
        assert service._locked_album_id is None
        assert service._session_played == set()
        assert service._silence_streak == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd server && pytest tests/test_state.py::TestLockRelease -v
```

Expected: FAIL — lock release isn't implemented yet.

- [ ] **Step 3: Add silence-release in `feed` and lock-clear in `_idle_countdown`**

Add a helper to `NowPlayingService` (place it near `_check_track_ended`):

```python
    def _check_silence_release(self) -> None:
        if self._locked_album_id is None:
            return
        if self._silence_streak >= SILENCE_FRAMES_FOR_RELEASE:
            self._locked_album_id = None
            self._session_played = set()
            self._last_played = None
```

In `feed()` (the existing method around line 44), call the helper right after `self._restart_idle_timer()`:

```python
    async def feed(self, candidates: list[MatchCandidate], recorded_at: float | None = None) -> None:
        self._last_feed_time = time.time()
        self._restart_idle_timer()
        self._check_silence_release()
        # ...rest of existing body unchanged...
```

Replace `server/app/state.py:234-248` (`_idle_countdown`) with:

```python
    async def _idle_countdown(self, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
            old_status = self._status
            self._status = "idle"
            self._current = None
            self._last_played = None
            self._anchor_time = None
            self._anchor_offset = None
            self._buffer.clear()
            self._pending_candidates.clear()
            self._locked_album_id = None
            self._session_played = set()
            self._silence_streak = 0
            if old_status != "idle":
                await self._notify()
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd server && pytest tests/test_state.py::TestLockRelease -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite**

```bash
cd server && pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add server/app/state.py server/tests/test_state.py
git commit -m "$(cat <<'EOF'
Release album lock on sustained silence and on idle

- feed() drops the lock when silence streak hits SILENCE_FRAMES_FOR_RELEASE
- _idle_countdown clears lock, session_played, and silence_streak
EOF
)"
```

---

### Task C7: Cleanup hooks for album / track deletion

**Files:**
- Modify: `server/app/state.py` — add `on_album_deleted`, `on_track_deleted`
- Test: `server/tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_state.py`:

```python
class TestDeletionHooks:
    def _layout_svc(self, tracks):
        return NowPlayingService(get_tracks_for_album=lambda _aid: tracks)

    def test_on_album_deleted_clears_lock_if_locked(self):
        svc = self._layout_svc([
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
        ])
        try:
            svc._locked_album_id = 10
            svc._session_played = {1}
            svc._album_layout(10)
            svc.on_album_deleted(10)
            assert svc._locked_album_id is None
            assert svc._session_played == set()
            assert 10 not in svc._album_layout_cache
        finally:
            svc.shutdown()

    def test_on_album_deleted_other_album_leaves_lock(self):
        svc = self._layout_svc([])
        try:
            svc._locked_album_id = 10
            svc._session_played = {1}
            svc.on_album_deleted(99)
            assert svc._locked_album_id == 10
            assert svc._session_played == {1}
        finally:
            svc.shutdown()

    def test_on_track_deleted_invalidates_layout_and_removes_from_session(self):
        svc = self._layout_svc([
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
        ])
        try:
            svc._album_layout(10)
            svc._session_played = {1, 2}
            svc.on_track_deleted(1, 10)
            assert 10 not in svc._album_layout_cache
            assert svc._session_played == {2}
        finally:
            svc.shutdown()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd server && pytest tests/test_state.py::TestDeletionHooks -v
```

Expected: FAIL — methods don't exist.

- [ ] **Step 3: Add the methods**

Add to `NowPlayingService` (near `clear_album_cache`):

```python
    def on_album_deleted(self, album_id: int) -> None:
        self.clear_album_cache(album_id)
        if self._locked_album_id == album_id:
            self._locked_album_id = None
            self._session_played = set()
            self._last_played = None

    def on_track_deleted(self, track_id: int, album_id: int) -> None:
        self.clear_album_cache(album_id)
        self._session_played.discard(track_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd server && pytest tests/test_state.py::TestDeletionHooks -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/app/state.py server/tests/test_state.py
git commit -m "$(cat <<'EOF'
Clear lock and layout on album/track deletion

- on_album_deleted drops the locked album from cache and session
- on_track_deleted invalidates the layout and prunes session_played
EOF
)"
```

---

## Phase D — Listen handler wiring

### Task D1: RMS gate

**Files:**
- Modify: `server/app/main.py` — the listen handler (around line 655-672)
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Identify the silence threshold constants and the listen handler**

Read the current listen handler at `server/app/main.py:650-680` to understand the body. The handler decodes audio to PCM (via `fingerprint_audio` internally, but PCM decoding might happen before). For Step 3 you'll need to know where the decoded PCM is available before fingerprinting.

If PCM decoding happens inside `fingerprint_audio`, factor RMS computation to operate on the raw `audio_bytes` by decoding via the same helper `fingerprint_audio` uses (likely `numpy.frombuffer` after a WAV header parse — check `server/app/fingerprint.py`). If a `decode_wav` helper already exists, import it. Otherwise add a small helper in `server/app/fingerprint.py`:

```python
def compute_rms_dbfs(audio_bytes: bytes) -> float:
    """Return the RMS energy of a WAV blob in dBFS (0 dBFS = full-scale int16)."""
    import wave
    import io
    import math
    with wave.open(io.BytesIO(audio_bytes), "rb") as w:
        sample_width = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if not frames:
        return -math.inf
    dtype = np.int16 if sample_width == 2 else np.int8
    samples = np.frombuffer(frames, dtype=dtype).astype(np.float64)
    if samples.size == 0:
        return -math.inf
    rms = math.sqrt(float(np.mean(samples ** 2)))
    if rms <= 0:
        return -math.inf
    full_scale = float(np.iinfo(dtype).max)
    return 20.0 * math.log10(rms / full_scale)
```

(Adjust imports at top of `fingerprint.py`: `import wave`, `import io`, `import math`, and ensure `numpy as np` is already imported.)

- [ ] **Step 2: Write the failing test**

Append to `server/tests/test_api.py`:

```python
def test_listen_silent_audio_skips_fingerprint(client, monkeypatch):
    """A near-silent WAV must skip fingerprinting and increment the silence streak."""
    import wave, io, numpy as np
    from app import main as app_main

    # Build a 3-second 11025 Hz mono int16 WAV of near silence (RMS ~ -80 dBFS).
    sr = 11025
    samples = (np.random.randn(sr * 3) * 4).astype(np.int16)  # tiny noise
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())

    calls = {"fingerprint": 0}
    orig_fp = app_main.fingerprint_audio
    def spy_fp(*a, **kw):
        calls["fingerprint"] += 1
        return orig_fp(*a, **kw)
    monkeypatch.setattr(app_main, "fingerprint_audio", spy_fp)

    streak_before = app_main.now_playing._silence_streak
    resp = client.post("/listen", content=buf.getvalue(),
                       headers={"Content-Type": "audio/wav"})
    assert resp.status_code == 202
    # Give the background task a moment to run.
    import time as _t; _t.sleep(0.2)
    assert calls["fingerprint"] == 0
    assert app_main.now_playing._silence_streak > streak_before
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd server && pytest tests/test_api.py::test_listen_silent_audio_skips_fingerprint -v
```

Expected: FAIL — fingerprint is still called on silent audio.

- [ ] **Step 4: Add the constant and the RMS gate**

In `server/app/state.py`, add to the constants block (if not added in Task C3):

```python
SILENCE_RMS_DBFS = -40.0
```

In the listen handler in `server/app/main.py` (the function containing line 655), import the helper and add the gate before the existing `fingerprint_audio` call. Replace the lines that look like:

```python
        logger.debug("Listen: processing %d bytes", len(audio_bytes))
        start = time.time()
        query_hashes = await asyncio.to_thread(fingerprint_audio, audio_bytes)
```

with:

```python
        logger.debug("Listen: processing %d bytes", len(audio_bytes))
        start = time.time()
        rms_dbfs = await asyncio.to_thread(compute_rms_dbfs, audio_bytes)
        if rms_dbfs < SILENCE_RMS_DBFS:
            now_playing.note_silence()
            await now_playing.feed([], recorded_at=recorded_at)
            logger.info("Listen: silence (rms=%.1f dBFS)", rms_dbfs)
            return
        query_hashes = await asyncio.to_thread(fingerprint_audio, audio_bytes)
```

Add the imports at the top of `server/app/main.py`:

```python
from .fingerprint import fingerprint_audio, compute_rms_dbfs
from .state import SILENCE_RMS_DBFS
```

(Adjust to merge with existing imports.)

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd server && pytest tests/test_api.py::test_listen_silent_audio_skips_fingerprint -v
```

Expected: PASS.

- [ ] **Step 6: Run the full test suite**

```bash
cd server && pytest -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add server/app/fingerprint.py server/app/main.py server/app/state.py server/tests/test_api.py
git commit -m "$(cat <<'EOF'
Gate /listen on RMS energy before fingerprinting

- compute_rms_dbfs decodes the WAV and returns dBFS relative to full-scale
- Sub-threshold chunks skip fingerprinting and increment the silence streak
EOF
)"
```

---

### Task D2: Hash-density gate

**Files:**
- Modify: `server/app/main.py` — the listen handler
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_api.py`:

```python
def test_listen_low_hash_density_discards_candidates(client, monkeypatch):
    """If fingerprint_audio returns very few hashes, the matcher is not called."""
    import wave, io, numpy as np
    from app import main as app_main

    sr = 11025
    samples = (np.random.randn(sr * 3) * 5000).astype(np.int16)  # loud enough
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(samples.tobytes())

    # Force the fingerprinter to return very few hashes.
    monkeypatch.setattr(app_main, "fingerprint_audio", lambda *_a, **_kw: [(1, 0), (2, 1)])
    matcher_calls = {"n": 0}
    orig_match = app_main.match_hashes
    def spy_match(*a, **kw):
        matcher_calls["n"] += 1
        return orig_match(*a, **kw)
    monkeypatch.setattr(app_main, "match_hashes", spy_match)

    streak_before = app_main.now_playing._silence_streak
    resp = client.post("/listen", content=buf.getvalue(),
                       headers={"Content-Type": "audio/wav"})
    assert resp.status_code == 202
    import time as _t; _t.sleep(0.2)
    assert matcher_calls["n"] == 0
    assert app_main.now_playing._silence_streak > streak_before
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd server && pytest tests/test_api.py::test_listen_low_hash_density_discards_candidates -v
```

Expected: FAIL — matcher is still called.

- [ ] **Step 3: Add the gate**

In `server/app/state.py`, ensure the constant exists (add it if not):

```python
HASH_MIN_COUNT = 150
```

Add the import in `server/app/main.py`:

```python
from .state import SILENCE_RMS_DBFS, HASH_MIN_COUNT
```

After the `fingerprint_audio` call inside the listen handler, before the existing matcher call, insert:

```python
        if len(query_hashes) < HASH_MIN_COUNT:
            now_playing.note_silence()
            await now_playing.feed([], recorded_at=recorded_at)
            logger.info("Listen: low hash density (hashes=%d)", len(query_hashes))
            return
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd server && pytest tests/test_api.py::test_listen_low_hash_density_discards_candidates -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite**

```bash
cd server && pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add server/app/main.py server/app/state.py server/tests/test_api.py
git commit -m "$(cat <<'EOF'
Gate /listen on hash density after fingerprinting

- Short fingerprints (vinyl crackle, runout) skip the matcher and feed []
- Bumps the silence streak so the album lock can release
EOF
)"
```

---

### Task D3: Wire `apply_boosts` + expected-next hints + `note_signal`

**Files:**
- Modify: `server/app/main.py` — listen handler
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_api.py`:

```python
def test_listen_passes_expected_next_hints_to_matcher(client, monkeypatch):
    """When a track is playing on a locked album, the matcher receives both
    the current track id and the expected-next track id as hints."""
    import wave, io, numpy as np
    from app import main as app_main
    from app.models import MatchCandidate

    sr = 11025
    samples = (np.random.randn(sr * 3) * 5000).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(samples.tobytes())

    # Swap in a fake album-tracks source so we don't depend on the test DB.
    fake_tracks = [
        {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
        {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
    ]
    svc = app_main.now_playing
    svc.clear_album_cache(None)
    svc._get_tracks_for_album = lambda _aid: fake_tracks

    svc._locked_album_id = 10
    svc._current = MatchCandidate(
        track_id=1, artist="A", album="Al", album_id=10, track="T1",
        track_number=1, year=2020, side="A", position="A1", score=20,
        confidence=2.0, offset_s=0.0, duration_s=180.0,
        discogs_url=None, cover_url=None,
    )
    svc._status = "playing"

    monkeypatch.setattr(app_main, "fingerprint_audio", lambda *_a, **_kw: [(1, 0)] * 1000)

    captured: dict = {}
    def spy_match(query, db, stoplist, hint_track_ids):
        captured["hint_track_ids"] = set(hint_track_ids or [])
        return []
    monkeypatch.setattr(app_main, "match_hashes", spy_match)

    resp = client.post("/listen", content=buf.getvalue(),
                       headers={"Content-Type": "audio/wav"})
    assert resp.status_code == 202
    import time as _t; _t.sleep(0.2)
    assert {1, 2}.issubset(captured.get("hint_track_ids", set())), captured
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd server && pytest tests/test_api.py::test_listen_passes_expected_next_hints_to_matcher -v
```

Expected: FAIL — the listen handler currently only adds the current track.

- [ ] **Step 3: Wire hints, boosts, and note_signal**

Replace the section of the listen handler that builds hints and calls the matcher (the lines added in Task A3) with:

```python
        hint_track_ids: set[int] = set()
        cur = now_playing.current_track_id()
        if cur is not None:
            hint_track_ids.add(cur)
        hint_track_ids.update(now_playing.expected_next_track_ids())
        raw_results = await asyncio.to_thread(
            match_hashes, query_hashes, get_db(), _stoplist, hint_track_ids
        )
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
        else:
            logger.info("Listen: no match (%.0fms)", elapsed_ms)
        await now_playing.feed(candidates, recorded_at=recorded_at)
        state = now_playing.get_state()
        logger.debug("Listen: status=%s%s", state.status,
                     f", track_id={state.track_id}" if state.track_id else "")
```

Remove the old lines that this replaces (the prior `candidates = [...]`, `if candidates: ... logger.info(...)` block, and the existing `await now_playing.feed(...)` call) so the handler ends up with the structure above. Keep the surrounding try/except untouched.

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd server && pytest tests/test_api.py::test_listen_passes_expected_next_hints_to_matcher -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite**

```bash
cd server && pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add server/app/main.py server/tests/test_api.py
git commit -m "$(cat <<'EOF'
Apply lock-aware boosts and expected-next hints in /listen

- Hint set extends to expected_next_track_ids so sparse next tracks
  reach the boost layer
- apply_boosts re-ranks candidates by lock state before feed()
- note_signal resets the silence streak whenever audio passes both gates
- Listen log includes raw score and boost factor for tuning
EOF
)"
```

---

### Task D4: Cleanup hooks on album/track CRUD and Discogs

**Files:**
- Modify: `server/app/main.py` — album/track PUT/DELETE endpoints and the Discogs metadata application path
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Locate the endpoints**

Find existing endpoints by inspecting `server/app/main.py`:

```bash
cd server && grep -nE '@app\.(put|delete|post).*(albums|tracks|discogs)' app/main.py
```

You should see PUTs and DELETEs for albums and tracks, and a Discogs-apply endpoint. Note the line numbers.

- [ ] **Step 2: Write the failing test**

Append to `server/tests/test_api.py`:

```python
def test_album_delete_clears_lock(client, db):
    from app import main as app_main
    # Create an album with one track via the ingest API or directly via db.
    album_id = db.insert_album(artist="A", name="Al", year=2020)
    track_id = db.insert_track(album_id, "A", "Al", "T1", track_number=1)
    db.insert_hashes(track_id, [(1, 0)])

    svc = app_main.now_playing
    svc._locked_album_id = album_id
    svc._session_played = {track_id}
    svc._album_layout(album_id)  # populate cache

    resp = client.delete(f"/albums/{album_id}")
    assert resp.status_code in (200, 204)
    assert svc._locked_album_id is None
    assert svc._session_played == set()
    assert album_id not in svc._album_layout_cache


def test_track_delete_invalidates_layout(client, db):
    from app import main as app_main
    album_id = db.insert_album(artist="A", name="Al", year=2020)
    t1 = db.insert_track(album_id, "A", "Al", "T1", track_number=1)
    t2 = db.insert_track(album_id, "A", "Al", "T2", track_number=2)
    db.insert_hashes(t1, [(1, 0)])
    db.insert_hashes(t2, [(2, 1)])

    svc = app_main.now_playing
    svc._album_layout(album_id)
    svc._session_played = {t1, t2}

    resp = client.delete(f"/tracks/{t1}")
    assert resp.status_code in (200, 204)
    assert album_id not in svc._album_layout_cache
    assert svc._session_played == {t2}
```

(If `client` and `db` fixtures don't already exist in `conftest.py`, mirror whatever the existing API tests use.)

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd server && pytest tests/test_api.py::test_album_delete_clears_lock tests/test_api.py::test_track_delete_invalidates_layout -v
```

Expected: FAIL — endpoints don't call the cleanup hooks.

- [ ] **Step 4: Wire cleanup hooks**

In each of the following endpoints in `server/app/main.py`, add the matching `now_playing` call right after the DB mutation succeeds:

- `DELETE /albums/{album_id}` (line found in Step 1): after `db.delete_album(album_id)`, call `now_playing.on_album_deleted(album_id)`.
- `DELETE /tracks/{track_id}`: after the track is deleted, call `now_playing.on_track_deleted(track_id, album_id)` where `album_id` is fetched from the track row before deletion.
- `PUT /albums/{album_id}` and `PUT /tracks/{track_id}`: after the update succeeds, call `now_playing.clear_album_cache(album_id)`.
- Discogs apply endpoint (any handler that mutates album/track metadata in bulk from Discogs): call `now_playing.clear_album_cache(album_id)` after the apply.

Use the actual function and endpoint shapes found in Step 1 — do not invent new ones. The pattern in each handler is:

```python
    db.delete_album(album_id)
    now_playing.on_album_deleted(album_id)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd server && pytest tests/test_api.py::test_album_delete_clears_lock tests/test_api.py::test_track_delete_invalidates_layout -v
```

Expected: PASS.

- [ ] **Step 6: Run the full test suite**

```bash
cd server && pytest -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add server/app/main.py server/tests/test_api.py
git commit -m "$(cat <<'EOF'
Wire lock and layout cleanup into album/track CRUD

- DELETE /albums and DELETE /tracks fire the lock/layout hooks
- PUT /albums, PUT /tracks, and the Discogs-apply path invalidate the
  layout cache so a renamed or re-ordered album re-derives ordering
EOF
)"
```

---

## Phase E — End-to-end regression

### Task E1: Dona Olimpia regression test

**Files:**
- Test: `server/tests/test_state.py`

- [ ] **Step 1: Write the regression test**

Append to `server/tests/test_state.py`:

```python
class TestDonaOlimpiaRegression:
    """Recreates the failure documented in the spec: a sparse next-track
    on a locked album promoting on its very first frame at raw score 5."""

    def test_sparse_expected_next_promotes_on_first_frame(self):
        loop = asyncio.get_event_loop()
        tracks = [
            {"track_id": 5, "album_id": 126, "side": "B", "position": "B1", "track_number": 5},  # Jazz Carnival
            {"track_id": 6, "album_id": 126, "side": "B", "position": "B2", "track_number": 6},  # Dona Olimpia
        ]
        svc = NowPlayingService(get_tracks_for_album=lambda _aid: tracks)
        try:
            # Establish the lock with Jazz Carnival, then drop to listening
            # so the next promote must use the sequential-with-boost path.
            jazz = make_candidate(track_id=5, album_id=126, track_number=5,
                                  side="B", position="B1", score=20)
            loop.run_until_complete(svc.feed([jazz]))
            assert svc._locked_album_id == 126
            svc._status = "listening"
            svc._last_played = jazz
            svc._current = None
            svc._buffer.clear()
            svc._pending_candidates.clear()

            # Mimic the listen handler: build raw candidates, apply boosts, feed.
            dona_raw = make_candidate(track_id=6, album_id=126, track_number=6,
                                      side="B", position="B2", score=5)
            boosted, _ = svc.apply_boosts([dona_raw])
            # ceil(5 * 2.5) = 13 which is >= MIN_PROMOTE_SCORE
            assert boosted[0].score == 13
            loop.run_until_complete(svc.feed(boosted))
            assert svc.get_state().status == "playing"
            assert svc.get_state().track_id == 6
        finally:
            svc.shutdown()
```

- [ ] **Step 2: Run the test**

```bash
cd server && pytest tests/test_state.py::TestDonaOlimpiaRegression -v
```

Expected: PASS first time — the whole point of this task is verifying the prior phases compose correctly.

- [ ] **Step 3: Run the full test suite one final time**

```bash
cd server && pytest -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add server/tests/test_state.py
git commit -m "$(cat <<'EOF'
Add end-to-end regression for the Dona Olimpia case

- Sparse expected-next track at raw score 5 promotes on first frame
  via the apply_boosts -> ceil(5 * 2.5) = 13 -> sequential path
EOF
)"
```

---

## Out-of-tree validation

After all tasks pass:

- [ ] Run a real listen session locally against an album with sparse tracks (e.g. *Live At The Copacabana Palace*) and confirm via logs that:
  - The `Listen:` lines now show `raw:N boost:xM.MM` where applicable.
  - Silence between sides shows up as `Listen: silence (rms=…)` / `Listen: low hash density (hashes=…)`.
  - Dona Olimpia (or whichever sparse track was missing) reaches `status=playing` and is scrobbled.
- [ ] Open a PR with `git push -u origin feat/album-lock-and-silence` and `gh pr create`.

---

## Self-review notes (already addressed inline)

- Spec §1 silence gates → Tasks D1, D2.
- Spec §2 album lock state → Task C2.
- Spec §3 score boosting + expected-next predicate → Tasks C3, C4.
- Spec §3a album layout + effective_track_number → Task B2.
- Spec §4 matcher hint extension → Tasks A1, A2.
- Spec §5 lock release (silence + cross-album) → Tasks C5, C6.
- Spec §6 state-machine integration → ambient in C2-C6.
- Spec §7 cleanup → Tasks C6 (idle), C7 (deletion), D4 (CRUD/Discogs).
- Spec §8 DB injection → Task B1.
- Spec §9 constants → added across C3, C6, D1, D2.
- All spec test cases covered; the Dona Olimpia end-to-end is Task E1.
