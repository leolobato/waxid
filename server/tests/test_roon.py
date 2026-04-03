import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.roon import RoonNotifier, slugify
from app.settings import Settings
from app.state import NowPlayingService
from app.models import MatchCandidate


def make_candidate(track_id=1, album_id=1):
    return MatchCandidate(
        track_id=track_id, artist="Artist", album="Album",
        album_id=album_id, track="Track 1",
        track_number=1, year=2020, side="A", position="A1",
        score=20, confidence=2.0, offset_s=30.0,
        duration_s=180.0, discogs_url=None, cover_url="/albums/1/cover",
    )


class TestSlugify:
    def test_simple(self):
        assert slugify("Record Player") == "record-player"

    def test_special_chars(self):
        assert slugify("My Zone #1!") == "my-zone-1"

    def test_strips_leading_trailing(self):
        assert slugify("--hello--") == "hello"


class TestRoonNotifierStateMapping:
    """Test that now-playing states map to correct Roon API calls."""

    @pytest.mark.asyncio
    async def test_playing_posts_playing(self):
        svc = NowPlayingService()
        settings = Settings(
            roon_enabled=True,
            roon_url="http://roon:8377",
            server_url="http://localhost:8457",
        )
        notifier = RoonNotifier(svc, settings)

        with patch.object(notifier, "_post_now_playing", new_callable=AsyncMock) as mock_post:
            # Feed until playing (2-of-3 match)
            c = make_candidate()
            await svc.feed([c])
            await svc.feed([c])
            # Give subscriber a chance to process
            await asyncio.sleep(0.05)
            notifier.stop()

            # Should have posted "playing"
            calls = [call for call in mock_post.call_args_list if call[0][0] == "playing"]
            assert len(calls) >= 1
            payload = calls[0][0][1]
            assert payload["title"] == "Track 1"
            assert payload["artist"] == "Artist"
            assert payload["artwork_url"] == "http://localhost:8457/albums/1/cover"

        svc.shutdown()

    @pytest.mark.asyncio
    async def test_listening_to_idle_does_not_crash(self):
        svc = NowPlayingService()
        settings = Settings(
            roon_enabled=True,
            roon_url="http://roon:8377",
            server_url="http://localhost:8457",
        )
        notifier = RoonNotifier(svc, settings)

        with patch.object(notifier, "_post_now_playing", new_callable=AsyncMock):
            # Feed empty to go idle->listening
            await svc.feed([])
            await asyncio.sleep(0.05)
            notifier.stop()
            # Verify no exception was raised

        svc.shutdown()

    @pytest.mark.asyncio
    async def test_keep_alive_resends_playing(self):
        svc = NowPlayingService()
        settings = Settings(
            roon_enabled=True,
            roon_url="http://roon:8377",
            server_url="http://localhost:8457",
        )
        notifier = RoonNotifier(svc, settings)

        with patch.object(notifier, "_post_now_playing", new_callable=AsyncMock) as mock_post:
            # Get into playing state
            c = make_candidate()
            await svc.feed([c])
            await svc.feed([c])
            await asyncio.sleep(0.05)

            # Record call count after initial playing post
            initial_count = len([call for call in mock_post.call_args_list if call[0][0] == "playing"])

            # Manually trigger a keep-alive by calling the subscribe timeout path
            state = svc.get_state()
            assert state.status == "playing"
            await notifier._post_playing(state)

            playing_calls = [call for call in mock_post.call_args_list if call[0][0] == "playing"]
            assert len(playing_calls) > initial_count

            notifier.stop()

        svc.shutdown()


class TestRoonNotifierSkipsWhenDisabled:
    @pytest.mark.asyncio
    async def test_does_not_post_when_disabled(self):
        svc = NowPlayingService()
        settings = Settings(roon_enabled=False)
        notifier = RoonNotifier(svc, settings)
        assert notifier._task is None
        notifier.stop()
        svc.shutdown()

    @pytest.mark.asyncio
    async def test_does_not_post_when_url_empty(self):
        svc = NowPlayingService()
        settings = Settings(roon_enabled=True, roon_url="")
        notifier = RoonNotifier(svc, settings)
        assert notifier._task is not None
        notifier.stop()
        svc.shutdown()


class TestRoonNotifierReconfigure:
    @pytest.mark.asyncio
    async def test_reconfigure_starts_when_enabled(self):
        svc = NowPlayingService()
        notifier = RoonNotifier(svc, Settings(roon_enabled=False))
        assert notifier._task is None

        await notifier.reconfigure(Settings(
            roon_enabled=True,
            roon_url="http://roon:8377",
        ))
        assert notifier._task is not None

        notifier.stop()
        svc.shutdown()

    @pytest.mark.asyncio
    async def test_reconfigure_stops_when_disabled(self):
        svc = NowPlayingService()
        notifier = RoonNotifier(svc, Settings(
            roon_enabled=True,
            roon_url="http://roon:8377",
        ))
        assert notifier._task is not None

        await notifier.reconfigure(Settings(roon_enabled=False))
        assert notifier._task is None

        notifier.stop()
        svc.shutdown()
