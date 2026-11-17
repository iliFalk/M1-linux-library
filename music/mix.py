"""Compose playlists from audio features and artist genres (AC51).

A mix is a named rule set over the numbers ``features`` stores: a range per
field, an optional genre allow or deny list, a sort order and a size cap. The
selection runs over the index — a track without features cannot enter a mix and
is counted, never silently dropped — and each mix becomes one ``.m3u`` beside
the ones a Spotify export produces.
"""

from __future__ import annotations

import json
from pathlib import Path

from music import features, playlists, store

#: Rule fields a mix may name besides the numeric features.
GENRE_FIELDS = ("genres_any", "genres_none")

#: Keys a mix object may carry; a typo is an error, not a silent no-op.
MIX_KEYS = ("require", "sort", "limit") + GENRE_FIELDS


class MixError(ValueError):
    """A mix file the rules cannot read."""


def load(path: Path | str) -> dict[str, dict]:
    """Read the mix file into ``{name: rules}`` and validate every rule."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MixError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MixError(f"{path} is not JSON: {exc}") from exc
    mixes = raw.get("playlists") if isinstance(raw, dict) else None
    if not isinstance(mixes, dict):
        raise MixError(f"{path} needs an object under \"playlists\"")
    for name, rules in mixes.items():
        if not isinstance(rules, dict):
            raise MixError(f"{name}: rules are not an object")
        unknown = [key for key in rules if key not in MIX_KEYS]
        if unknown:
            raise MixError(f"{name}: unknown key(s) {', '.join(unknown)}")
        for field, bounds in (rules.get("require") or {}).items():
            if field not in features.FEATURE_KEYS:
                raise MixError(f"{name}: unknown feature {field}")
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                raise MixError(f"{name}: {field} needs [min, max]")
    return {str(name): dict(rules) for name, rules in mixes.items()}


def _genres_for(artist: str, genres: dict[str, list[str]]) -> list[str]:
    primary = artist.split(",")[0].strip()
    return [genre.lower() for genre in genres.get(primary, [])]


def matches(track: dict, values: dict, genres: dict[str, list[str]], rules: dict) -> bool:
    """True when one track satisfies every requirement of ``rules``."""
    for field, (low, high) in (rules.get("require") or {}).items():
        value = values.get(field)
        if value is None:
            return False
        if value < low or value > high:
            return False
    allowed = [str(item).lower() for item in rules.get("genres_any") or []]
    if allowed:
        track_genres = _genres_for(track.get("artist") or "", genres)
        if not any(word in genre for genre in track_genres for word in allowed):
            return False
    denied = [str(item).lower() for item in rules.get("genres_none") or []]
    if denied:
        track_genres = _genres_for(track.get("artist") or "", genres)
        if any(word in genre for genre in track_genres for word in denied):
            return False
    return True


def _order(track: dict, values: dict, sort: list) -> tuple:
    """Sort key from the rule's ``sort`` list; a missing value sinks to the end."""
    key = []
    for entry in sort or []:
        field, direction = entry[0], entry[1] if len(entry) > 1 else "asc"
        value = values.get(field)
        if value is None:
            key.append((1, 0.0))
        elif str(direction).lower() == "desc":
            key.append((0, -float(value)))
        else:
            key.append((0, float(value)))
    return tuple(key)


def select(config: dict, rules: dict, conn=None) -> list[dict]:
    """The tracks one mix names, in the rule's order, capped by ``limit``.

    A track enters only when the index holds it as ``ok`` with a placed file:
    a mix must not name a file the player cannot find.
    """
    own = conn is None
    if own:
        conn = store.open_index_readonly(config)
    if conn is None:
        return []
    try:
        values = features.read_features(conn)
        genres = features.read_genres(conn)
        cursor = conn.execute(
            "SELECT spotify_id, artist, title, album, path "
            "FROM tracks WHERE status = ?",
            ("ok",),
        )
        picked = []
        for row in cursor:
            spotify_id, artist, title, album, path = row
            if path is None or not store.path_is_placed(path):
                continue
            track = {
                "spotify_id": str(spotify_id),
                "artist": str(artist or ""),
                "title": str(title or ""),
                "album": str(album or ""),
                "path": str(path),
            }
            numbers = values.get(track["spotify_id"])
            if numbers is None:
                continue
            if not matches(track, numbers, genres, rules):
                continue
            track["_sort"] = _order(track, numbers, rules.get("sort") or [])
            picked.append(track)
    finally:
        if own:
            conn.close()

    picked.sort(key=lambda track: track["_sort"])
    for track in picked:
        track.pop("_sort", None)
    limit = int(rules.get("limit") or 0)
    return picked[:limit] if limit > 0 else picked


def build(config: dict, source: Path | str) -> list[dict]:
    """Write one ``.m3u`` per mix; return ``PLAYLIST_KEYS`` rows (AC51)."""
    mixes = load(source)
    conn = store.open_index_readonly(config)
    if conn is None:
        raise MixError("no index yet; run `music sync` first")
    try:
        rows = []
        for name, rules in mixes.items():
            chosen = select(config, rules, conn)
            target = playlists.playlist_path(config, name)
            placed = {track["spotify_id"]: track["path"] for track in chosen}
            entries = [
                {
                    "spotify_id": track["spotify_id"],
                    "artist": track["artist"],
                    "title": track["title"],
                    "album": track["album"],
                    "added_at": "",
                }
                for track in chosen
            ]
            text, missing = playlists.render(entries, placed, target.parent)
            playlists._write(target, text)
            rows.append(
                {"name": name, "tracks": len(chosen), "missing": missing, "path": str(target)}
            )
    finally:
        conn.close()
    return rows
