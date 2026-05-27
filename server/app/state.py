import asyncio
import math
import re
import time
from dataclasses import dataclass
from typing import AsyncGenerator, Callable

from .models import MatchCandidate, NowPlayingResponse

MIN_PROMOTE_SCORE = 10
MIN_MAINTAIN_SCORE = 4
BUFFER_SIZE = 3
REQUIRED_MATCHES = 2
GRACE_MISSES = 6
IDLE_TIMEOUT_LISTENING_S = 10.0
IDLE_TIMEOUT_PLAYING_S = 120.0
BOOST_ON_ALBUM = 1.5
BOOST_EXPECTED_NEXT = 2.5
SILENCE_FRAMES_FOR_FLIP = 4
SILENCE_FRAMES_FOR_RELEASE = 20


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


@dataclass
class BoostInfo:
    raw_score: int
    boost: float


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


class NowPlayingService:
    def __init__(
        self,
        get_tracks_for_album: Callable[[int], list[dict]] | None = None,
    ):
        self._get_tracks_for_album = get_tracks_for_album or (lambda _album_id: [])
        self._buffer: list[tuple[int, int, int | None, int] | None] = []
        self._pending_candidates: dict[int, MatchCandidate] = {}
        self._current: MatchCandidate | None = None
        self._last_played: MatchCandidate | None = None  # remember last track for sequential detection
        self._anchor_time: float | None = None
        self._anchor_offset: float | None = None
        self._status: str = "idle"
        self._idle_task: asyncio.Task | None = None
        self._condition = asyncio.Condition()
        self._ready_event = asyncio.Event()
        self._last_feed_time: float | None = None
        self._miss_count: int = 0
        self._album_layout_cache: dict[int, AlbumLayout] = {}
        self._silence_streak: int = 0
        self._locked_album_id: int | None = None
        self._session_played: set[int] = set()

    async def notify_ready(self) -> None:
        """Signal that the server is ready, waking any SSE clients waiting for startup."""
        self._ready_event.set()
        await self._notify()

    async def wait_ready(self) -> None:
        """Block until the server is ready."""
        await self._ready_event.wait()

    def shutdown(self):
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()

    async def feed(self, candidates: list[MatchCandidate], recorded_at: float | None = None) -> None:
        self._last_feed_time = time.time()
        self._restart_idle_timer()
        self._check_silence_release()

        old_status = self._status
        old_track_id = self._current.track_id if self._current else None

        if self._status == "idle":
            self._status = "listening"

        # If currently playing, keep the track alive as long as it shows up
        # anywhere in the candidate list at or above the maintain threshold.
        # This absorbs weak frames without flapping into "listening".
        if self._status == "playing" and self._current is not None:
            current_match = self._find_candidate(candidates, self._current.track_id)
            if current_match is not None and current_match.score >= MIN_MAINTAIN_SCORE:
                self._miss_count = 0
                self._check_track_ended()
                new_status = self._status
                new_track_id = self._current.track_id if self._current else None
                if old_status != new_status or old_track_id != new_track_id:
                    await self._notify()
                return

        top = self._top_candidate(candidates)
        entry = None
        if top and top.score >= MIN_PROMOTE_SCORE:
            entry = (top.track_id, top.album_id, top.track_number, top.score)
            self._pending_candidates[top.track_id] = top

        self._buffer.append(entry)
        if len(self._buffer) > BUFFER_SIZE:
            self._buffer.pop(0)

        if (
            top is not None
            and top.score >= MIN_PROMOTE_SCORE
            and self._is_sequential_track(top)
        ):
            self._promote(top, recorded_at)
        else:
            self._evaluate_stability(recorded_at)

        self._check_track_ended()

        new_status = self._status
        new_track_id = self._current.track_id if self._current else None
        if old_status != new_status or old_track_id != new_track_id:
            await self._notify()

    def get_state(self) -> NowPlayingResponse:
        self._check_track_ended()

        if self._status != "playing" or self._current is None:
            return NowPlayingResponse(status=self._status)

        elapsed = None
        if self._anchor_time is not None and self._anchor_offset is not None:
            elapsed = round(self._anchor_offset + (time.time() - self._anchor_time), 1)

        return NowPlayingResponse(
            status="playing",
            track_id=self._current.track_id,
            artist=self._current.artist,
            album=self._current.album,
            album_id=self._current.album_id,
            track=self._current.track,
            track_number=self._current.track_number,
            side=self._current.side,
            position=self._current.position,
            year=self._current.year,
            duration_s=self._current.duration_s,
            cover_url=self._current.cover_url,
            discogs_url=self._current.discogs_url,
            elapsed_s=elapsed,
            started_at=self._anchor_time,
            offset_s=self._anchor_offset,
            score=self._current.score,
            confidence=self._current.confidence,
        )

    async def subscribe(self, timeout: float = 30.0) -> AsyncGenerator[NowPlayingResponse | None, None]:
        while True:
            try:
                async with self._condition:
                    await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                yield self.get_state()
            except asyncio.TimeoutError:
                yield None

    def current_track_id(self) -> int | None:
        """Return the currently playing track_id, or None if not playing.
        Used by the match pipeline to hint the matcher for maintain signals."""
        if self._status == "playing" and self._current is not None:
            return self._current.track_id
        return None

    def note_silence(self) -> None:
        """Called by the listen handler when a chunk was deemed silent
        (RMS gate or hash-density gate). feed() stays silence-agnostic."""
        self._silence_streak += 1

    def note_signal(self) -> None:
        """Called by the listen handler when a chunk passed both silence gates,
        regardless of whether the matcher returned candidates."""
        self._silence_streak = 0

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

    def on_album_deleted(self, album_id: int) -> None:
        self.clear_album_cache(album_id)
        if self._locked_album_id == album_id:
            self._locked_album_id = None
            self._session_played = set()
            self._last_played = None

    def on_track_deleted(self, track_id: int, album_id: int) -> None:
        self.clear_album_cache(album_id)
        self._session_played.discard(track_id)

    def _top_candidate(self, candidates: list[MatchCandidate]) -> MatchCandidate | None:
        return candidates[0] if candidates else None

    def _find_candidate(self, candidates: list[MatchCandidate], track_id: int) -> MatchCandidate | None:
        for c in candidates:
            if c.track_id == track_id:
                return c
        return None

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
        """True if `candidate` is the expected next track on the locked album.
        Covers both the sequential case and the side-flip case."""
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
        # Sequential (bonus/no-side tracks never qualify as next)
        if (
            cand_entry.side is not None
            and cand_entry.effective_track_number == ref_entry.effective_track_number + 1
        ):
            return True
        # Side-flip
        return cand_entry.track_id in self._side_flip_targets(layout, ref_entry)

    def expected_next_track_ids(self) -> set[int]:
        """Track IDs the matcher should hint for the expected-next case.
        Covers both the sequential case and the side-flip case."""
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
            if entry.side is not None
            and entry.effective_track_number == target
        }
        return sequential | self._side_flip_targets(layout, ref_entry)

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
        self._pending_candidates.clear()

    def _evaluate_stability(self, recorded_at: float | None = None) -> None:
        if len(self._buffer) < 2:
            return

        counts: dict[int, int] = {}
        for entry in self._buffer:
            if entry is not None:
                tid = entry[0]
                counts[tid] = counts.get(tid, 0) + 1

        for tid, count in counts.items():
            if count >= REQUIRED_MATCHES:
                if self._current and self._current.track_id == tid:
                    self._miss_count = 0
                    return
                if tid in self._pending_candidates:
                    self._promote(self._pending_candidates[tid], recorded_at)
                return

        # No stable track in buffer
        if self._status == "playing":
            self._miss_count += 1
            if self._miss_count >= GRACE_MISSES:
                self._last_played = self._current
                self._status = "listening"
                self._current = None
                self._anchor_time = None
                self._anchor_offset = None
                self._miss_count = 0

    def _check_silence_release(self) -> None:
        if self._locked_album_id is None:
            return
        if self._silence_streak >= SILENCE_FRAMES_FOR_RELEASE:
            self._locked_album_id = None
            self._session_played = set()
            self._last_played = None

    def _check_track_ended(self) -> None:
        if self._status != "playing" or self._current is None:
            return
        if self._anchor_time is None or self._anchor_offset is None:
            return
        if self._current.duration_s is None:
            return
        elapsed = self._anchor_offset + (time.time() - self._anchor_time)
        if elapsed >= self._current.duration_s:
            self._end_track()

    def _end_track(self) -> None:
        self._last_played = self._current
        self._current = None
        self._anchor_time = None
        self._anchor_offset = None
        if self._last_feed_time and (time.time() - self._last_feed_time) < IDLE_TIMEOUT_PLAYING_S:
            self._status = "listening"
        else:
            self._status = "idle"

    def _restart_idle_timer(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        timeout = IDLE_TIMEOUT_PLAYING_S if self._status == "playing" else IDLE_TIMEOUT_LISTENING_S
        self._idle_task = asyncio.create_task(self._idle_countdown(timeout))

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

    async def _notify(self) -> None:
        async with self._condition:
            self._condition.notify_all()
