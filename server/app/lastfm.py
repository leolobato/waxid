from __future__ import annotations

import asyncio
import hashlib
import logging
import time

import httpx

from .settings import Settings
from .state import NowPlayingService

logger = logging.getLogger(__name__)

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"


def sign_params(params: dict[str, str], secret: str) -> str:
    """Generate Last.fm API signature: md5 of sorted key-value pairs + secret."""
    filtered = {k: v for k, v in params.items() if k != "format"}
    parts = "".join(f"{k}{v}" for k, v in sorted(filtered.items()))
    return hashlib.md5((parts + secret).encode()).hexdigest()


def scrobble_delay(duration_s: float | None) -> float:
    """Seconds to wait before scrobbling. Last.fm rule: played for 30s AND half duration (max 240s)."""
    if duration_s is None:
        return 30.0
    return max(30.0, min(duration_s / 2, 240.0))


def build_auth_url(api_key: str, token: str) -> str:
    """Build the Last.fm authorization URL for the user to visit."""
    return f"https://www.last.fm/api/auth/?api_key={api_key}&token={token}"


async def get_auth_token(api_key: str, secret: str) -> str:
    """Request a temporary auth token from Last.fm."""
    params = {"method": "auth.getToken", "api_key": api_key}
    params["api_sig"] = sign_params(params, secret)
    params["format"] = "json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(LASTFM_API_URL, data=params)
        resp.raise_for_status()
        return resp.json()["token"]


async def complete_auth(token: str, api_key: str, secret: str) -> tuple[str, str]:
    """Exchange an authorized token for a session key. Returns (username, session_key)."""
    params = {"method": "auth.getSession", "api_key": api_key, "token": token}
    params["api_sig"] = sign_params(params, secret)
    params["format"] = "json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(LASTFM_API_URL, data=params)
        resp.raise_for_status()
        data = resp.json()
        session = data["session"]
        return session["name"], session["key"]


class LastfmScrobbler:
    def __init__(
        self,
        now_playing: NowPlayingService,
        settings: Settings,
        api_key: str,
        secret: str,
    ):
        self._now_playing = now_playing
        self._settings = settings
        self._api_key = api_key
        self._secret = secret
        self._client = httpx.AsyncClient(timeout=5.0)
        self._task: asyncio.Task | None = None
        self._current_track_id: int | None = None
        self._current_started_at: float | None = None
        self._current_duration: float | None = None
        self._last_scrobbled_track_id: int | None = None
        self._scrobble_timer: asyncio.Task | None = None
        if settings.lastfm_enabled and settings.lastfm_session_key:
            self._start()

    def _start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._scrobble_timer and not self._scrobble_timer.done():
            self._scrobble_timer.cancel()
        self._scrobble_timer = None
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def shutdown(self) -> None:
        self.stop()
        await self._client.aclose()

    async def reconfigure(self, settings: Settings) -> None:
        self.stop()
        self._settings = settings
        self._current_track_id = None
        self._current_started_at = None
        self._last_scrobbled_track_id = None
        if settings.lastfm_enabled and settings.lastfm_session_key:
            self._start()

    async def _run(self) -> None:
        try:
            # Yield to let any in-progress state changes settle, then check
            # the current state so we don't miss notifications that fired
            # before this task had a chance to call condition.wait().
            await asyncio.sleep(0)
            state = self._now_playing.get_state()
            if state.status == "playing" and state.track_id is not None:
                await self._on_track_playing(state)

            async for update in self._now_playing.subscribe(timeout=30.0):
                if update is None:
                    continue
                if update.status == "playing" and update.track_id is not None:
                    await self._on_track_playing(update)
                elif self._current_track_id is not None:
                    self._on_track_stopped()
        except asyncio.CancelledError:
            pass

    async def _on_track_playing(self, state) -> None:
        if state.track_id == self._current_track_id:
            return

        # Cancel pending scrobble timer for previous track
        if self._scrobble_timer and not self._scrobble_timer.done():
            logger.info("Last.fm: cancelled scrobble timer (track changed)")
            self._scrobble_timer.cancel()
        self._scrobble_timer = None

        self._current_track_id = state.track_id
        self._current_started_at = time.time()
        self._current_duration = state.duration_s

        # Send updateNowPlaying
        params = {
            "artist": state.artist,
            "track": state.track,
            "album": state.album or "",
        }
        if state.duration_s:
            params["duration"] = str(int(state.duration_s))
        await self._call_lastfm("track.updateNowPlaying", params)

        # Start scrobble timer (unless already scrobbled this track)
        if state.track_id != self._last_scrobbled_track_id:
            delay = scrobble_delay(state.duration_s)
            logger.info("Last.fm: now playing %s - %s (scrobble in %.0fs)", state.artist, state.track, delay)
            self._scrobble_timer = asyncio.create_task(self._scrobble_after(delay))

    def _on_track_stopped(self) -> None:
        if self._scrobble_timer and not self._scrobble_timer.done():
            logger.info("Last.fm: cancelled scrobble timer (track stopped)")
            self._scrobble_timer.cancel()
        self._scrobble_timer = None
        self._current_track_id = None

    async def _scrobble_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._current_track_id is None:
                logger.info("Last.fm: scrobble timer fired but no current track")
                return
            if self._current_track_id == self._last_scrobbled_track_id:
                logger.info("Last.fm: scrobble timer fired but already scrobbled track %s", self._current_track_id)
                return
            state = self._now_playing.get_state()
            if state.status != "playing" or state.track_id != self._current_track_id:
                logger.info("Last.fm: scrobble timer fired but track no longer playing (status=%s, track_id=%s, expected=%s)",
                            state.status, state.track_id, self._current_track_id)
                return
            params = {
                "artist": state.artist,
                "track": state.track,
                "album": state.album or "",
                "timestamp": str(int(self._current_started_at)),
            }
            if state.duration_s:
                params["duration"] = str(int(state.duration_s))
            await self._call_lastfm("track.scrobble", params)
            self._last_scrobbled_track_id = self._current_track_id
            logger.info("Last.fm: scrobbled %s - %s", state.artist, state.track)
        except asyncio.CancelledError:
            pass

    async def _call_lastfm(self, method: str, params: dict[str, str]) -> None:
        if not self._settings.lastfm_session_key:
            return
        payload = {
            **params,
            "method": method,
            "api_key": self._api_key,
            "sk": self._settings.lastfm_session_key,
        }
        payload["api_sig"] = sign_params(payload, self._secret)
        payload["format"] = "json"
        try:
            resp = await self._client.post(LASTFM_API_URL, data=payload)
            if resp.status_code == 200:
                data = resp.json()
                if "error" in data:
                    logger.warning("Last.fm %s error: %s", method, data.get("message", data))
                else:
                    logger.debug("Last.fm %s: OK", method)
            else:
                logger.warning("Last.fm %s: HTTP %d", method, resp.status_code)
        except Exception as e:
            logger.warning("Last.fm %s: POST failed: %s", method, e)
