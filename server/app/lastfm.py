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
        # A play session survives brief drops to "listening". We bank the
        # actual seconds played across segments and scrobble once that crosses
        # the threshold — whether mid-play (timer) or at the moment it stops.
        self._session_track_id: int | None = None
        self._session_state = None              # last "playing" snapshot, for scrobble-on-stop
        self._session_started_at: float | None = None  # first play -> scrobble timestamp
        self._session_duration: float | None = None
        self._played_s: float = 0.0             # banked seconds from finished segments
        self._segment_start: float | None = None  # start of the open segment, None while paused
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
        self._reset_session()
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
                elif self._session_track_id is not None:
                    await self._on_track_stopped()
        except asyncio.CancelledError:
            pass

    # --- session bookkeeping -------------------------------------------------

    def _played_total(self) -> float:
        """Active seconds played this session, including the open segment."""
        total = self._played_s
        if self._segment_start is not None:
            total += time.time() - self._segment_start
        return total

    def _threshold(self) -> float:
        return scrobble_delay(self._session_duration)

    def _cancel_timer(self) -> None:
        if self._scrobble_timer and not self._scrobble_timer.done():
            self._scrobble_timer.cancel()
        self._scrobble_timer = None

    def _arm_timer(self) -> None:
        """(Re)schedule a mid-play wakeup for the time still owed."""
        if self._session_track_id == self._last_scrobbled_track_id:
            return
        self._cancel_timer()
        remaining = max(0.0, self._threshold() - self._played_total())
        self._scrobble_timer = asyncio.create_task(self._scrobble_after(remaining))

    def _reset_session(self) -> None:
        self._cancel_timer()
        self._session_track_id = None
        self._session_state = None
        self._session_started_at = None
        self._session_duration = None
        self._played_s = 0.0
        self._segment_start = None

    def _bank_segment(self) -> None:
        if self._segment_start is not None:
            self._played_s += time.time() - self._segment_start
            self._segment_start = None

    @staticmethod
    def _now_playing_params(state) -> dict[str, str]:
        params = {
            "artist": state.artist,
            "track": state.track,
            "album": state.album or "",
        }
        if state.duration_s:
            params["duration"] = str(int(state.duration_s))
        return params

    # --- event handlers ------------------------------------------------------

    async def _on_track_playing(self, state) -> None:
        # Same track resuming or continuing.
        if state.track_id == self._session_track_id:
            self._session_state = state
            if self._segment_start is None:
                # Resuming after a brief drop: reopen a segment, keep banked time.
                self._segment_start = time.time()
                await self._call_lastfm("track.updateNowPlaying", self._now_playing_params(state))
                if state.track_id != self._last_scrobbled_track_id:
                    logger.info("Last.fm: resumed %s - %s (%.0fs of %.0fs played)",
                                state.artist, state.track, self._played_total(), self._threshold())
                    self._arm_timer()
            return

        # A different track: close out the previous session, scrobbling it if
        # it already earned one, then begin a fresh session.
        await self._finalize_session()
        self._session_track_id = state.track_id
        self._session_state = state
        self._session_started_at = time.time()
        self._session_duration = state.duration_s
        self._played_s = 0.0
        self._segment_start = time.time()

        await self._call_lastfm("track.updateNowPlaying", self._now_playing_params(state))
        if state.track_id != self._last_scrobbled_track_id:
            logger.info("Last.fm: now playing %s - %s (scrobble after %.0fs played)",
                        state.artist, state.track, self._threshold())
            self._arm_timer()

    async def _on_track_stopped(self) -> None:
        """The track left "playing". Bank the segment and scrobble if it has
        already played long enough; otherwise keep the session paused so a
        resume picks up where it left off."""
        self._bank_segment()
        self._cancel_timer()
        if (self._session_track_id is not None
                and self._session_track_id != self._last_scrobbled_track_id
                and self._played_total() >= self._threshold()):
            logger.info("Last.fm: track stopped past threshold (%.0fs played)", self._played_total())
            await self._do_scrobble()

    async def _finalize_session(self) -> None:
        """Close the current session (on track change), scrobbling if owed."""
        self._bank_segment()
        self._cancel_timer()
        if (self._session_track_id is not None
                and self._session_track_id != self._last_scrobbled_track_id
                and self._played_total() >= self._threshold()):
            await self._do_scrobble()
        self._reset_session()

    async def _scrobble_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._session_track_id is None:
                return
            if self._session_track_id == self._last_scrobbled_track_id:
                return
            if self._played_total() < self._threshold():
                # Pauses pushed actual play behind the wall clock; wait the gap.
                self._arm_timer()
                return
            state = self._now_playing.get_state()
            if state.status != "playing" or state.track_id != self._session_track_id:
                # Not playing right now; the stop handler scrobbles if it's owed.
                return
            await self._do_scrobble()
        except asyncio.CancelledError:
            pass

    async def _do_scrobble(self) -> None:
        st = self._session_state
        if st is None or self._session_started_at is None:
            return
        if self._session_track_id == self._last_scrobbled_track_id:
            return
        params = {
            "artist": st.artist,
            "track": st.track,
            "album": st.album or "",
            "timestamp": str(int(self._session_started_at)),
        }
        if st.duration_s:
            params["duration"] = str(int(st.duration_s))
        await self._call_lastfm("track.scrobble", params)
        self._last_scrobbled_track_id = self._session_track_id
        logger.info("Last.fm: scrobbled %s - %s", st.artist, st.track)

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
