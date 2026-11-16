"""The Spotify adapter and the merge into ``<state_dir>/likes.json``.

Two sources, one shape (AC1, AC2): the ``spotify liked --json`` CLI when it is
on ``PATH``, otherwise the JSON export named by ``config["spotify_export"]``.
Both yield objects with exactly :data:`music._contract.LIKES_KEYS`.

``ingest()`` merges what the source yielded into ``<state_dir>/likes.json``,
keyed on ``spotify_id``, so a second run on the same source changes nothing
(AC3, AC4).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from music._contract import LIKES_KEYS

#: The executable looked up on ``PATH`` for AC2.
SPOTIFY_CLI = "spotify"

#: The arguments of the CLI invocation whose stdout is read (AC2).
SPOTIFY_ARGS = ("liked", "--json")

#: The identity of one like, taken from the contract, never restated.
ID_KEY = LIKES_KEYS[0]

#: File name inside ``state_dir`` (AC3).
LIKES_FILENAME = "likes.json"

#: Keys a source may wrap its track object in. A Spotify API dump nests the
#: track, an episode dump nests the episode (AC1).
WRAPPER_KEYS = ("track", "episode", "item")

#: Names a nested playlist entry may use for the value it wraps.
VALUE_KEYS = ("spotify_id", "id")


def likes_path(config: dict) -> Path:
    """``<state_dir>/likes.json`` for ``config`` (AC3)."""
    return Path(config["state_dir"]) / LIKES_FILENAME


def find_spotify_cli() -> str | None:
    """The ``spotify`` executable on ``PATH``, or ``None`` (AC1, AC2)."""
    return shutil.which(SPOTIFY_CLI)


def _text(value: object) -> str:
    """A string from a plain value, else ``""``."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    return ""


def _name(value: object) -> str:
    """A display name from a string, a list of objects or one object (AC1)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(part for part in (_name(item) for item in value) if part)
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("title"))
    return ""


def _take(row: dict, inner: dict, *keys: str) -> str:
    """The first non-empty value of ``keys``, the wrapped object first (AC1)."""
    for source in (inner, row):
        for key in keys:
            value = _text(source.get(key))
            if value:
                return value
    return ""


def _entry(raw: object) -> dict:
    """Normalise one liked-track object to exactly ``LIKES_KEYS``.

    A flat object carrying the five keys is copied as it is. A nested Spotify
    row — ``{"added_at": …, "track": {"id": …, "name": …, "artists": [...],
    "album": {...}}}`` — is read through its aliases, so a dump works as well
    as a hand-written list (AC1).
    """
    if not isinstance(raw, dict):
        raise ValueError(f"liked entry is not an object: {raw!r}")
    inner = raw
    for key in WRAPPER_KEYS:
        value = raw.get(key)
        if isinstance(value, dict):
            inner = value
            break
    absent = [key for key in LIKES_KEYS if key not in raw]
    if not absent:
        return {
            key: "" if raw[key] is None else str(raw[key]) for key in LIKES_KEYS
        }
    entry = {
        "spotify_id": _take(raw, inner, *VALUE_KEYS),
        "artist": _name(
            inner.get("artist")
            or inner.get("artists")
            or raw.get("artist")
            or raw.get("artists")
        ),
        "title": _take(raw, inner, "title", "name"),
        "album": _name(inner.get("album") or raw.get("album")),
        "added_at": _take(raw, inner, "added_at", "added", "addedAt")
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if not entry["spotify_id"] and not entry["title"]:
        raise ValueError(
            f"liked entry names neither an id nor a title: {raw!r}"
        )
    return entry


def normalise(raw: object) -> dict:
    """Public name of :func:`_entry`: one liked or playlist row, five keys."""
    return _entry(raw)


def _entries(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError(f"liked tracks are not a JSON list: {raw!r}")
    return [_entry(item) for item in raw]


def parse_spotify_cli(stdout: str) -> list[dict]:
    """Parse the stdout of ``spotify liked --json`` (AC2)."""
    try:
        raw = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"spotify printed no JSON list: {exc}") from exc
    return _entries(raw)


def parse_export(path: Path) -> list[dict]:
    """Parse the JSON export file (AC1)."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} holds no JSON list: {exc}") from exc
    return _entries(raw)


def _read_cli(cli: str) -> list[dict]:
    try:
        done = subprocess.run(
            [cli, *SPOTIFY_ARGS], check=False, capture_output=True, text=True
        )
    except OSError as exc:
        raise RuntimeError(f"{cli}: {exc}") from exc
    if done.returncode != 0:
        raise RuntimeError(
            f"{cli} {' '.join(SPOTIFY_ARGS)} exited {done.returncode}: "
            f"{done.stderr.strip()}"
        )
    return parse_spotify_cli(done.stdout)


def source_likes(config: dict) -> list[dict]:
    """Read the liked tracks through the adapter (AC1, AC2).

    The CLI wins when ``find_spotify_cli()`` names one; a CLI that fails (non
    zero exit, unreadable stdout) falls back to the export file, so a broken
    helper does not cost the run its likes.
    """
    cli = find_spotify_cli()
    if cli:
        try:
            return _read_cli(cli)
        except (RuntimeError, ValueError):
            pass
    return parse_export(Path(config["spotify_export"]))


def _dedupe(entries: list[dict]) -> list[dict]:
    """Drop repeated ``spotify_id``s, keeping the first occurrence's order."""
    seen: set[str] = set()
    unique: list[dict] = []
    for entry in entries:
        spotify_id = entry[ID_KEY]
        if spotify_id in seen:
            continue
        seen.add(spotify_id)
        unique.append(entry)
    return unique


def load_likes(config: dict) -> list[dict]:
    """The likes recorded in ``<state_dir>/likes.json`` (AC3).

    A missing file is an empty list; a corrupt one is an error, not a silent
    overwrite of whatever is there.
    """
    path = likes_path(config)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} holds no JSON list: {exc}") from exc
    return _entries(raw)


def _dump(path: Path, likes: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(likes, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def merge_likes(existing: list[dict], fetched: list[dict]) -> tuple[list[dict], int]:
    """Merge ``fetched`` into ``existing``; returns ``(merged, added)`` (AC4).

    Existing order is kept, new entries are appended in source order, and the
    ``spotify_id`` is the identity — so a second run adds nothing.
    """
    known = {entry[ID_KEY] for entry in existing}
    added: list[dict] = []
    for entry in fetched:
        spotify_id = entry[ID_KEY]
        if spotify_id in known:
            continue
        known.add(spotify_id)
        added.append(entry)
    return _dedupe(list(existing)) + added, len(added)


def ingest(config: dict) -> dict:
    """Read the likes and merge them into ``<state_dir>/likes.json``.

    Returns :data:`INGEST_RESULT_KEYS`: ``total`` is what the source yielded
    after dedupe, ``added`` is how many of those were new to ``likes.json``,
    ``likes`` is the full merged list.
    """
    fetched = _dedupe(source_likes(config))
    path = likes_path(config)
    existing = load_likes(config)
    merged, added = merge_likes(existing, fetched)
    _dump(path, merged)
    return {"total": len(fetched), "added": added, "likes": merged}
