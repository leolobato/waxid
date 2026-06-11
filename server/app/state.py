import asyncio
import re
import time
from dataclasses import dataclass
from typing import AsyncGenerator, Callable

from .models import MatchCandidate, NowPlayingResponse

MIN_PROMOTE_SCORE = 6        # = matcher min_count; one bar for everyone
MIN_SEQUENTIAL_SCORE = 4     # hinted sequential-next shortcut
MIN_MAINTAIN_SCORE = 4
CROSS_ALBUM_MARGIN = 1.5
BUFFER_SIZE = 3
REQUIRED_MATCHES = 2
GRACE_MISSES = 6
IDLE_TIMEOUT_LISTENING_S = 10.0
IDLE_TIMEOUT_PLAYING_S = 120.0
SILENCE_FRAMES_FOR_RELEASE = 20
SILENCE_RMS_DBFS = -40.0
HASH_MIN_COUNT = 150


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


class NowPlayingService:
    def __init__(
        self,
        get_tracks_for_album: Callable[[int], list[dict]] | None = None,
    ):
        self._get_tracks_for_album = get_tracks_for_album or (lambda _album_id: [])
        self._buffer: list[dict[int, MatchCandidate]] = []
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
        if self._current is not None and self._current.album_id == album_id:
            self._current = None
            self._anchor_time = None
            self._anchor_offset = None
            self._status = "listening"
        if self._last_played is not None and self._last_played.album_id == album_id:
            self._last_played = None
        if self._locked_album_id == album_id:
            self._locked_album_id = None
            self._session_played = set()

    def on_track_deleted(self, track_id: int, album_id: int) -> None:
        self.clear_album_cache(album_id)
        self._session_played.discard(track_id)
        if self._current is not None and self._current.track_id == track_id:
            self._current = None
            self._anchor_time = None
            self._anchor_offset = None
            self._status = "listening"
        if self._last_played is not None and self._last_played.track_id == track_id:
            self._last_played = None

    def _find_candidate(self, candidates: list[MatchCandidate], track_id: int) -> MatchCandidate | None:
        for c in candidates:
            if c.track_id == track_id:
                return c
        return None

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
