import asyncio
import time
import pytest
from unittest.mock import patch
from app.state import NowPlayingService
from app.models import MatchCandidate, NowPlayingResponse


def make_candidate(track_id=1, album_id=1, track_number=1, score=20,
                   offset_s=30.0, duration_s=180.0, confidence=2.0):
    return MatchCandidate(
        track_id=track_id, artist="Artist", album="Album",
        album_id=album_id, track=f"Track {track_id}",
        track_number=track_number, year=2020, side="A", position="A1",
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

        c2 = make_candidate(track_id=2, track_number=2, score=5)
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
    async def test_idle_after_timeout(self, service):
        service._idle_timeout_s = 0.1
        await service.feed([make_candidate()])
        assert service.get_state().status == "listening"
        await asyncio.sleep(0.2)
        assert service.get_state().status == "idle"

    @pytest.mark.asyncio
    async def test_feed_resets_idle_timer(self, service):
        service._idle_timeout_s = 0.2
        await service.feed([make_candidate()])
        await asyncio.sleep(0.1)
        await service.feed([make_candidate()])
        await asyncio.sleep(0.15)
        assert service.get_state().status != "idle"

    @pytest.mark.asyncio
    async def test_idle_from_playing_state(self, service):
        service._idle_timeout_s = 0.1
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
        service._pending_candidates.clear()
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
        service._pending_candidates.clear()
        cand2 = make_candidate(track_id=2, album_id=10, score=20)
        await service.feed([cand2])
        await service.feed([cand2])
        assert service._locked_album_id == 10
        assert service._session_played == {1, 2}


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
