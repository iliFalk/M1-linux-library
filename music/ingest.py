"""Collect the user's liked tracks and merge them into ``<state>/likes.json``.

Two read paths, one shape: the ``spotify`` CLI when it is on PATH, otherwise a
JSON export.  Everything written here is the five-key track dict from the
shared contract, and ``added_at`` is never ``None``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import shutil
import subprocess

_STDERR_TAIL = 500
_PAYLOAD_LIST_KEYS = ("items", "tracks", "likes", "liked")
_WRAPPED_ROW_KEYS = ("track", "song")


def media_root(config) -> pathlib.Path:
    if config and config.get("root"):
        return pathlib.Path(str(config["root"]))
    return pathlib.Path(os.environ.get("MUSIC_ROOT") or (pathlib.Path.home() / "Music"))


def state_dir(config) -> pathlib.Path:
    if config and config.get("state"):
        return pathlib.Path(str(config["state"]))
    return pathlib.Path(os.environ.get("MUSIC_STATE") or (pathlib.Path.cwd() / "state"))


def likes_path(config) -> pathlib.Path:
    return state_dir(config) / "likes.json"


def _tail(text: str | None, limit: int = _STDERR_TAIL) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        text = text[-limit:]
    return text or "no diagnostic output"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _artist(value) -> str:
    if isinstance(value, list):
        names = []
        for item in value:
            if isinstance(item, dict):
                names.append(_text(item.get("name") or item.get("artist")))
            else:
                names.append(_text(item))
        return ", ".join(name for name in names if name)
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("artist"))
    return _text(value)


def _album(value) -> str:
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("album"))
    if isinstance(value, list):
        return _album(value[0]) if value else ""
    return _text(value)


def _first(row: dict, keys) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return _text(value)
    return ""


def _local_id(artist: str, title: str, album: str) -> str:
    digest = hashlib.sha1(f"{artist}|{title}|{album}".encode("utf-8")).hexdigest()
    return "local-" + digest[:16]


def _row_to_track(row, now: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    inner = row
    for key in _WRAPPED_ROW_KEYS:
        if isinstance(row.get(key), dict):
            inner = row[key]
            break

    def take(*keys) -> str:
        for source in (inner, row):
            value = _first(source, keys)
            if value:
                return value
        return ""

    spotify_id = take("spotify_id", "id")
    title = take("title", "name")
    if not spotify_id and not title:
        return None

    artist = _artist(
        inner.get("artist") or inner.get("artists") or row.get("artist") or row.get("artists")
    )
    album = _album(inner.get("album") or row.get("album"))
    added_at = take("added_at", "added", "addedAt") or now

    if not spotify_id:
        spotify_id = _local_id(artist, title, album)
    if not title:
        title = ""
    return {
        "spotify_id": spotify_id,
        "artist": artist,
        "title": title,
        "album": album,
        "added_at": added_at,
    }


def _rows_to_tracks(payload, now: str | None = None) -> list[dict]:
    now = now or _now_iso()
    rows = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in _PAYLOAD_LIST_KEYS:
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    tracks = []
    seen = set()
    for row in rows:
        track = _row_to_track(row, now)
        if track is None or track["spotify_id"] in seen:
            continue
        seen.add(track["spotify_id"])
        tracks.append(track)
    return tracks


def _collect_export(export) -> dict:
    path = pathlib.Path(str(export)).expanduser()
    if not path.exists():
        raise RuntimeError(f"likes export not found at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"could not read the likes export at {path}: {exc}") from exc
    return {"source": "export", "likes": _rows_to_tracks(payload)}


def _collect_cli(cli: str, config) -> dict:
    proc = subprocess.run(
        [cli, "liked", "--json"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        export = config.get("likes_export") if config else None
        if export:
            return _collect_export(export)
        raise RuntimeError(
            f"`spotify liked --json` exited with {proc.returncode}: {_tail(proc.stderr)}"
        )
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"`spotify liked --json` did not print JSON: {exc}"
        ) from exc
    return {"source": "cli", "likes": _rows_to_tracks(payload)}


def collect_likes(config) -> dict:
    """Read liked tracks through the adapter; writes nothing."""
    cli = shutil.which("spotify")
    if cli:
        return _collect_cli(cli, config)
    export = config.get("likes_export") if config else None
    if export:
        return _collect_export(export)
    raise RuntimeError(
        "no likes source: the `spotify` binary is not on PATH and no likes export "
        "is configured (set MUSIC_LIKES_EXPORT / config['likes_export'] to a Spotify "
        "likes JSON export, or install the spotify CLI)"
    )


def read_likes(config) -> list[dict]:
    """The stored like list, or ``[]`` when the file does not exist yet."""
    path = likes_path(config)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"{path} is not a JSON list of tracks")
    return [dict(item) for item in data if isinstance(item, dict)]


def _normalise_track(track, now: str) -> dict | None:
    if not isinstance(track, dict):
        return None
    spotify_id = _text(track.get("spotify_id"))
    artist = _text(track.get("artist"))
    title = _text(track.get("title"))
    album = _text(track.get("album"))
    added_at = _text(track.get("added_at")) or now
    if not spotify_id:
        if not title:
            return None
        spotify_id = _local_id(artist, title, album)
    return {
        "spotify_id": spotify_id,
        "artist": artist,
        "title": title,
        "album": album,
        "added_at": added_at,
    }


def _write_likes(config, tracks: list[dict]) -> None:
    path = likes_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}-{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(tracks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def merge_likes(config, likes) -> int:
    """Merge ``likes`` into ``state/likes.json``; return how many are new."""
    existing = read_likes(config)
    known = {t.get("spotify_id") for t in existing if isinstance(t.get("spotify_id"), str)}
    now = _now_iso()
    merged = list(existing)
    added = 0
    for item in likes or []:
        track = _normalise_track(item, now)
        if track is None or track["spotify_id"] in known:
            continue
        known.add(track["spotify_id"])
        merged.append(track)
        added += 1
    if added or not likes_path(config).exists():
        _write_likes(config, merged)
    return added


def ingest(config) -> dict:
    """collect_likes + merge_likes, with counts for the CLI to report."""
    collected = collect_likes(config)
    likes = collected["likes"]
    added = merge_likes(config, likes)
    return {
        "source": collected["source"],
        "liked": len(likes),
        "added": added,
        "total": len(read_likes(config)),
    }
