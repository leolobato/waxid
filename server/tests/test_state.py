import asyncio
import time
import pytest
from unittest.mock import patch
from app.state import NowPlayingService
from app.models import MatchCandidate, NowPlayingResponse


def make_candidate(track_id=1, album_id=1, track_number=1, score=20,
                   offset_s=30.0, duration_s=180.0, confidence=2.0,
                   side="A", position="A1"):
    return MatchCandidate(
        track_id=track_id, artist="Artist", album="Album",
        album_id=album_id, track=f"Track {track_id}",
        track_number=track_number, year=2020, side=side, position=position,
        score=score, confidence=confidence, offset_s=offset_s,
        duration_s=duration_s, discogs_url=None, cover_url="/albums/1/cover",
    )


@pytest.fixture
def service():
    svc = NowPlayingService()
    yield svc
    svc.shutdown()


# Album layout for album_id=1: two tracks in order.
_ALBUM1_TRACKS = [
    {"track_id": 1, "album_id": 1, "side": "A", "position": "A1", "track_number": 1},
    {"track_id": 2, "album_id": 1, "side": "A", "position": "A2", "track_number": 2},
]


def _album1_get_tracks(album_id: int) -> list[dict]:
    return _ALBUM1_TRACKS if album_id == 1 else []


@pytest.fixture
def sequential_service():
    """NowPlayingService with a real album layout for album_id=1 (track_ids 1 and 2)."""
    svc = NowPlayingService(get_tracks_for_album=_album1_get_tracks)
    yield svc
    svc.shutdown()


class TestInitialState:
    def test_starts_idle(self, service):
        state = service.get_state()
        assert state.status == "idle"
        assert state.track_id is None


class TestStability:
    @pytest.mark.asyncio
    async def test_single_match_stays_listening(self, service):
        await service.feed([make_candidate()])
        assert service.get_state().status == "listening"

    @pytest.mark.asyncio
    async def test_two_of_three_matches_becomes_playing(self, service):
        c = make_candidate()
        await service.feed([c])
        await service.feed([])  # miss
        await service.feed([c])
        assert service.get_state().status == "playing"
        assert service.get_state().track_id == 1

    @pytest.mark.asyncio
    async def test_three_matches_becomes_playing(self, service):
        c = make_candidate()
        await service.feed([c])
        await service.feed([c])
        state = service.get_state()
        assert state.status == "playing"

    @pytest.mark.asyncio
    async def test_low_score_ignored(self, service):
        c = make_candidate(score=5)
        await service.feed([c])
        await service.feed([c])
        await service.feed([c])
        assert service.get_state().status == "listening"

    @pytest.mark.asyncio
    async def test_empty_feed_is_miss(self, service):
        await service.feed([])
        assert service.get_state().status == "listening"

    @pytest.mark.asyncio
    async def test_different_tracks_no_stability(self, service):
        await service.feed([make_candidate(track_id=1)])
        await service.feed([make_candidate(track_id=2)])
        await service.feed([make_candidate(track_id=3)])
        assert service.get_state().status == "listening"


class TestSequentialTrackShortcut:
    @pytest.mark.asyncio
    async def test_next_track_auto_promotes(self, sequential_service):
        c1 = make_candidate(track_id=1, track_number=1)
        await sequential_service.feed([c1])
        await sequential_service.feed([c1])
        assert sequential_service.get_state().status == "playing"
        assert sequential_service.get_state().track_id == 1

        c2 = make_candidate(track_id=2, track_number=2)
        await sequential_service.feed([c2])
        assert sequential_service.get_state().status == "playing"
        assert sequential_service.get_state().track_id == 2

    @pytest.mark.asyncio
    async def test_shortcut_requires_same_album(self, sequential_service):
        c1 = make_candidate(track_id=1, album_id=1, track_number=1)
        await sequential_service.feed([c1])
        await sequential_service.feed([c1])
        assert sequential_service.get_state().status == "playing"

        c2 = make_candidate(track_id=2, album_id=99, track_number=2)
        await sequential_service.feed([c2])
        assert sequential_service.get_state().track_id != 2

    @pytest.mark.asyncio
    async def test_shortcut_requires_track_in_layout(self, sequential_service):
        """Candidate with a track_id absent from the layout cache is not promoted sequentially."""
        c1 = make_candidate(track_id=1, track_number=1)
        await sequential_service.feed([c1])
        await sequential_service.feed([c1])

        # track_id=99 is not in album 1's layout, so the shortcut returns False.
        c2 = make_candidate(track_id=99, track_number=2)
        await sequential_service.feed([c2])
        assert sequential_service.get_state().track_id == 1

    @pytest.mark.asyncio
    async def test_shortcut_requires_min_score(self, sequential_service):
        c1 = make_candidate(track_id=1, track_number=1)
        await sequential_service.feed([c1])
        await sequential_service.feed([c1])

        c2 = make_candidate(track_id=2, track_number=2, score=3)
        await sequential_service.feed([c2])
        assert sequential_service.get_state().track_id == 1


