"""Spotify playlists to Navidrome ``.m3u`` files.

One playlist of the export becomes one file under
``<library_root>/Playlists/<name>.m3u`` (AC44). A track is written only when
the index holds it with a placed file, so the playlist never names a file the
library does not have. The file names the tracks relative to ``library_root``:
Navidrome reads an ``.m3u`` entry relative to its music folder, and the folder
inside the player is not the host path (AC45).

The export shapes accepted here match the ingest reader: a flat object with the
five like keys, or a nested Spotify row.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from music import resolve, store
from music.ingest import normalise

#: Folder inside ``library_root`` that holds the generated playlists (AC44).
PLAYLISTS_DIR = "Playlists"

#: Suffix of every generated playlist file (AC44).
PLAYLIST_SUFFIX = ".m3u"

#: Names a playlist object may use for itself, and for its tracks (AC44).
NAME_KEYS = ("name", "title", "playlist")
TRACKS_KEYS = ("tracks", "items", "songs", "entries")

#: The key a whole export may wrap its playlist list in (AC44).
WRAPPER_KEYS = ("playlists", "items")


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _name_of(raw: dict) -> str:
    for key in NAME_KEYS:
        value = _text(raw.get(key)).strip()
        if value:
            return value
    return ""


def _tracks_of(raw: dict) -> list[dict]:
    for key in TRACKS_KEYS:
        value = raw.get(key)
        if isinstance(value, list):
            return [normalise(item) for item in value]
    return []


def parse_export(path: Path | str) -> list[dict]:
    """Read the playlist export into ``[{"name": str, "tracks": [...]}]``.

    Accepts a list of playlist objects, an object with a ``playlists`` list, or
    an object that maps a name to its track list (AC44).
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} holds no JSON: {exc}") from exc

    if isinstance(raw, dict):
        for key in WRAPPER_KEYS:
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            mapped = []
            for name, tracks in raw.items():
                if isinstance(tracks, list):
                    mapped.append({"name": str(name), "tracks": tracks})
            raw = mapped
    if not isinstance(raw, list):
        raise ValueError(f"{path} holds neither a list nor a mapping of playlists")

    playlists = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"playlist is not an object: {item!r}")
        name = _name_of(item)
        if not name:
            raise ValueError(f"playlist names neither name nor title: {item!r}")
        playlists.append({"name": name, "tracks": _tracks_of(item)})
    return playlists


def playlist_path(config: dict, name: str) -> Path:
    """``<library_root>/Playlists/<name>.m3u`` for one playlist (AC44)."""
    folder = Path(config["library_root"]) / PLAYLISTS_DIR
    return folder / f"{resolve.sanitize(name)}{PLAYLIST_SUFFIX}"


def _relative(library_root: Path, path: str) -> str:
    """The entry as Navidrome reads it: a path below the music folder (AC45)."""
    return Path(path).relative_to(library_root).as_posix()


def render(tracks: list[dict], placed: dict[str, str], library_root: Path) -> tuple[str, int]:
    """The file text and the number of tracks the library does not hold."""
    lines = ["#EXTM3U"]
    missing = 0
    for track in tracks:
        path = placed.get(track["spotify_id"])
        if path is None:
            missing += 1
            continue
        lines.append(_relative(library_root, path))
    return "\n".join(lines) + "\n", missing


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a private temp file, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()


def build(config: dict, source: Path | str | None = None) -> list[dict]:
    """Write one ``.m3u`` per playlist; return one ``PLAYLIST_KEYS`` row each.

    ``source`` defaults to ``MUSIC_PLAYLISTS_EXPORT``. Only a track the index
    holds with a placed file is written; the rest are counted as missing (AC44,
    AC45).
    """
    path = source or os.environ.get("MUSIC_PLAYLISTS_EXPORT") or ""
    if not path:
        raise ValueError(
            "no playlist export; pass --from PATH or set MUSIC_PLAYLISTS_EXPORT"
        )
    library_root = Path(config["library_root"])
    playlists = parse_export(path)

    conn = store.open_index_readonly(config)
    placed: dict[str, str] = {}
    try:
        if conn is not None:
            for playlist in playlists:
                for track in playlist["tracks"]:
                    spotify_id = track["spotify_id"]
                    if spotify_id in placed:
                        continue
                    row = store.get_track(conn, spotify_id)
                    if row is None or row["status"] != "ok":
                        continue
                    if store.path_is_placed(row["path"]):
                        placed[spotify_id] = str(row["path"])
    finally:
        if conn is not None:
            conn.close()

    rows = []
    for playlist in playlists:
        text, missing = render(playlist["tracks"], placed, library_root)
        target = playlist_path(config, playlist["name"])
        _write(target, text)
        rows.append(
            {
                "name": playlist["name"],
                "tracks": len(playlist["tracks"]) - missing,
                "missing": missing,
                "path": str(target),
            }
        )
    return rows
