from __future__ import annotations
import sqlite3
from collections import defaultdict

import numpy as np


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA mmap_size=3221225472")
        self.conn.execute("PRAGMA cache_size=-256000")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS albums (
                album_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                artist       TEXT NOT NULL,
                name         TEXT NOT NULL,
                year         INTEGER,
                discogs_url  TEXT,
                cover_path   TEXT,
                created_at   TEXT DEFAULT (datetime('now')),
                UNIQUE(artist, name)
            );
            CREATE TABLE IF NOT EXISTS tracks (
                track_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id     INTEGER NOT NULL REFERENCES albums(album_id) ON DELETE CASCADE,
                artist       TEXT NOT NULL,
                album        TEXT NOT NULL,
                track        TEXT NOT NULL,
                track_number INTEGER,
                side         TEXT,
                position     TEXT,
                year         INTEGER,
                duration_s   REAL,
                source_path  TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS hashes (
                hash     INTEGER NOT NULL,
                track_id INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
                t_frame  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hash_cover ON hashes(hash, track_id, t_frame);
            CREATE INDEX IF NOT EXISTS idx_hash_track ON hashes(track_id);
            DROP INDEX IF EXISTS idx_hash;
        """)
        self.conn.commit()

    def insert_album(self, artist: str, name: str, year: int | None = None,
                     discogs_url: str | None = None) -> tuple[int, bool]:
        """Insert or find an album. Returns (album_id, created).
        If the album already exists, updates year/discogs_url when they were NULL."""
        row = self.conn.execute(
            "SELECT album_id FROM albums WHERE artist = ? AND name = ?",
            (artist, name),
        ).fetchone()
        if row:
            if year is not None or discogs_url is not None:
                self.conn.execute(
                    "UPDATE albums SET "
                    "year = COALESCE(year, ?), "
                    "discogs_url = COALESCE(discogs_url, ?) "
                    "WHERE album_id = ?",
                    (year, discogs_url, row[0]),
                )
                self.conn.commit()
            return row[0], False
        cur = self.conn.execute(
            "INSERT INTO albums (artist, name, year, discogs_url) VALUES (?, ?, ?, ?)",
            (artist, name, year, discogs_url),
        )
        self.conn.commit()
        return cur.lastrowid, True

    def get_album(self, album_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM albums WHERE album_id = ?", (album_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_albums(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT a.*, COUNT(t.track_id) as track_count "
            "FROM albums a LEFT JOIN tracks t ON a.album_id = t.album_id "
            "GROUP BY a.album_id ORDER BY a.album_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_album(self, album_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM albums WHERE album_id = ?", (album_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def update_album_cover(self, album_id: int, cover_path: str):
        self.conn.execute(
            "UPDATE albums SET cover_path = ? WHERE album_id = ?",
            (cover_path, album_id),
        )
        self.conn.commit()

    def update_album_discogs(self, album_id: int, discogs_url: str):
        self.conn.execute(
            "UPDATE albums SET discogs_url = ? WHERE album_id = ?",
            (discogs_url, album_id),
        )
        self.conn.commit()

    def update_album(self, album_id: int, **fields) -> dict | None:
        album = self.get_album(album_id)
        if album is None:
            return None
        updates = {k: v for k, v in fields.items() if v is not None}
        if not updates:
            return album
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [album_id]
        self.conn.execute(
            f"UPDATE albums SET {set_clause} WHERE album_id = ?", values
        )
        self.conn.commit()
        return self.get_album(album_id)

    def get_track_with_album(self, track_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT t.*, a.discogs_url, a.cover_path, a.album_id as a_album_id "
            "FROM tracks t JOIN albums a ON t.album_id = a.album_id "
            "WHERE t.track_id = ?", (track_id,)
        ).fetchone()
        return dict(row) if row else None

    def insert_track(self, album_id: int, artist: str, album: str, track: str,
                     track_number: int | None = None, year: int | None = None,
                     duration_s: float | None = None, source_path: str | None = None,
                     side: str | None = None, position: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO tracks (album_id, artist, album, track, track_number, year, "
            "duration_s, source_path, side, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (album_id, artist, album, track, track_number, year, duration_s,
             source_path, side, position),
        )
        self.conn.commit()
        return cur.lastrowid

    def insert_hashes(self, hashes: list[tuple[int, int, int]]):
        self.conn.executemany(
            "INSERT INTO hashes (hash, track_id, t_frame) VALUES (?, ?, ?)", hashes,
        )
        self.conn.commit()

    def lookup_hashes(self, hash_values: list[int], batch_size: int = 500) -> dict[int, list[tuple[int, int]]]:
        if not hash_values:
            return {}
        result: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for i in range(0, len(hash_values), batch_size):
            batch = hash_values[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self.conn.execute(
                f"SELECT hash, track_id, t_frame FROM hashes WHERE hash IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                result[row[0]].append((row[1], row[2]))
        return dict(result)

    def lookup_hashes_flat(self, hash_values: list[int], batch_size: int = 8000) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return DB matches as flat numpy arrays (hashes, track_ids, t_frames)."""
        if not hash_values:
            empty = np.empty(0, dtype=np.int64)
            return empty, empty, empty
        # Use a dedicated cursor to avoid Row factory overhead
        cur = self.conn.cursor()
        cur.row_factory = None
        all_rows = []
        try:
            for i in range(0, len(hash_values), batch_size):
                batch = hash_values[i:i + batch_size]
                placeholders = ",".join("?" for _ in batch)
                rows = cur.execute(
                    f"SELECT hash, track_id, t_frame FROM hashes WHERE hash IN ({placeholders})",
                    batch,
                ).fetchall()
                all_rows.extend(rows)
        finally:
            cur.close()
        if not all_rows:
            empty = np.empty(0, dtype=np.int64)
            return empty, empty, empty
        arr = np.array(all_rows, dtype=np.int64)
        return arr[:, 0], arr[:, 1], arr[:, 2]

    def get_tracks_for_album(self, album_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT t.*, COUNT(h.hash) as num_hashes "
            "FROM tracks t LEFT JOIN hashes h ON t.track_id = h.track_id "
            "WHERE t.album_id = ? "
            "GROUP BY t.track_id ORDER BY t.track_number, t.track_id",
            (album_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_tracks(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT t.*, COUNT(h.hash) as num_hashes "
            "FROM tracks t LEFT JOIN hashes h ON t.track_id = h.track_id "
            "GROUP BY t.track_id ORDER BY t.album_id, t.track_number, t.track_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_track(self, track_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM tracks WHERE track_id = ?", (track_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_track(self, track_id: int, **fields) -> dict | None:
        track = self.get_track(track_id)
        if track is None:
            return None
        updates = {k: v for k, v in fields.items() if v is not None}
        if not updates:
            return track
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [track_id]
        self.conn.execute(
            f"UPDATE tracks SET {set_clause} WHERE track_id = ?", values
        )
        self.conn.commit()
        return self.get_track(track_id)

    def delete_track(self, track_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM tracks WHERE track_id = ?", (track_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def get_health(self) -> dict:
        tracks = self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        hashes = self.conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]
        albums = self.conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        return {"status": "ok", "tracks_count": tracks, "hashes_count": hashes, "albums_count": albums}

    def close(self):
        self.conn.close()