class TestPositionClock:
    @pytest.mark.asyncio
    async def test_elapsed_computed_from_anchor(self, service):
        c = make_candidate(offset_s=30.0, duration_s=180.0)
        await service.feed([c])
        await service.feed([c])
        state = service.get_state()
        assert state.status == "playing"
        assert state.elapsed_s is not None
        assert state.elapsed_s >= 30.0

    @pytest.mark.asyncio
    async def test_elapsed_none_when_not_playing(self, service):
        await service.feed([make_candidate()])
        assert service.get_state().elapsed_s is None

    @pytest.mark.asyncio
    async def test_track_ends_when_elapsed_exceeds_duration(self, service):
        c = make_candidate(offset_s=179.0, duration_s=180.0)
        with patch("app.state.time") as mock_time:
            mock_time.time.return_value = 1000.0
            await service.feed([c])
            await service.feed([c])
            assert service.get_state().status == "playing"

            mock_time.time.return_value = 1002.0
            state = service.get_state()
            assert state.status == "listening"


class TestIdleTimer:
    @pytest.mark.asyncio
    async def test_idle_after_timeout(self, service, monkeypatch):
        from app import state as state_module
        monkeypatch.setattr(state_module, "IDLE_TIMEOUT_LISTENING_S", 0.1)
        await service.feed([make_candidate()])
        assert service.get_state().status == "listening"
        await asyncio.sleep(0.2)
        assert service.get_state().status == "idle"

    @pytest.mark.asyncio
    async def test_feed_resets_idle_timer(self, service, monkeypatch):
        from app import state as state_module
        monkeypatch.setattr(state_module, "IDLE_TIMEOUT_LISTENING_S", 0.2)
        await service.feed([make_candidate()])
        await asyncio.sleep(0.1)
        await service.feed([make_candidate()])
        await asyncio.sleep(0.15)
        assert service.get_state().status != "idle"

    @pytest.mark.asyncio
    async def test_idle_from_playing_state(self, service, monkeypatch):
        from app import state as state_module
        # _restart_idle_timer reads the status BEFORE the promotion fires
        # inside the same feed call, so the timeout in use is the
        # LISTENING one even though the test ends up in "playing".
        monkeypatch.setattr(state_module, "IDLE_TIMEOUT_LISTENING_S", 0.1)
        monkeypatch.setattr(state_module, "IDLE_TIMEOUT_PLAYING_S", 0.1)
        c = make_candidate()
        await service.feed([c])
        await service.feed([c])
        assert service.get_state().status == "playing"
        await asyncio.sleep(0.2)
        assert service.get_state().status == "idle"


class TestSequentialShortcutClearsBuffer:
    @pytest.mark.asyncio
    async def test_buffer_cleared_after_shortcut(self, sequential_service):
        c1 = make_candidate(track_id=1, track_number=1)
        await sequential_service.feed([c1])
        await sequential_service.feed([c1])
        assert sequential_service.get_state().status == "playing"

        c2 = make_candidate(track_id=2, track_number=2)
        await sequential_service.feed([c2])
        assert sequential_service.get_state().track_id == 2
        await sequential_service.feed([])
        assert sequential_service.get_state().status == "playing"
        assert sequential_service.get_state().track_id == 2


