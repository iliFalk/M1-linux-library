"""A TuneMyMusic library export as the pipeline's two input files.

The export is one CSV: a row per track, with ``Type`` naming what the row is
part of — ``Favorite`` for the liked songs, ``Playlist`` for one entry of one
playlist, ``Album`` and ``Artist`` for the saved albums and artists. Every row
carries the Spotify id and the ISRC, so the identity is exact and no fuzzy
matching is needed.

``convert`` splits the rows into ``likes.json`` (the liked tracks, one entry
each) and ``playlists.json`` (one object per playlist, tracks in file order).
The export has no like date, so the entry takes a synthetic ``added_at`` that
follows the row order: the album track numbers stay what the file decided.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: The columns the export must carry; a missing one is an error, not a guess.
COLUMNS = (
    "Track name",
    "Artist name",
    "Album",
    "Playlist name",
    "Type",
    "ISRC",
    "Spotify - id",
)

#: ``Type`` values that become a like entry and a playlist entry (AC44).
TYPE_FAVORITE = "Favorite"
TYPE_PLAYLIST = "Playlist"

#: ``Type`` values the converter counts and drops: saved albums and artists are
#: not tracks.
SKIPPED_TYPES = ("Album", "Artist")

#: Start of the synthetic ``added_at`` sequence; the row order fixes the rest.
BASE_DATE = datetime(2001, 1, 1, tzinfo=timezone.utc)

#: File names the pipeline reads (AC1, AC44).
LIKES_NAME = "likes.json"
PLAYLISTS_NAME = "playlists.json"


def _stamp(index: int) -> str:
    """A deterministic ISO time for row ``index``: one second per row."""
    return (BASE_DATE + timedelta(seconds=index)).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_rows(path: Path | str) -> list[dict]:
    """The CSV as dicts; a missing column raises instead of yielding blanks."""
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in COLUMNS if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} misses the column(s) {', '.join(missing)}")
        return [row for row in reader]


def _entry(row: dict, index: int) -> dict:
    """One like entry in the pipeline's five-key shape (AC3)."""
    return {
        "spotify_id": row["Spotify - id"].strip(),
        "artist": row["Artist name"].strip(),
        "title": row["Track name"].strip(),
        "album": row["Album"].strip(),
        "added_at": _stamp(index),
    }


def convert(path: Path | str) -> tuple[list[dict], list[dict]]:
    """Return ``(likes, playlists)`` for the export at ``path``."""
    likes: list[dict] = []
    seen: set[str] = set()
    playlists: dict[str, list[dict]] = {}
    listed: dict[str, set[str]] = {}

    for index, row in enumerate(read_rows(path)):
        kind = (row.get("Type") or "").strip()
        spotify_id = (row.get("Spotify - id") or "").strip()
        if not spotify_id:
            continue
        entry = _entry(row, index)
        if kind == TYPE_FAVORITE:
            if spotify_id in seen:
                continue
            seen.add(spotify_id)
            likes.append(entry)
        elif kind == TYPE_PLAYLIST:
            name = (row.get("Playlist name") or "").strip()
            if not name:
                continue
            playlists.setdefault(name, [])
            listed.setdefault(name, set())
            if spotify_id in listed[name]:
                continue
            listed[name].add(spotify_id)
            playlists[name].append(entry)

    return likes, [{"name": name, "tracks": tracks} for name, tracks in playlists.items()]


def write(outdir: Path | str, likes: list[dict], playlists: list[dict]) -> tuple[Path, Path]:
    """Write both files into ``outdir``; return their paths."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    likes_path = outdir / LIKES_NAME
    playlists_path = outdir / PLAYLISTS_NAME
    likes_path.write_text(json.dumps(likes, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    playlists_path.write_text(
        json.dumps(playlists, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return likes_path, playlists_path
