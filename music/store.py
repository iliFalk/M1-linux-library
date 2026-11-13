"""SQLite index for the music library.

The index lives at ``<config["state"]>/library.db`` and is the only persistent
record of what the library knows about.  ``scan`` owns the file tree side of
the reconciliation; this module owns the rows.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    spotify_id TEXT PRIMARY KEY,
    artist     TEXT NOT NULL,
    title      TEXT NOT NULL,
    album      TEXT NOT NULL,
    path       TEXT,
    status     TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    added_at   TEXT NOT NULL,
    seconds    REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_artist_album ON tracks(artist, album);
"""

_UNSET = object()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path(config_or_path: Any) -> Path:
    """Accept either the config dict or the database path itself."""
    if isinstance(config_or_path, Mapping):
        state = config_or_path.get("state")
        if not state:
            raise ValueError("config['state'] is required to locate library.db")
        return Path(state) / "library.db"
    return Path(config_or_path)


def connect(config_or_path: Any, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the index, creating the parent directory and schema when writable.

    ``readonly=True`` never creates a directory or a file; it raises
    ``FileNotFoundError`` when the index does not exist yet.  That is what
    makes dry runs safe.
    """
    path = _db_path(config_or_path)
    if readonly:
        if not path.is_file():
            raise FileNotFoundError(str(path))
        uri = "file:" + urllib.parse.quote(path.as_posix()) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def get_track(conn: sqlite3.Connection, spotify_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM tracks WHERE spotify_id = ?", (spotify_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def upsert_track(
    conn: sqlite3.Connection,
    track: Mapping[str, Any],
    status: str | None = None,
) -> dict[str, Any]:
    """Insert a track or refresh its metadata.

    ``status=None`` keeps the status of an existing row and uses ``pending``
    for a new one.  Rescanning a like must never undo a row's progress.
    """
    spotify_id = str(track["spotify_id"])
    now = _utcnow()
    artist = str(track.get("artist", ""))
    title = str(track.get("title", ""))
    album = str(track.get("album", ""))
    added_at = str(track.get("added_at") or now)
    existing = conn.execute(
        "SELECT spotify_id FROM tracks WHERE spotify_id = ?", (spotify_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO tracks "
            "(spotify_id, artist, title, album, path, status, attempts, error, "
            " added_at, seconds, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, 0, NULL, ?, 0, ?)",
            (spotify_id, artist, title, album, status or "pending", added_at, now),
        )
    elif status is None:
        conn.execute(
            "UPDATE tracks SET artist = ?, title = ?, album = ?, added_at = ?, "
            "updated_at = ? WHERE spotify_id = ?",
            (artist, title, album, added_at, now, spotify_id),
        )
    else:
        conn.execute(
            "UPDATE tracks SET artist = ?, title = ?, album = ?, added_at = ?, "
            "status = ?, updated_at = ? WHERE spotify_id = ?",
            (artist, title, album, added_at, status, now, spotify_id),
        )
    conn.commit()
    row = get_track(conn, spotify_id)
    assert row is not None
    return row


def set_result(
    conn: sqlite3.Connection,
    spotify_id: str,
    *,
    status: str | None = None,
    path: Any = _UNSET,
    error: Any = _UNSET,
    seconds: Any = _UNSET,
    bump_attempts: bool = False,
) -> bool:
    """Update one row.

    Arguments left unset keep their column value.  Passing ``path=None`` or
    ``error=None`` explicitly clears the column.  ``bump_attempts`` increments
    ``attempts`` by one without the caller having to read it first.
    """
    assignments: list[str] = []
    values: list[Any] = []
    if status is not None:
        assignments.append("status = ?")
        values.append(status)
    if path is not _UNSET:
        assignments.append("path = ?")
        values.append(None if path is None else str(path))
    if error is not _UNSET:
        assignments.append("error = ?")
        values.append(None if error is None else str(error))
    if seconds is not _UNSET:
        assignments.append("seconds = ?")
        values.append(float(seconds))
    if bump_attempts:
        assignments.append("attempts = attempts + 1")
    assignments.append("updated_at = ?")
    values.append(_utcnow())
    values.append(spotify_id)
    cursor = conn.execute(
        "UPDATE tracks SET " + ", ".join(assignments) + " WHERE spotify_id = ?",
        values,
    )
    conn.commit()
    return cursor.rowcount > 0


def list_tracks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM tracks ORDER BY artist, album, title, spotify_id"
    ).fetchall()
    return [dict(row) for row in rows]


def album_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per artist+album with the number of playable tracks and seconds."""
    rows = conn.execute(
        "SELECT artist, album, "
        "       CAST(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS INTEGER) AS tracks, "
        "       COALESCE(SUM(CASE WHEN status = 'ok' THEN seconds ELSE 0 END), 0.0) AS seconds "
        "FROM tracks GROUP BY artist, album ORDER BY artist, album"
    ).fetchall()
    return [dict(row) for row in rows]