class TestSubscription:
    @pytest.mark.asyncio
    async def test_subscriber_receives_state_change(self, service):
        received = []

        async def listener():
            async for update in service.subscribe(timeout=1.0):
                if update is not None:
                    received.append(update)
                    break

        task = asyncio.create_task(listener())
        await asyncio.sleep(0.05)
        c = make_candidate()
        await service.feed([c])
        await service.feed([c])
        await asyncio.wait_for(task, timeout=2.0)
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_subscriber_gets_keepalive_on_timeout(self, service):
        received = []

        async def listener():
            async for update in service.subscribe(timeout=0.1):
                received.append(update)
                break

        task = asyncio.create_task(listener())
        await asyncio.wait_for(task, timeout=1.0)
        assert received == [None]


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
        # Layout cache for album 10 only; album 20 returns empty.
        svc = NowPlayingService(get_tracks_for_album=lambda aid: tracks_a if aid == 10 else [])
        try:
            ref = make_candidate(track_id=1, album_id=10, track_number=1)
            cand = make_candidate(track_id=99, album_id=20, track_number=1, score=10)
            svc._last_played = ref
            assert svc._is_sequential_track(cand) is False
        finally:
            svc.shutdown()


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

    @pytest.mark.asyncio
    async def test_feed_is_silence_agnostic(self, service):
        """feed() does not touch _silence_streak — that's the handler's job."""
        service.note_silence()
        service.note_silence()
        await service.feed([])
        assert service._silence_streak == 2


class TestAlbumLock:
    @pytest.mark.asyncio
    async def test_lock_set_on_first_promote(self, service):
        cand = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([cand])
        await service.feed([cand])
        assert service._locked_album_id == 10
        assert service._session_played == {1}

    @pytest.mark.asyncio
    async def test_lock_change_resets_session_played(self, service):
        cand1 = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([cand1])
        await service.feed([cand1])
        assert service._session_played == {1}
        # Force a promotion from a different album by clearing the current
        # state and feeding a strong cross-album candidate.
        service._current = None
        service._last_played = None
        service._buffer.clear()
        cand99 = make_candidate(track_id=99, album_id=20, score=20)
        await service.feed([cand99])
        await service.feed([cand99])
        assert service._locked_album_id == 20
        assert service._session_played == {99}

    @pytest.mark.asyncio
    async def test_same_album_promote_adds_to_session_played(self, service):
        cand1 = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([cand1])
        await service.feed([cand1])
        assert service._session_played == {1}
        # Promote a different track from the same album via a fresh stability run.
        service._current = None
        service._last_played = None
        service._buffer.clear()
        cand2 = make_candidate(track_id=2, album_id=10, score=20)
        await service.feed([cand2])
        await service.feed([cand2])
        assert service._locked_album_id == 10
        assert service._session_played == {1, 2}


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


class TestCrossAlbumRelease:
    @pytest.mark.asyncio
    async def test_lock_moves_when_off_album_wins_stability(self, service):
        # Lock on album 10.
        await service.feed([make_candidate(track_id=1, album_id=10, score=20)])
        await service.feed([make_candidate(track_id=1, album_id=10, score=20)])
        assert service._locked_album_id == 10
        # Drop back to listening so stability buffer starts fresh.
        service._current = None
        service._status = "listening"
        service._buffer.clear()
        # Two strong frames for an off-album candidate trigger stability promotion.
        await service.feed([make_candidate(track_id=99, album_id=20, score=20)])
        await service.feed([make_candidate(track_id=99, album_id=20, score=20)])
        assert service._locked_album_id == 20
        assert service._session_played == {99}


class TestLockRelease:
    @pytest.mark.asyncio
    async def test_lock_released_after_sustained_silence(self, service):
        await service.feed([make_candidate(track_id=1, album_id=10, score=20)])
        await service.feed([make_candidate(track_id=1, album_id=10, score=20)])
        assert service._locked_album_id == 10
        # Silence streak reaches the release threshold.
        for _ in range(20):
            service.note_silence()
        await service.feed([])
        assert service._locked_album_id is None
        assert service._session_played == set()
        assert service._last_played is None

    @pytest.mark.asyncio
    async def test_idle_countdown_clears_lock(self, service):
        await service.feed([make_candidate(track_id=1, album_id=10, score=20)])
        await service.feed([make_candidate(track_id=1, album_id=10, score=20)])
        assert service._locked_album_id == 10
        # Drive the idle path synchronously with a short timeout.
        service._status = "listening"
        await service._idle_countdown(0.01)
        assert service._locked_album_id is None
        assert service._session_played == set()
        assert service._silence_streak == 0


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

    def test_on_album_deleted_drops_current_track_if_on_deleted_album(self):
        svc = self._layout_svc([])
        try:
            cur = make_candidate(track_id=1, album_id=10, score=20)
            svc._current = cur
            svc._status = "playing"
            svc._last_played = cur
            svc._locked_album_id = 10
            svc.on_album_deleted(10)
            assert svc._current is None
            assert svc._last_played is None
            assert svc._status == "listening"
        finally:
            svc.shutdown()

    def test_on_album_deleted_leaves_current_when_other_album_deleted(self):
        svc = self._layout_svc([])
        try:
            cur = make_candidate(track_id=1, album_id=10, score=20)
            svc._current = cur
            svc._status = "playing"
            svc.on_album_deleted(99)
            assert svc._current is cur
            assert svc._status == "playing"
        finally:
            svc.shutdown()

    def test_on_track_deleted_drops_current_track_if_deleted(self):
        svc = self._layout_svc([])
        try:
            cur = make_candidate(track_id=5, album_id=10, score=20)
            svc._current = cur
            svc._status = "playing"
            svc._last_played = cur
            svc.on_track_deleted(5, 10)
            assert svc._current is None
            assert svc._last_played is None
            assert svc._status == "listening"
        finally:
            svc.shutdown()


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


