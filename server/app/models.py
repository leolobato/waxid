from __future__ import annotations
from typing import Literal
from pydantic import BaseModel


class AlbumCreate(BaseModel):
    artist: str
    name: str
    year: int | None = None
    discogs_url: str | None = None


class AlbumInfo(BaseModel):
    album_id: int
    artist: str
    name: str
    year: int | None
    discogs_url: str | None
    cover_path: str | None
    track_count: int


class TrackMetadata(BaseModel):
    album_id: int
    artist: str
    album: str
    track: str
    track_number: int | None = None
    year: int | None = None
    duration_s: float | None = None
    side: str | None = None
    position: str | None = None


class IngestResponse(BaseModel):
    track_id: int
    num_hashes: int
    duration_s: float | None


class MatchCandidate(BaseModel):
    track_id: int
    artist: str
    album: str
    album_id: int
    track: str
    track_number: int | None
    year: int | None
    side: str | None
    position: str | None
    score: int
    confidence: float | None
    offset_s: float
    duration_s: float | None
    discogs_url: str | None
    cover_url: str | None


class FinishedTrack(BaseModel):
    """A track that played through to (essentially) its end. Emitted once, on
    the frame reporting the transition away from it, so clients can fire a
    "track finished" notification without inferring completion from state."""
    track_id: int
    artist: str | None = None
    album: str | None = None
    album_id: int | None = None
    track: str | None = None
    track_number: int | None = None
    side: str | None = None
    position: str | None = None
    year: int | None = None
    tracks_on_side: int | None = None
    is_last_on_side: bool | None = None
    sides: dict[str, int] | None = None


class NowPlayingResponse(BaseModel):
    status: Literal["playing", "listening", "idle", "starting"]
    track_id: int | None = None
    artist: str | None = None
    album: str | None = None
    album_id: int | None = None
    track: str | None = None
    track_number: int | None = None
    side: str | None = None
    position: str | None = None
    year: int | None = None
    duration_s: float | None = None
    cover_url: str | None = None
    discogs_url: str | None = None
    elapsed_s: float | None = None
    started_at: float | None = None
    offset_s: float | None = None
    score: int | None = None
    confidence: float | None = None
    tracks_on_side: int | None = None
    is_last_on_side: bool | None = None
    sides: dict[str, int] | None = None
    finished_track: FinishedTrack | None = None


class MatchResponse(BaseModel):
    results: list[MatchCandidate]
    processing_time_ms: float


class TrackInfo(BaseModel):
    track_id: int
    album_id: int
    artist: str
    album: str
    track: str
    track_number: int | None
    year: int | None
    side: str | None
    position: str | None
    duration_s: float | None
    num_hashes: int


class AlbumDetail(BaseModel):
    album_id: int
    artist: str
    name: str
    year: int | None
    discogs_url: str | None
    cover_path: str | None
    tracks: list[TrackInfo]


class HealthResponse(BaseModel):
    status: str
    tracks_count: int
    hashes_count: int
    albums_count: int
    version: str | None = None


class AlbumUpdate(BaseModel):
    artist: str | None = None
    name: str | None = None
    year: int | None = None
    discogs_url: str | None = None


class TrackUpdate(BaseModel):
    track: str | None = None
    track_number: int | None = None
    side: str | None = None
    position: str | None = None
