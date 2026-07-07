import asyncio
import re
import time
from dataclasses import dataclass
from typing import AsyncGenerator, Callable

from .models import FinishedTrack, MatchCandidate, NowPlayingResponse

MIN_PROMOTE_SCORE = 6        # = matcher min_count; one bar for everyone
MIN_SEQUENTIAL_SCORE = 4     # hinted sequential-next shortcut
MIN_MAINTAIN_SCORE = 4
CROSS_ALBUM_MARGIN = 1.5
REANCHOR_THRESHOLD_S = 5.0   # re-sync the playback clock if measured offset drifts past this
BUFFER_SIZE = 3
REQUIRED_MATCHES = 2
GRACE_MISSES = 6
IDLE_TIMEOUT_LISTENING_S = 10.0
IDLE_TIMEOUT_PLAYING_S = 120.0
MIN_EVIDENCE_SCORE = 6           # context-album raw score that counts as evidence
NO_EVIDENCE_FRAMES_FOR_RELEASE = 15   # ~45s at one frame per ~3s
SILENCE_RMS_DBFS = -50.0   # quiet track intros sit ~-45 dBFS; HASH_MIN_COUNT is the real backstop
HASH_MIN_COUNT = 150
COMPLETION_MIN_FRACTION = 0.9   # a track counts as "finished" once it plays this far


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
        self._no_evidence_streak: int = 0
        # Latest completed track, retained and stamped with a monotonic sequence
        # so each subscriber can receive it exactly once (see subscribe()).
        self._finished: FinishedTrack | None = None
        self._finished_seq: int = 0

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
        self._update_evidence(candidates)

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
            return NowPlayingResponse(status=self._status, finished_track=self._finished)

        elapsed = None
        if self._anchor_time is not None and self._anchor_offset is not None:
            elapsed = round(self._anchor_offset + (time.time() - self._anchor_time), 1)

        tracks_on_side, is_last_on_side, sides = self._side_progress(self._current)

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
            tracks_on_side=tracks_on_side,
            is_last_on_side=is_last_on_side,
            sides=sides,
            finished_track=self._finished,
        )

    def _current_elapsed(self) -> float | None:
        if self._anchor_time is None or self._anchor_offset is None:
            return None
        return self._anchor_offset + (time.time() - self._anchor_time)

    def _mark_finished(self, candidate: MatchCandidate | None, elapsed: float | None) -> None:
        """Record a completed track for one-shot emission, but only if it
        played through at least COMPLETION_MIN_FRACTION of its duration. Mid-track
        stops (pause, needle lift, lost detection) fall short and are ignored, so
        no spurious "finished" is reported."""
        if candidate is None or candidate.duration_s is None or elapsed is None:
            return
        if elapsed >= candidate.duration_s * COMPLETION_MIN_FRACTION:
            self._finished = self._build_finished(candidate)
            self._finished_seq += 1

    def _build_finished(self, candidate: MatchCandidate) -> FinishedTrack:
        tracks_on_side, is_last_on_side, sides = self._side_progress(candidate)
        return FinishedTrack(
            track_id=candidate.track_id,
            artist=candidate.artist,
            album=candidate.album,
            album_id=candidate.album_id,
            track=candidate.track,
            track_number=candidate.track_number,
            side=candidate.side,
            position=candidate.position,
            year=candidate.year,
            tracks_on_side=tracks_on_side,
            is_last_on_side=is_last_on_side,
            sides=sides,
        )

    def _side_progress(
        self, current: MatchCandidate
    ) -> tuple[int | None, bool | None, dict[str, int] | None]:
        """Track counts per side for the release, plus how many tracks share
        the current track's side and whether it is the last one on that side.
        Returns (None, None, None) for bonus/digital tracks (side is None) or
        when the album layout is unknown, since the side sequence is undefined.
        Bonus tracks without a side are excluded from the counts."""
        layout = self._album_layout(current.album_id)
        cur_entry = layout.by_track_id.get(current.track_id)
        if cur_entry is None or cur_entry.side is None:
            return None, None, None
        sides: dict[str, int] = {}
        max_etn_by_side: dict[str, int] = {}
        for entry in layout.by_track_id.values():
            if entry.side is None:
                continue
            sides[entry.side] = sides.get(entry.side, 0) + 1
            max_etn_by_side[entry.side] = max(
                max_etn_by_side.get(entry.side, 0), entry.effective_track_number
            )
        is_last = cur_entry.effective_track_number == max_etn_by_side[cur_entry.side]
        return sides[cur_entry.side], is_last, sides

    async def subscribe(self, timeout: float = 30.0) -> AsyncGenerator[NowPlayingResponse | None, None]:
        # Each subscriber tracks the finished-track sequence it has already
        # delivered, so a one-shot completion reaches every subscriber (SSE,
        # Roon, Last.fm) exactly once instead of being consumed by whichever
        # waiter the condition happens to wake first. Start at the current
        # sequence so a subscriber never replays a completion from before it
        # connected.
        seen_finished_seq = self._finished_seq
        while True:
            try:
                async with self._condition:
                    await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                state = self.get_state()
                if self._finished_seq != seen_finished_seq:
                    seen_finished_seq = self._finished_seq
                elif state.finished_track is not None:
                    state = state.model_copy(update={"finished_track": None})
                yield state
            except asyncio.TimeoutError:
                yield None

    def current_track_id(self) -> int | None:
        """Return the currently playing track_id, or None if not playing.
        Used by the match pipeline to hint the matcher for maintain signals."""
        if self._status == "playing" and self._current is not None:
            return self._current.track_id
        return None

    def _update_evidence(self, candidates: list[MatchCandidate]) -> None:
        """Count frames with no credible sign of the context album; expire
        the context after NO_EVIDENCE_FRAMES_FOR_RELEASE. Hint-injected
        junk below MIN_EVIDENCE_SCORE does not count as evidence. Expiry is
        suppressed while a track is playing (_current set); the streak restarts
        when a track drops (_drop_current) or ends (_end_track)."""
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

        layout = AlbumLayout(by_track_id=by_track_id)
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
        self._no_evidence_streak = 0

    def on_track_deleted(self, track_id: int, album_id: int) -> None:
        self.clear_album_cache(album_id)
        if self._current is not None and self._current.track_id == track_id:
            self._current = None
            self._anchor_time = None
            self._anchor_offset = None
            self._status = "listening"
        if self._last_played is not None and self._last_played.track_id == track_id:
            self._last_played = None
        self._no_evidence_streak = 0

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

        # Re-sync the playback clock when the current track confirms this frame.
        # The initial anchor is set once at promotion; without this, needle
        # repositioning within a track and turntable speed drift would leave
        # elapsed_s wrong and could fire track-end detection early.
        if self._status == "playing" and cur_match is not None:
            self._maybe_reanchor(cur_match, recorded_at)

        winner = self._stable_winner()
        if winner is not None:
            if self._current is not None and winner.track_id == self._current.track_id:
                self._miss_count = 0
                return
            if self._passes_challenger_guard(winner, cur_match):
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

    def _passes_challenger_guard(
        self, winner: MatchCandidate, cur_match: MatchCandidate | None = None
    ) -> bool:
        """Stickiness as a score preference: any challenger must outscore the
        current track's recent best; cross-album or no-context challengers
        additionally need a CROSS_ALBUM_MARGIN lead over the field.
        cur_match supplies the current track's live score for frames where it
        sits below MIN_PROMOTE_SCORE (absent from the buffer)."""
        cur_best = (
            self._recent_best_score(self._current.track_id)
            if self._current is not None else 0
        )
        if cur_match is not None:
            cur_best = max(cur_best, cur_match.score)
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
        if not runner_up and not cur_best:
            # Nothing to measure a lead against — require an absolute one,
            # so a lone false positive repeating at the promote bar can't
            # take over during a quiet window.
            return winner.score >= MIN_PROMOTE_SCORE * CROSS_ALBUM_MARGIN
        if runner_up and winner.score < runner_up * CROSS_ALBUM_MARGIN:
            return False
        if cur_best and winner.score < cur_best * CROSS_ALBUM_MARGIN:
            return False
        return True

    def _drop_current(self) -> None:
        # Lost mid-track: only counts as finished if it had already played
        # nearly to the end (e.g. the run-out of the last track on a side).
        self._mark_finished(self._current, self._current_elapsed())
        self._last_played = self._current
        self._status = "listening"
        self._current = None
        self._anchor_time = None
        self._anchor_offset = None
        self._miss_count = 0
        self._buffer.clear()
        self._no_evidence_streak = 0  # the expiry clock measures the gap, so it starts now

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
        # A different track was detected; the outgoing one finished if it had
        # played nearly to its end (guards against early/overlapping matches).
        if self._current is not None and self._current.track_id != candidate.track_id:
            self._mark_finished(self._current, self._current_elapsed())
        self._current = candidate
        self._set_anchor(candidate.offset_s, recorded_at)
        self._status = "playing"
        self._miss_count = 0
        self._no_evidence_streak = 0
        self._buffer.clear()

    def _set_anchor(self, offset_s: float | None, recorded_at: float | None) -> None:
        """Anchor the playback clock to a measured offset. Compensates for
        pipeline delay: the audio was captured at recorded_at, so it is
        (now - recorded_at) seconds old by the time we anchor."""
        now = time.time()
        offset = offset_s or 0.0
        if recorded_at is not None:
            offset += now - recorded_at
        self._anchor_time = now
        self._anchor_offset = offset

    def _maybe_reanchor(self, cur_match: MatchCandidate, recorded_at: float | None) -> None:
        """Re-sync the anchor when a confirming frame's measured offset diverges
        from the predicted elapsed by more than REANCHOR_THRESHOLD_S. Only a
        solidly-voted frame is trusted, so match noise can't yank the clock."""
        if self._anchor_time is None or self._anchor_offset is None:
            return
        if cur_match.score < MIN_PROMOTE_SCORE:
            return
        ref_time = recorded_at if recorded_at is not None else time.time()
        predicted = self._anchor_offset + (ref_time - self._anchor_time)
        if abs(cur_match.offset_s - predicted) > REANCHOR_THRESHOLD_S:
            self._set_anchor(cur_match.offset_s, recorded_at)

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
        # Reached full duration, so always >= the completion threshold.
        self._mark_finished(self._current, self._current_elapsed())
        self._last_played = self._current
        self._current = None
        self._anchor_time = None
        self._anchor_offset = None
        self._buffer.clear()
        self._no_evidence_streak = 0  # the expiry clock measures the gap, so it starts now
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
            self._no_evidence_streak = 0
            if old_status != "idle":
                await self._notify()
        except asyncio.CancelledError:
            pass

    async def _notify(self) -> None:
        async with self._condition:
            self._condition.notify_all()