class TestTrackEndBufferClear:
    @pytest.mark.asyncio
    async def test_ended_track_does_not_zombie_repromote(self, service):
        """After a track ends by duration (detected via get_state, like the
        SSE poll), leftover buffer frames must not re-promote it on the next
        silent feed."""
        c = make_candidate(offset_s=170.0, duration_s=180.0)
        with patch("app.state.time") as mock_time:
            mock_time.time.return_value = 1000.0
            await service.feed([c])
            await service.feed([c])   # promotes (buffer cleared here)
            await service.feed([c])   # post-promote frame 1
            await service.feed([c])   # post-promote frame 2
            assert service.get_state().status == "playing"

            mock_time.time.return_value = 1015.0  # elapsed 185 >= 180
            assert service.get_state().status == "listening"  # ends here

            await service.feed([])
            assert service.get_state().status == "listening"  # no zombie

    @pytest.mark.asyncio
    async def test_grace_misses_drop_to_listening(self, service):
        """6 consecutive frames without the current track drop to listening
        and remember it as last_played."""
        c = make_candidate(track_id=1, album_id=1, score=20)
        await service.feed([c])
        await service.feed([c])
        assert service.get_state().status == "playing"
        for _ in range(6):
            await service.feed([])
        assert service.get_state().status == "listening"
        assert service._last_played is not None
        assert service._last_played.track_id == 1


class TestGuardSeesWeakCurrent:
    @pytest.mark.asyncio
    async def test_weakly_maintained_current_still_guarded(self, service):
        """A current track alive at 4-5 (below the buffer bar) still forces
        cross-album challengers to clear the x1.5 margin."""
        cur = make_candidate(track_id=1, album_id=1, score=20)
        await service.feed([cur])
        await service.feed([cur])
        weak = make_candidate(track_id=1, album_id=1, score=5)   # alive, not buffered
        rival = make_candidate(track_id=9, album_id=3, score=7)  # 7 < 5 * 1.5
        await service.feed([weak, rival])
        await service.feed([weak, rival])
        assert service.get_state().track_id == 1

    @pytest.mark.asyncio
    async def test_clear_challenger_still_dethrones_weak_current(self, service):
        cur = make_candidate(track_id=1, album_id=1, score=20)
        await service.feed([cur])
        await service.feed([cur])
        weak = make_candidate(track_id=1, album_id=1, score=5)
        rival = make_candidate(track_id=9, album_id=3, score=8)  # 8 > 5 * 1.5
        await service.feed([weak, rival])
        await service.feed([weak, rival])
        assert service.get_state().track_id == 9

    @pytest.mark.asyncio
    async def test_same_album_challenger_blocked_by_recent_best(self, service):
        """The incumbent is judged by its recent BEST, the challenger by its
        latest score — a fading current track isn't stolen by a mid-score
        album-mate."""
        cur20 = make_candidate(track_id=1, album_id=1, score=20)
        await service.feed([cur20])
        await service.feed([cur20])  # promote clears the buffer
        neighbor = make_candidate(track_id=2, album_id=1, score=10)
        await service.feed([cur20, neighbor])
        fading = make_candidate(track_id=1, album_id=1, score=6)
        await service.feed([fading, neighbor])
        # neighbor (10) is stable but <= current's recent best (20).
        assert service.get_state().track_id == 1
