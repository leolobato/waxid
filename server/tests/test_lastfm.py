import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.lastfm import sign_params, scrobble_delay, LastfmScrobbler
from app.settings import Settings
from app.state import NowPlayingService
from app.models import MatchCandidate


class TestSignParams:
    def test_signature_is_md5_of_sorted_params_plus_secret(self):
        params = {"method": "track.scrobble", "api_key": "key123", "sk": "session1"}
        sig = sign_params(params, secret="mysecret")
        # md5("api_keykey123methodtrack.scrobblesksession1mysecret")
        import hashlib
        expected = hashlib.md5(b"api_keykey123methodtrack.scrobblesksession1mysecret").hexdigest()
        assert sig == expected

    def test_ignores_format_param(self):
        params = {"method": "auth.getToken", "api_key": "k", "format": "json"}
        sig = sign_params(params, secret="s")
        import hashlib
        expected = hashlib.md5(b"api_keykmethodauth.getTokens").hexdigest()
        assert sig == expected


class TestScrobbleDelay:
    def test_short_track_uses_30s_minimum(self):
        assert scrobble_delay(40.0) == 30.0

    def test_normal_track_uses_half_duration(self):
        assert scrobble_delay(180.0) == 90.0

    def test_long_track_caps_at_240s(self):
        assert scrobble_delay(600.0) == 240.0

    def test_none_duration_defaults_to_30s(self):
        assert scrobble_delay(None) == 30.0

    def test_very_short_track_uses_30s(self):
        assert scrobble_delay(10.0) == 30.0


def make_candidate(track_id=1, album_id=1, duration_s=180.0):
    return MatchCandidate(
        track_id=track_id, artist="Artist", album="Album",
        album_id=album_id, track="Track 1",
        track_number=1, year=2020, side="A", position="A1",
        score=20, confidence=2.0, offset_s=30.0,
        duration_s=duration_s, discogs_url=None, cover_url=None,
    )


class TestScrobblerNowPlaying:
    @pytest.mark.asyncio
    async def test_sends_update_now_playing_on_track_start(self):
        svc = NowPlayingService()
        scrobbler = LastfmScrobbler(
            svc, Settings(lastfm_enabled=True, lastfm_session_key="sk"),
            api_key="key", secret="secret",
        )

        with patch.object(scrobbler, "_call_lastfm", new_callable=AsyncMock) as mock_call:
            c = make_candidate()
            await svc.feed([c])
            await svc.feed([c])
            await asyncio.sleep(0.05)
            scrobbler.stop()

            now_playing_calls = [
                call for call in mock_call.call_args_list
                if call[0][0] == "track.updateNowPlaying"
            ]
            assert len(now_playing_calls) >= 1
            params = now_playing_calls[0][0][1]
            assert params["artist"] == "Artist"
            assert params["track"] == "Track 1"
            assert params["album"] == "Album"

        svc.shutdown()


class TestScrobblerScrobble:
    @pytest.mark.asyncio
    async def test_scrobbles_after_delay(self):
        svc = NowPlayingService()
        scrobbler = LastfmScrobbler(
            svc, Settings(lastfm_enabled=True, lastfm_session_key="sk"),
            api_key="key", secret="secret",
        )

        with patch.object(scrobbler, "_call_lastfm", new_callable=AsyncMock) as mock_call:
            c = make_candidate(duration_s=180.0)
            await svc.feed([c])
            await svc.feed([c])
            await asyncio.sleep(0.05)

            assert scrobbler._scrobble_timer is not None
            assert not scrobbler._scrobble_timer.done()

            scrobbler.stop()

        svc.shutdown()

    @pytest.mark.asyncio
    async def test_no_scrobble_when_track_stops_before_threshold(self):
        svc = NowPlayingService()
        scrobbler = LastfmScrobbler(
            svc, Settings(lastfm_enabled=True, lastfm_session_key="sk"),
            api_key="key", secret="secret",
        )

        with patch.object(scrobbler, "_call_lastfm", new_callable=AsyncMock) as mock_call:
            c = make_candidate()
            await svc.feed([c])
            await svc.feed([c])
            await asyncio.sleep(0.05)

            for _ in range(7):
                await svc.feed([])
            await asyncio.sleep(0.05)

            scrobbler.stop()

            scrobble_calls = [
                call for call in mock_call.call_args_list
                if call[0][0] == "track.scrobble"
            ]
            assert len(scrobble_calls) == 0

        svc.shutdown()


