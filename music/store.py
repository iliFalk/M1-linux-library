"""sqlite index for the music pipeline.

The index is ``<state_dir>/library.db``.  One row per ``spotify_id`` in table
``tracks`` (columns fixed by ``music._contract.TRACK_COLUMNS``) plus the
per-track duration the pipeline measured, kept in its own table so the
``tracks`` surface stays exactly the nine declared columns.
"""

import os
import sqlite3
import stat
from pathlib import Path

from music import _contract as contract

DB_NAME = "library.db"

_TRACK_COLUMNS = ", ".join(contract.TRACK_COLUMNS)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS tracks (
    spotify_id TEXT PRIMARY KEY,
    artist TEXT,
    title TEXT,
    album TEXT,
    path TEXT,
    status TEXT NOT NULL DEFAULT '{contract.STATUS_PENDING}',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    added_at TEXT
)
"""

_DURATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS track_durations (
    spotify_id TEXT PRIMARY KEY,
    seconds REAL NOT NULL DEFAULT 0
)
"""

_METADATA_COLUMNS = ("artist", "title", "album", "added_at")
_LIFECYCLE_COLUMNS = ("path", "status", "attempts", "error")


# --- opening --------------------------------------------------------------


def index_path(config: dict) -> Path:
    """Path of the index file for ``config`` (no side effects)."""
    return Path(config["state_dir"]) / DB_NAME


def open_index(config: dict) -> sqlite3.Connection:
    """Open (and create) the index file, with the schema in place."""
    db = index_path(config)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    init_index(conn)
    return conn


