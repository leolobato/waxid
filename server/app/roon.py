from __future__ import annotations

import asyncio
import logging
import re

import httpx

from .settings import Settings
from .state import NowPlayingService

logger = logging.getLogger(__name__)

KEEP_ALIVE_TIMEOUT = 30.0


def slugify(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


class RoonNotifier:
    def __init__(self, now_playing: NowPlayingService, settings: Settings):
        self._now_playing = now_playing
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=5.0)
        self._task: asyncio.Task | None = None
        self._last_status: str | None = None
        if settings.roon_enabled:
            self._start()

    def _start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def shutdown(self) -> None:
        self.stop()
        await self._client.aclose()

    async def reconfigure(self, settings: Settings) -> None:
        self.stop()
        self._settings = settings
        self._last_status = None
        if settings.roon_enabled:
            self._start()

    async def _run(self) -> None:
        try:
            # Yield to let any in-progress state changes settle, then check
            # the current state so we don't miss notifications that fired
            # before this task had a chance to call condition.wait().
            await asyncio.sleep(0)
            state = self._now_playing.get_state()
            if state.status == "playing" and state.album_id is not None:
                await self._post_playing(state)
                self._last_status = "playing"

            async for update in self._now_playing.subscribe(timeout=KEEP_ALIVE_TIMEOUT):
                if update is None:
                    # Timeout — send keep-alive if playing
                    state = self._now_playing.get_state()
                    if state.status == "playing" and state.album_id is not None:
                        await self._post_playing(state)
                    continue

                if update.status == "playing" and update.album_id is not None:
                    await self._post_playing(update)
                    self._last_status = "playing"
                elif self._last_status == "playing":
                    await self._post_now_playing("stopped", {})
                    self._last_status = "stopped"
        except asyncio.CancelledError:
            if self._last_status == "playing":
                try:
                    await self._post_now_playing("stopped", {})
                except Exception:
                    pass

    async def _post_playing(self, state) -> None:
        artwork_url = None
        if state.album_id and self._settings.server_url:
            artwork_url = f"{self._settings.server_url}/albums/{state.album_id}/cover"

        payload = {
            "title": state.track,
            "artist": state.artist,
            "album": state.album,
            "artwork_url": artwork_url,
            "seek_position": state.elapsed_s,
            "duration_seconds": state.duration_s,
        }
        await self._post_now_playing("playing", payload)

    async def _post_now_playing(self, roon_state: str, payload: dict) -> None:
        if not self._settings.roon_url:
            return

        zone_id = slugify(self._settings.roon_zone_name or "record-player")
        url = f"{self._settings.roon_url}/api/sources/{zone_id}/now-playing"

        body = {
            "zone_name": self._settings.roon_zone_name,
            "state": roon_state,
            **payload,
        }

        try:
            resp = await self._client.post(url, json=body)
            if resp.status_code == 200:
                logger.info("Roon: %s posted", roon_state)
            else:
                logger.warning("Roon: HTTP %d", resp.status_code)
        except Exception as e:
            logger.warning("Roon: POST failed: %s", e)