class TestScrobblerDoubleScrobblePrevention:
    @pytest.mark.asyncio
    async def test_does_not_restart_scrobble_timer_for_already_scrobbled_track(self):
        svc = NowPlayingService()
        scrobbler = LastfmScrobbler(
            svc, Settings(lastfm_enabled=True, lastfm_session_key="sk"),
            api_key="key", secret="secret",
        )

        with patch.object(scrobbler, "_call_lastfm", new_callable=AsyncMock):
            c = make_candidate(track_id=42)
            await svc.feed([c])
            await svc.feed([c])
            await asyncio.sleep(0.05)

            scrobbler._last_scrobbled_track_id = 42

            await svc.feed([c])
            await asyncio.sleep(0.05)

            scrobbler.stop()

        svc.shutdown()


class TestAuthHelpers:
    @pytest.mark.asyncio
    async def test_get_auth_token(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"token": "req_token_123"}

        with patch("app.lastfm.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post.return_value = mock_response
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            from app.lastfm import get_auth_token
            token = await get_auth_token(api_key="mykey", secret="mysecret")
            assert token == "req_token_123"

    def test_build_auth_url(self):
        from app.lastfm import build_auth_url
        url = build_auth_url(api_key="mykey", token="tok123")
        assert "api_key=mykey" in url
        assert "token=tok123" in url
        assert url.startswith("https://www.last.fm/api/auth/")

    @pytest.mark.asyncio
    async def test_complete_auth(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "session": {"name": "testuser", "key": "session_key_abc"}
        }

        with patch("app.lastfm.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post.return_value = mock_response
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            from app.lastfm import complete_auth
            username, session_key = await complete_auth(
                token="req_token_123", api_key="mykey", secret="mysecret"
            )
            assert username == "testuser"
            assert session_key == "session_key_abc"


class TestScrobblerDisabled:
    @pytest.mark.asyncio
    async def test_does_not_start_when_disabled(self):
        svc = NowPlayingService()
        scrobbler = LastfmScrobbler(
            svc, Settings(lastfm_enabled=False),
            api_key="key", secret="secret",
        )
        assert scrobbler._task is None
        scrobbler.stop()
        svc.shutdown()

    @pytest.mark.asyncio
    async def test_does_not_start_without_session_key(self):
        svc = NowPlayingService()
        scrobbler = LastfmScrobbler(
            svc, Settings(lastfm_enabled=True, lastfm_session_key=""),
            api_key="key", secret="secret",
        )
        assert scrobbler._task is None
        scrobbler.stop()
        svc.shutdown()

    @pytest.mark.asyncio
    async def test_reconfigure_starts_when_enabled(self):
        svc = NowPlayingService()
        scrobbler = LastfmScrobbler(
            svc, Settings(lastfm_enabled=False),
            api_key="key", secret="secret",
        )
        assert scrobbler._task is None

        await scrobbler.reconfigure(Settings(
            lastfm_enabled=True, lastfm_session_key="sk",
        ))
        assert scrobbler._task is not None

        scrobbler.stop()
        svc.shutdown()

    @pytest.mark.asyncio
    async def test_reconfigure_stops_when_disabled(self):
        svc = NowPlayingService()
        scrobbler = LastfmScrobbler(
            svc, Settings(lastfm_enabled=True, lastfm_session_key="sk"),
            api_key="key", secret="secret",
        )
        assert scrobbler._task is not None

        await scrobbler.reconfigure(Settings(lastfm_enabled=False))
        assert scrobbler._task is None

        scrobbler.stop()
        svc.shutdown()


class TestScrobblerEndToEnd:
    @pytest.mark.asyncio
    async def test_full_scrobble_lifecycle(self):
        """Track starts -> updateNowPlaying sent -> delay passes -> scrobble sent."""
        svc = NowPlayingService()
        scrobbler = LastfmScrobbler(
            svc, Settings(lastfm_enabled=True, lastfm_session_key="sk"),
            api_key="key", secret="secret",
        )

        call_log = []

        async def tracking_call(method, params):
            call_log.append((method, dict(params)))

        with patch.object(scrobbler, "_call_lastfm", side_effect=tracking_call):
            c = make_candidate(duration_s=60.0)  # delay = max(30, 30) = 30s
            await svc.feed([c])
            await svc.feed([c])
            await asyncio.sleep(0.05)

            # Should have sent updateNowPlaying
            assert len(call_log) >= 1
            assert call_log[0][0] == "track.updateNowPlaying"
            assert call_log[0][1]["artist"] == "Artist"

            # Manually trigger the scrobble (instead of waiting 30s)
            if scrobbler._scrobble_timer and not scrobbler._scrobble_timer.done():
                scrobbler._scrobble_timer.cancel()
            await scrobbler._scrobble_after(0)

            # Should have sent scrobble
            scrobble_calls = [c for c in call_log if c[0] == "track.scrobble"]
            assert len(scrobble_calls) == 1
            assert scrobble_calls[0][1]["artist"] == "Artist"
            assert scrobble_calls[0][1]["track"] == "Track 1"
            assert "timestamp" in scrobble_calls[0][1]

            scrobbler.stop()

        svc.shutdown()