def open_index_readonly(config: dict) -> sqlite3.Connection | None:
    """Open an existing index for reading; ``None`` when it does not exist."""
    db = index_path(config)
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def in_memory_index(config: dict) -> sqlite3.Connection:
    """A private in-memory copy of the index, for dry runs.

    The file itself is opened read-only (or not at all when absent), so a dry
    run can mutate rows without touching ``state/library.db`` (AC14).
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = index_path(config)
    if db.exists():
        source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            source.backup(conn)
        finally:
            source.close()
    init_index(conn)
    return conn


def init_index(conn: sqlite3.Connection) -> None:
    """Create the schema when it is missing.  Safe to call repeatedly."""
    conn.execute(_SCHEMA)
    conn.execute(_DURATIONS_SCHEMA)
    conn.commit()


# --- rows -----------------------------------------------------------------


def get_track(conn: sqlite3.Connection, spotify_id: str) -> dict | None:
    """One row keyed by ``TRACK_COLUMNS``, or ``None``."""
    cursor = conn.execute(
        f"SELECT {_TRACK_COLUMNS} FROM tracks WHERE spotify_id = ?",
        (spotify_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        column: row[index]
        for index, column in enumerate(contract.TRACK_COLUMNS)
    }


def upsert_track(conn: sqlite3.Connection, track: dict) -> None:
    """Insert a new row, or refresh an existing one without losing progress.

    A like entry carries identity and metadata only, so an existing row keeps
    its ``path``/``status``/``attempts``/``error`` — a second sync can never
    reset an ``ok`` row and overwrite its file (AC20).
    """
    spotify_id = track["spotify_id"]
    existing = get_track(conn, spotify_id)
    if existing is None:
        values = {
            column: track.get(column) for column in contract.TRACK_COLUMNS
        }
        values["status"] = values["status"] or contract.STATUS_PENDING
        values["attempts"] = int(values["attempts"] or 0)
        placeholders = ", ".join("?" for _ in contract.TRACK_COLUMNS)
        conn.execute(
            f"INSERT INTO tracks ({_TRACK_COLUMNS}) VALUES ({placeholders})",
            tuple(values[column] for column in contract.TRACK_COLUMNS),
        )
    else:
        updates = {
            column: track[column]
            for column in _METADATA_COLUMNS + _LIFECYCLE_COLUMNS
            if column in track
        }
        if updates:
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE tracks SET {assignments} WHERE spotify_id = ?",
                (*updates.values(), spotify_id),
            )
    duration = track.get("duration", track.get("seconds"))
    if duration is not None:
        record_duration(conn, spotify_id, duration)
    conn.commit()


def record_duration(
    conn: sqlite3.Connection, spotify_id: str, seconds: float
) -> None:
    """Store the duration ``ffprobe`` reported for a placed track (AC10/AC17)."""
    conn.execute(
        "INSERT INTO track_durations (spotify_id, seconds) VALUES (?, ?) "
        "ON CONFLICT(spotify_id) DO UPDATE SET seconds = excluded.seconds",
        (spotify_id, float(seconds)),
    )
    conn.commit()


def pending_tracks(
    conn: sqlite3.Connection, likes: list[dict]
) -> list[dict]:
    """The like entries the resolver still has to see, in ``likes`` order.

    Absent from the index, ``pending``, or ``failed`` with fewer than
    ``MAX_ATTEMPTS`` attempts.  ``ok`` and ``parked`` never come back (AC20,
    AC23); identity is ``spotify_id``, never the path.
    """
    pending: list[dict] = []
    seen: set[str] = set()
    for entry in likes:
        spotify_id = entry.get("spotify_id")
        if not spotify_id or spotify_id in seen:
            continue
        seen.add(spotify_id)
        row = get_track(conn, spotify_id)
        if row is None:
            pending.append(
                {column: entry.get(column) for column in contract.LIKES_KEYS}
            )
            continue
        if row["status"] in (contract.STATUS_OK, contract.STATUS_PARKED):
            continue
        if int(row["attempts"] or 0) >= contract.MAX_ATTEMPTS:
            continue
        pending.append(
            {column: entry.get(column) for column in contract.LIKES_KEYS}
        )
    return pending


def record_result(conn: sqlite3.Connection, result: dict) -> None:
    """Apply one ``TRACK_RESULT_KEYS`` result to the index.

    Success writes the placed path and clears the error.  Failure increments
    ``attempts``, keeps the resolver's stderr text in ``error`` and parks the
    track at ``MAX_ATTEMPTS`` (AC21-AC23).  Attempt counting happens here and
    nowhere else.
    """
    spotify_id = result["spotify_id"]
    existing = get_track(conn, spotify_id)
    attempts = int(existing["attempts"] or 0) if existing else 0
    if result.get("ok"):
        status = contract.STATUS_OK
        path = None if result.get("path") is None else str(result["path"])
        error = None
    else:
        attempts += 1
        status = (
            contract.STATUS_PARKED
            if attempts >= contract.MAX_ATTEMPTS
            else contract.STATUS_FAILED
        )
        path = None
        error = result.get("error")
        if error is None:
            error = ""
    if existing is None:
        conn.execute(
            f"INSERT INTO tracks ({_TRACK_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (spotify_id, None, None, None, path, status, attempts, error, None),
        )
    else:
        conn.execute(
            "UPDATE tracks SET path = ?, status = ?, attempts = ?, error = ? "
            "WHERE spotify_id = ?",
            (path, status, attempts, error, spotify_id),
        )
    conn.commit()
    if result.get("ok") and result.get("duration") is not None:
        # The resolver measured the written file; report sums this (AC10/AC17).
        record_duration(conn, spotify_id, float(result["duration"]))


# --- reconciliation and report --------------------------------------------


def _status_order() -> tuple[str, ...]:
    """The statuses in the order the report prints them (AC43)."""
    return (
        contract.STATUS_PENDING,
        contract.STATUS_OK,
        contract.STATUS_FAILED,
        contract.STATUS_PARKED,
    )


def status_rows(conn: sqlite3.Connection) -> list[dict]:
    """One ``STATUS_ROW_KEYS`` dict per status, in contract order (AC43).

    A status the index does not hold yet is reported with zero, so the shape of
    the table does not change between runs.
    """
    counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM tracks GROUP BY status"
        )
    }
    order = _status_order()
    rows = [{"status": name, "tracks": counts.get(name, 0)} for name in order]
    rows.extend(
        {"status": name, "tracks": counts[name]}
        for name in sorted(set(counts) - set(order))
    )
    return rows


def fault_rows(conn: sqlite3.Connection) -> list[dict]:
    """One ``FAULT_ROW_KEYS`` dict per failed or parked track (AC43).

    ``parked`` comes last: those tracks are never retried, the ``failed`` ones
    are retried by the next run.
    """
    cursor = conn.execute(
        f"SELECT {', '.join(contract.FAULT_ROW_KEYS)} FROM tracks "
        "WHERE status IN (?, ?) "
        "ORDER BY status, artist, album, title",
        (contract.STATUS_FAILED, contract.STATUS_PARKED),
    )
    rows = []
    for row in cursor:
        entry = {key: row[key] for key in contract.FAULT_ROW_KEYS}
        entry["attempts"] = int(entry["attempts"] or 0)
        for key in ("artist", "title", "album", "error"):
            entry[key] = "" if entry[key] is None else str(entry[key])
        rows.append(entry)
    return rows


def path_is_placed(path: str | None) -> bool:
    """True when ``path`` is a regular file, judged without following a link.

    ``Path.exists()`` follows a symlink and is ``False`` for a dangling one, so
    a link could be counted as a placed track and a dangling link as a missing
    one.  Placement itself is what keeps the tree inside ``library_root``
    (``music.convert.reserve``, AC34/AC35); here the read-back only has to tell
    a real file from a link.  No ``library_root`` anchor: a dry run reads a
    private copy of the index whose ``ok`` rows still point at the real tree.
    """
    if not path:
        return False
    try:
        return stat.S_ISREG(os.lstat(str(path)).st_mode)
    except OSError:
        return False


def scan(config: dict, conn: sqlite3.Connection) -> dict:
    """Reconcile the index with the library tree.

    A row whose file is gone keeps its row (AC18) and loses only its ``path``;
    running the scan again on an unchanged tree writes nothing (AC19), and a
    stored path that is a symlink or not a file counts as gone rather than as a
    placed track (AC34, AC35).
    """
    indexed = 0
    missing = 0
    changed = False
    rows = conn.execute(
        "SELECT spotify_id, path, status FROM tracks"
    ).fetchall()
    for spotify_id, path, status in rows:
        if path_is_placed(path):
            indexed += 1
            continue
        if status != contract.STATUS_OK:
            continue
        missing += 1
        if path is not None:
            conn.execute(
                "UPDATE tracks SET path = NULL WHERE spotify_id = ?",
                (spotify_id,),
            )
            changed = True
    if changed:
        conn.commit()
    return {"indexed": indexed, "missing": missing}


def _track_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute("PRAGMA table_info(tracks)").fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {row[1] for row in rows}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def report_rows(conn: sqlite3.Connection) -> list[dict]:
    """One ``REPORT_ROW_KEYS`` dict per album, ordered by artist then album.

    Everything is read from the index.  ``seconds`` is a whole number summed
    from the per-track duration the pipeline recorded (AC16, AC17); a per-track
    ``duration`` column is honoured too when an index carries one.
    """
    columns = _track_columns(conn)
    if "duration" in columns:
        seconds_sql = "COALESCE(SUM(t.duration), 0)"
        join_sql = ""
    elif _has_table(conn, "track_durations"):
        seconds_sql = "COALESCE(SUM(d.seconds), 0)"
        join_sql = (
            " LEFT JOIN track_durations AS d ON d.spotify_id = t.spotify_id"
        )
    else:
        seconds_sql = "0"
        join_sql = ""
    cursor = conn.execute(
        f"SELECT t.artist, t.album, COUNT(*), {seconds_sql} "
        f"FROM tracks AS t{join_sql} "
        "WHERE t.status = ? "
        "GROUP BY t.artist, t.album "
        "ORDER BY t.artist, t.album",
        (contract.STATUS_OK,),
    )
    rows: list[dict] = []
    for artist, album, tracks, seconds in cursor.fetchall():
        rows.append(
            {
                "artist": artist,
                "album": album,
                "tracks": int(tracks),
                "seconds": int(float(seconds or 0) + 0.5),
            }
        )
    return rows
