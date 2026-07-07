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


class TestSideProgress:
    """get_state() surfaces how far the current track is through its side."""

    # Side A has two tracks, side B has one.
    _TRACKS = [
        {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
        {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
        {"track_id": 3, "album_id": 10, "side": "B", "position": "B1", "track_number": 3},
    ]

    def _svc(self):
        return NowPlayingService(get_tracks_for_album=lambda aid: self._TRACKS if aid == 10 else [])

    def test_mid_side_track_is_not_last(self):
        svc = self._svc()
        try:
            svc._promote(make_candidate(track_id=1, album_id=10, side="A", position="A1"))
            state = svc.get_state()
            assert state.tracks_on_side == 2
            assert state.is_last_on_side is False
            assert state.sides == {"A": 2, "B": 1}
        finally:
            svc.shutdown()

    def test_last_track_on_side_is_flagged(self):
        svc = self._svc()
        try:
            svc._promote(make_candidate(track_id=2, album_id=10, side="A", position="A2"))
            state = svc.get_state()
            assert state.tracks_on_side == 2
            assert state.is_last_on_side is True
            assert state.sides == {"A": 2, "B": 1}
        finally:
            svc.shutdown()

    def test_sole_track_on_side_is_last(self):
        svc = self._svc()
        try:
            svc._promote(make_candidate(track_id=3, album_id=10, side="B", position="B1"))
            state = svc.get_state()
            assert state.tracks_on_side == 1
            assert state.is_last_on_side is True
            assert state.sides == {"A": 2, "B": 1}
        finally:
            svc.shutdown()

    def test_bonus_track_without_side_reports_none(self):
        tracks = [{"track_id": 9, "album_id": 10, "side": None, "position": None, "track_number": 1}]
        svc = NowPlayingService(get_tracks_for_album=lambda aid: tracks if aid == 10 else [])
        try:
            svc._promote(make_candidate(track_id=9, album_id=10, side=None, position=None))
            state = svc.get_state()
            assert state.tracks_on_side is None
            assert state.is_last_on_side is None
            assert state.sides is None
        finally:
            svc.shutdown()

    def test_bonus_track_excluded_from_side_counts(self):
        tracks = [
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
            {"track_id": 9, "album_id": 10, "side": None, "position": None, "track_number": 3},
        ]
        svc = NowPlayingService(get_tracks_for_album=lambda aid: tracks if aid == 10 else [])
        try:
            svc._promote(make_candidate(track_id=2, album_id=10, side="A", position="A2"))
            state = svc.get_state()
            assert state.sides == {"A": 2}
            assert state.tracks_on_side == 2
            assert state.is_last_on_side is True
        finally:
            svc.shutdown()

    def test_unknown_album_layout_reports_none(self):
        svc = NowPlayingService()  # no track source -> empty layout
        try:
            svc._promote(make_candidate(track_id=1, album_id=10, side="A", position="A1"))
            state = svc.get_state()
            assert state.tracks_on_side is None
            assert state.is_last_on_side is None
            assert state.sides is None
        finally:
            svc.shutdown()


class TestFinishedSignal:
    """A one-shot finished_track is emitted only when a track plays through to
    (essentially) its end — not on mid-track stops."""

    _TRACKS = [
        {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
        {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
    ]

    def _svc(self):
        return NowPlayingService(get_tracks_for_album=lambda aid: self._TRACKS if aid == 10 else [])

    def _play(self, svc, track_id, duration_s=100.0, elapsed=0.0):
        cand = make_candidate(
            track_id=track_id, album_id=10, duration_s=duration_s,
            side="A", position=f"A{track_id}",
        )
        svc._promote(cand)
        svc._anchor_offset = elapsed
        svc._anchor_time = time.time()
        return cand

    def test_natural_completion_reports_finished_track(self):
        svc = self._svc()
        try:
            self._play(svc, 2, duration_s=100.0, elapsed=100.0)
            svc._check_track_ended()  # elapsed >= duration -> _end_track
            state = svc.get_state()
            assert state.status != "playing"
            assert state.finished_track is not None
            assert state.finished_track.track_id == 2
            assert state.finished_track.is_last_on_side is True
            assert state.finished_track.tracks_on_side == 2
            assert state.finished_track.sides == {"A": 2}
        finally:
            svc.shutdown()

    def test_drop_below_threshold_is_not_finished(self):
        svc = self._svc()
        try:
            self._play(svc, 1, duration_s=100.0, elapsed=50.0)
            svc._drop_current()
            assert svc.get_state().finished_track is None
        finally:
            svc.shutdown()

    def test_drop_near_end_is_finished(self):
        svc = self._svc()
        try:
            self._play(svc, 2, duration_s=100.0, elapsed=95.0)
            svc._drop_current()
            ft = svc.get_state().finished_track
            assert ft is not None and ft.track_id == 2
        finally:
            svc.shutdown()

    def test_next_track_marks_previous_finished_when_near_end(self):
        svc = self._svc()
        try:
            self._play(svc, 1, duration_s=100.0, elapsed=95.0)
            svc._promote(make_candidate(track_id=2, album_id=10, duration_s=100.0, side="A", position="A2"))
            ft = svc.get_state().finished_track
            assert ft is not None and ft.track_id == 1
        finally:
            svc.shutdown()

    def test_next_track_does_not_mark_previous_when_early(self):
        svc = self._svc()
        try:
            self._play(svc, 1, duration_s=100.0, elapsed=20.0)
            svc._promote(make_candidate(track_id=2, album_id=10, duration_s=100.0, side="A", position="A2"))
            assert svc.get_state().finished_track is None
        finally:
            svc.shutdown()

    def test_missing_duration_is_never_finished(self):
        svc = self._svc()
        try:
            self._play(svc, 1, duration_s=None, elapsed=9999.0)
            svc._drop_current()
            assert svc.get_state().finished_track is None
        finally:
            svc.shutdown()

    @pytest.mark.asyncio
    async def test_finished_track_delivered_to_every_subscriber(self):
        """The race fix: a one-shot completion reaches each subscriber, not
        just whichever waiter the condition happens to wake first."""
        svc = self._svc()
        try:
            got_a: list = []
            got_b: list = []

            async def listener(sink):
                async for update in svc.subscribe(timeout=1.0):
                    if update is not None:
                        sink.append(update)
                        break

            ta = asyncio.create_task(listener(got_a))
            tb = asyncio.create_task(listener(got_b))
            await asyncio.sleep(0.05)
            self._play(svc, 2, duration_s=100.0, elapsed=100.0)
            svc._check_track_ended()  # marks finished + status listening
            await svc._notify()
            await asyncio.wait_for(asyncio.gather(ta, tb), timeout=2.0)
            assert got_a[0].finished_track is not None
            assert got_a[0].finished_track.track_id == 2
            assert got_b[0].finished_track is not None
            assert got_b[0].finished_track.track_id == 2
        finally:
            svc.shutdown()

    @pytest.mark.asyncio
    async def test_finished_track_not_repeated_to_same_subscriber(self):
        """One-shot per subscriber: a later frame with no new completion does
        not replay the same finished_track."""
        svc = self._svc()
        try:
            received: list = []

            async def listener():
                async for update in svc.subscribe(timeout=1.0):
                    if update is not None:
                        received.append(update)
                        if len(received) >= 2:
                            break

            task = asyncio.create_task(listener())
            await asyncio.sleep(0.05)
            self._play(svc, 2, duration_s=100.0, elapsed=100.0)
            svc._check_track_ended()
            await svc._notify()          # first update carries the completion
            await asyncio.sleep(0.05)
            await svc._notify()          # nothing new finished since
            await asyncio.wait_for(task, timeout=2.0)
            assert received[0].finished_track is not None
            assert received[1].finished_track is None
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

    @pytest.mark.asyncio
    async def test_organic_drop_gets_full_expiry_window(self, service):
        """A track maintained weakly (4-5, below evidence bar) grows the
        streak while playing; when it organically drops via grace misses,
        the expiry clock must restart — the context keeps the full
        15-frame gap window."""
        strong = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([strong])
        await service.feed([strong])
        weak = make_candidate(track_id=1, album_id=10, score=5)
        for _ in range(16):  # streak would grow well past the threshold
            await service.feed([weak])
        assert service.get_state().status == "playing"
        for _ in range(6):  # grace misses -> organic drop
            await service.feed([])
        assert service.get_state().status == "listening"
        assert service._last_played is not None
        for _ in range(14):
            await service.feed([])
        assert service._last_played is not None  # full window survives
        await service.feed([])  # 15th gap frame
        assert service._last_played is None

    @pytest.mark.asyncio
    async def test_expiry_suppressed_while_playing(self, service):
        """A playing track maintained below the evidence bar is never
        expired out from under itself."""
        strong = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([strong])
        await service.feed([strong])
        weak = make_candidate(track_id=1, album_id=10, score=5)
        for _ in range(20):
            await service.feed([weak])
        assert service.get_state().status == "playing"
        assert service._current is not None

    @pytest.mark.asyncio
    async def test_cross_album_candidates_do_not_reset_streak(self, service):
        """Off-album matches are not evidence for the context album — the
        expiry clock keeps running (shifting track_ids so nothing
        stabilizes into a promote)."""
        played = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([played])
        await service.feed([played])
        _drop_to_listening(service, played)
        for i, tid in enumerate([51, 52, 53], start=1):
            await service.feed([make_candidate(track_id=tid, album_id=9, score=20)])
            assert service._no_evidence_streak == i
        assert service._last_played is not None


class TestDeletionHooks:
    def _layout_svc(self, tracks):
        return NowPlayingService(get_tracks_for_album=lambda _aid: tracks)

    def test_on_album_deleted_drops_layout_cache(self):
        svc = self._layout_svc([
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
        ])
        try:
            svc._album_layout(10)
            svc.on_album_deleted(10)
            assert 10 not in svc._album_layout_cache
        finally:
            svc.shutdown()

    def test_on_track_deleted_invalidates_layout(self):
        svc = self._layout_svc([
            {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
            {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
        ])
        try:
            svc._album_layout(10)
            svc.on_track_deleted(1, 10)
            assert 10 not in svc._album_layout_cache
        finally:
            svc.shutdown()

    def test_on_album_deleted_drops_current_track_if_on_deleted_album(self):
        svc = self._layout_svc([])
        try:
            cur = make_candidate(track_id=1, album_id=10, score=20)
            svc._current = cur
            svc._status = "playing"
            svc._last_played = cur
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

    def test_on_album_deleted_clears_last_played_when_between_tracks(self):
        svc = self._layout_svc([])
        try:
            last = make_candidate(track_id=1, album_id=10, score=20)
            svc._last_played = last   # between-tracks: no _current
            svc._status = "listening"
            svc.on_album_deleted(10)
            assert svc._last_played is None
            assert svc._status == "listening"  # status unchanged
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

    @pytest.mark.asyncio
    async def test_lone_junk_at_promote_bar_does_not_promote_after_track_end(self, service):
        """No-current window with context: a lone cross-album false positive
        repeating at the promote bar has no field to be measured against —
        the vacuum floor (>= 9) must block it."""
        played = make_candidate(track_id=1, album_id=10, score=20)
        await service.feed([played])
        await service.feed([played])
        _drop_to_listening(service, played)
        junk = make_candidate(track_id=50, album_id=9, score=6)
        await service.feed([junk])
        await service.feed([junk])
        assert service.get_state().status == "listening"

    @pytest.mark.asyncio
    async def test_lone_junk_at_promote_bar_does_not_promote_without_context(self, service):
        junk = make_candidate(track_id=50, album_id=9, score=6)
        await service.feed([junk])
        await service.feed([junk])
        assert service.get_state().status == "listening"

    @pytest.mark.asyncio
    async def test_clear_lone_candidate_promotes_in_vacuum(self, service):
        """A genuinely playing record clears the vacuum floor quickly."""
        real = make_candidate(track_id=7, album_id=2, score=9)
        await service.feed([real])
        await service.feed([real])
        assert service.get_state().status == "playing"
        assert service.get_state().track_id == 7


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
