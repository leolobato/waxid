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
    async def test_next_track_auto_promotes(self, service):
        c1 = make_candidate(track_id=1, track_number=1)
        await service.feed([c1])
        await service.feed([c1])
        assert service.get_state().status == "playing"
        assert service.get_state().track_id == 1

        c2 = make_candidate(track_id=2, track_number=2)
        await service.feed([c2])
        assert service.get_state().status == "playing"
        assert service.get_state().track_id == 2

    @pytest.mark.asyncio
    async def test_shortcut_requires_same_album(self, service):
        c1 = make_candidate(track_id=1, album_id=1, track_number=1)
        await service.feed([c1])
        await service.feed([c1])
        assert service.get_state().status == "playing"

        c2 = make_candidate(track_id=2, album_id=99, track_number=2)
        await service.feed([c2])
        assert service.get_state().track_id != 2

    @pytest.mark.asyncio
    async def test_shortcut_requires_non_none_track_numbers(self, service):
        c1 = make_candidate(track_id=1, track_number=1)
        await service.feed([c1])
        await service.feed([c1])

        c2 = make_candidate(track_id=2, track_number=None)
        await service.feed([c2])
        assert service.get_state().track_id == 1

    @pytest.mark.asyncio
    async def test_shortcut_requires_min_score(self, service):
        c1 = make_candidate(track_id=1, track_number=1)
        await service.feed([c1])
        await service.feed([c1])

        c2 = make_candidate(track_id=2, track_number=2, score=5)
        await service.feed([c2])
        assert service.get_state().track_id == 1


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
    async def test_buffer_cleared_after_shortcut(self, service):
        c1 = make_candidate(track_id=1, track_number=1)
        await service.feed([c1])
        await service.feed([c1])
        assert service.get_state().status == "playing"

        c2 = make_candidate(track_id=2, track_number=2)
        await service.feed([c2])
        assert service.get_state().track_id == 2
        await service.feed([])
        assert service.get_state().status == "playing"
        assert service.get_state().track_id == 2


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
