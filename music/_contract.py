"""Cross-slice contract for the self-hosted music pipeline.

This module is declaration only: every body raises ``NotImplementedError``.
The three slices (ingest/sync, resolver/tagging, index/report) implement the
names below in their own modules; this file is the single place where the
shared names, their signatures, the config/env vocabulary and the key order of
every returned dict are written down.

Nothing here may be changed by a slice.  See ``spec/design.md`` for the data
model and the reasoning behind these shapes.
"""

import sqlite3
from pathlib import Path

__all__ = [
    "LIKES_KEYS",
    "TRACK_COLUMNS",
    "CONFIG_KEYS",
    "ENV_VARS",
    "SYNC_STEPS",
    "STATUS_PENDING",
    "STATUS_OK",
    "STATUS_FAILED",
    "STATUS_PARKED",
    "MAX_ATTEMPTS",
    "CONCURRENCY_VAR",
    "DEFAULT_WORKERS",
    "MAX_WORKERS",
    "INGEST_RESULT_KEYS",
    "TRACK_RESULT_KEYS",
    "SCAN_RESULT_KEYS",
    "SYNC_SUMMARY_KEYS",
    "REPORT_ROW_KEYS",
    "STATUS_ROW_KEYS",
    "FAULT_ROW_KEYS",
    "PLAYLIST_KEYS",
    "load_config",
    "find_spotify_cli",
    "parse_spotify_cli",
    "parse_export",
    "ingest",
    "resolver_argv",
    "resolve_track",
    "resolve_pending",
    "convert_to_m4a",
    "probe_duration",
    "write_tags",
    "ensure_cover",
    "FakeResolver",
    "open_index",
    "init_index",
    "upsert_track",
    "get_track",
    "pending_tracks",
    "record_result",
    "scan",
    "report_rows",
    "status_rows",
    "fault_rows",
    "workers",
    "sync",
    "main",
]

# --- vocabulary -----------------------------------------------------------

#: The exact keys of one liked-track entry, in this order, in
#: ``<state_dir>/likes.json`` and in the Spotify source (AC1, AC3).
LIKES_KEYS = ("spotify_id", "artist", "title", "album", "added_at")

#: The sqlite columns of table ``tracks``, in this order (Surface).
TRACK_COLUMNS = (
    "spotify_id",
    "artist",
    "title",
    "album",
    "path",
    "status",
    "attempts",
    "error",
    "added_at",
)

#: The keys of the config object, in this order (Surface, AC24).
CONFIG_KEYS = ("library_root", "state_dir", "resolver", "spotify_export")

#: The environment variable that names each config key (AC24).
ENV_VARS = {
    "library_root": "MUSIC_LIBRARY_ROOT",
    "state_dir": "MUSIC_STATE_DIR",
    "resolver": "MUSIC_RESOLVER",
    "spotify_export": "MUSIC_SPOTIFY_EXPORT",
}

#: The keys of the ``steps`` dict of ``sync()``, in run order (AC25).
SYNC_STEPS = ("ingest", "resolve", "scan")

#: Values of ``tracks.status`` (Surface, AC21-AC23).
STATUS_PENDING = "pending"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_PARKED = "parked"

#: Attempts after which a failed track becomes parked (AC23).
MAX_ATTEMPTS = 3

#: The environment variable that limits parallel resolver calls (AC42).
CONCURRENCY_VAR = "MUSIC_CONCURRENCY"

#: Resolver calls that run at once when the variable is unset, and the cap the
#: variable can never pass (AC42).
DEFAULT_WORKERS = 3
MAX_WORKERS = 3

# --- result shapes --------------------------------------------------------

#: Keys of the dict returned by ``ingest()``: total liked entries seen,
#: liking entries added by this run, and the full likes list.
INGEST_RESULT_KEYS = ("total", "added", "likes")

#: Keys of one resolver result, from any resolver and from ``resolve_track``.
TRACK_RESULT_KEYS = (
    "spotify_id",
    "ok",
    "path",
    "error",
    "source",
    "duration",
)

#: Keys of the dict returned by ``scan()``.
SCAN_RESULT_KEYS = ("indexed", "missing")

#: Keys of the dict returned by ``sync()`` (AC12).
SYNC_SUMMARY_KEYS = ("added", "missing", "failed")

#: Keys of one album row returned by ``report_rows()`` (AC16, AC17).
REPORT_ROW_KEYS = ("artist", "album", "tracks", "seconds")

#: Keys of one count row returned by ``status_rows()`` (AC43).
STATUS_ROW_KEYS = ("status", "tracks")

#: Keys of one row returned by ``fault_rows()`` (AC43).
FAULT_ROW_KEYS = ("artist", "title", "album", "status", "attempts", "error")

#: Keys of one row returned by ``playlists.build()`` (AC44).
PLAYLIST_KEYS = ("name", "tracks", "missing", "path")


# --- config and Spotify ---------------------------------------------------


def load_config(
    path: str | None = None, env: dict[str, str] | None = None
) -> dict:
    raise NotImplementedError


def find_spotify_cli() -> str | None:
    raise NotImplementedError


def parse_spotify_cli(stdout: str) -> list[dict]:
    raise NotImplementedError


def parse_export(path: Path) -> list[dict]:
    raise NotImplementedError


# --- ingest ---------------------------------------------------------------


def ingest(config: dict) -> dict:
    raise NotImplementedError


# --- resolver -------------------------------------------------------------


def resolver_argv(config: dict) -> list[str]:
    raise NotImplementedError


def resolve_track(config: dict, track: dict) -> dict:
    raise NotImplementedError


def resolve_pending(config: dict, tracks: list[dict]) -> list[dict]:
    raise NotImplementedError


def workers(env: dict | None = None) -> int:
    raise NotImplementedError


def convert_to_m4a(config: dict, src: Path, dst: Path) -> None:
    raise NotImplementedError


def probe_duration(path: Path) -> float:
    raise NotImplementedError


def write_tags(path: Path, tags: dict[str, str]) -> None:
    raise NotImplementedError


def ensure_cover(album_dir: Path, cover_src: str | None = None) -> Path:
    raise NotImplementedError


class FakeResolver:
    def __init__(
        self, config: dict, sources: dict[str, Path] | None = None
    ) -> None:
        raise NotImplementedError

    def resolve(self, artist: str, title: str, album: str) -> dict:
        raise NotImplementedError


# --- index ----------------------------------------------------------------


def open_index(config: dict) -> sqlite3.Connection:
    raise NotImplementedError


def init_index(conn: sqlite3.Connection) -> None:
    raise NotImplementedError


def upsert_track(conn: sqlite3.Connection, track: dict) -> None:
    raise NotImplementedError


def get_track(conn: sqlite3.Connection, spotify_id: str) -> dict | None:
    raise NotImplementedError


def pending_tracks(
    conn: sqlite3.Connection, likes: list[dict]
) -> list[dict]:
    raise NotImplementedError


def record_result(conn: sqlite3.Connection, result: dict) -> None:
    raise NotImplementedError


def scan(config: dict, conn: sqlite3.Connection) -> dict:
    raise NotImplementedError


def report_rows(conn: sqlite3.Connection) -> list[dict]:
    raise NotImplementedError


def status_rows(conn: sqlite3.Connection) -> list[dict]:
    raise NotImplementedError


def fault_rows(conn: sqlite3.Connection) -> list[dict]:
    raise NotImplementedError


# --- orchestration and CLI ------------------------------------------------


def sync(
    config: dict, dry_run: bool = False, steps: dict | None = None
) -> dict:
    raise NotImplementedError


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    raise NotImplementedError
