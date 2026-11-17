"""Spotify audio features and artist genres, stored in the index (AC50).

A mix is a filter over numbers, so the numbers have to live next to the tracks:
``features`` holds one row per Spotify id (tempo, energy, valence, …) and
``artist_genres`` one row per artist and genre. Both come from the Web API in
batches — 100 ids per audio-features call, 50 per artists call — and both are
refreshed by ``refresh``, which reads the liked tracks once to learn the ids.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from music import spotify, store

#: Numeric columns of the ``features`` table, in the order they are stored.
FEATURE_KEYS = (
    "danceability",
    "energy",
    "tempo",
    "valence",
    "acousticness",
    "instrumentalness",
    "speechiness",
    "liveness",
    "loudness",
    "key",
    "mode",
    "duration_ms",
)

#: Ids per call; the API caps audio-features at 100 and artists at 50.
FEATURE_BATCH = 100
ARTIST_BATCH = 50

_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
    spotify_id TEXT PRIMARY KEY,
    danceability REAL, energy REAL, tempo REAL, valence REAL,
    acousticness REAL, instrumentalness REAL, speechiness REAL,
    liveness REAL, loudness REAL, key INTEGER, mode INTEGER,
    duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS artist_genres (
    artist TEXT NOT NULL,
    genre TEXT NOT NULL,
    PRIMARY KEY (artist, genre)
);
"""


def ensure(conn) -> None:
    """Create the two tables when they are missing. Safe to repeat (AC50)."""
    conn.executescript(_SCHEMA)
    conn.commit()


def store_features(conn, rows: list[dict]) -> int:
    """Insert one feature row per track; return how many were stored."""
    stored = 0
    for row in rows:
        spotify_id = str(row.get("id") or "")
        if not spotify_id:
            continue
        values = [row.get(key) for key in FEATURE_KEYS]
        if all(value is None for value in values):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO features (spotify_id, "
            + ", ".join(FEATURE_KEYS)
            + ") VALUES (?, "
            + ", ".join("?" for _ in FEATURE_KEYS)
            + ")",
            (spotify_id, *values),
        )
        stored += 1
    conn.commit()
    return stored


def store_genres(conn, mapping: dict[str, list[str]]) -> int:
    """Store ``{artist_name: [genre, …]}``; return how many pairs were written."""
    written = 0
    for artist, genres in mapping.items():
        for genre in genres:
            conn.execute(
                "INSERT OR IGNORE INTO artist_genres (artist, genre) VALUES (?, ?)",
                (artist, genre),
            )
            written += 1
    conn.commit()
    return written


def read_features(conn) -> dict[str, dict]:
    """``{spotify_id: {feature: value}}`` for every stored track."""
    cursor = conn.execute(
        "SELECT spotify_id, " + ", ".join(FEATURE_KEYS) + " FROM features"
    )
    rows = {}
    for row in cursor:
        rows[str(row[0])] = {
            key: row[index + 1] for index, key in enumerate(FEATURE_KEYS)
        }
    return rows


def read_genres(conn) -> dict[str, list[str]]:
    """``{artist: [genre, …]}`` for every stored artist."""
    rows: dict[str, list[str]] = {}
    for artist, genre in conn.execute(
        "SELECT artist, genre FROM artist_genres ORDER BY artist, genre"
    ):
        rows.setdefault(str(artist), []).append(str(genre))
    return rows


def _api(url: str, token: str, get=None) -> dict:
    if get is not None:
        return get(url, token)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            return json.load(answer)
    except urllib.error.HTTPError as exc:
        raise spotify.SpotifyError(
            f"{url}: {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}"
        ) from exc


def _chunks(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _liked_ids(token: str, get=None) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """Every liked id, plus ``{id: (artist name, artist id)}`` of its first artist."""
    ids: list[str] = []
    primary: dict[str, tuple[str, str]] = {}
    seen: set[str] = set()
    url = f"{spotify.API}/me/tracks?limit=50"
    while url:
        page = _api(url, token, get)
        for item in page.get("items") or []:
            entry = spotify._entry(item)
            if entry is None or entry["spotify_id"] in seen:
                continue
            seen.add(entry["spotify_id"])
            ids.append(entry["spotify_id"])
            artists = (item.get("track") or {}).get("artists") or []
            if artists:
                primary[entry["spotify_id"]] = (
                    str(artists[0].get("name") or ""),
                    str(artists[0].get("id") or ""),
                )
        url = page.get("next") or ""
    return ids, primary


def refresh(config: dict, token_path: Path | str, get=None, post=spotify._post) -> dict:
    """Read the liked tracks once, then their features and artist genres (AC50).

    Returns ``{"tracks": n, "features": n, "artists": n, "genres": n}``.
    """
    token, refreshed = spotify.access_token(token_path, post=post)
    spotify.store_token(token_path, refreshed)

    ids, primary = _liked_ids(token, get)
    artist_ids = sorted({artist_id for _, artist_id in primary.values() if artist_id})

    rows: list[dict] = []
    for batch in _chunks(ids, FEATURE_BATCH):
        answer = _api(
            f"{spotify.API}/audio-features?ids={urllib.parse.quote(','.join(batch))}",
            token,
            get,
        )
        rows.extend(item for item in (answer.get("audio_features") or []) if item)

    genres: dict[str, list[str]] = {}
    name_of: dict[str, str] = {}
    for batch in _chunks(artist_ids, ARTIST_BATCH):
        answer = _api(
            f"{spotify.API}/artists?ids={urllib.parse.quote(','.join(batch))}",
            token,
            get,
        )
        for artist in answer.get("artists") or []:
            if not artist:
                continue
            artist_id = str(artist.get("id") or "")
            name = str(artist.get("name") or "")
            if name:
                name_of[artist_id] = name
                genres.setdefault(name, []).extend(
                    str(genre) for genre in (artist.get("genres") or [])
                )
    # A name that the artist object did not confirm still gets an entry, so the
    # mix rules see the artist they know from the likes.
    for name, artist_id in primary.values():
        if name and name not in genres:
            fallback = name_of.get(artist_id)
            if fallback:
                genres[name] = []

    conn = store.open_index(config)
    try:
        ensure(conn)
        stored = store_features(conn, rows)
        written = store_genres(conn, {name: sorted(set(values)) for name, values in genres.items()})
    finally:
        conn.close()

    return {
        "tracks": len(ids),
        "features": stored,
        "artists": len(artist_ids),
        "genres": written,
    }
