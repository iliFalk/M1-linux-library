"""Spotify Web API as the source of likes and playlists (AC48, AC49).

An app registered by the account owner now needs Spotify Premium, so the
pipeline uses the public client id of Exportify — a pre-2026 app — with its own
PKCE flow. The one-time authorization stores a refresh token; everything after
that is HTTP.

``fetch`` returns the same two shapes the CSV reader produces: the liked
tracks, one entry per Spotify id, and one object per playlist.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: The public client id of Exportify; a self-registered app would need Premium.
CLIENT_ID = "9950ac751e34487dbbe027c4fd7f8e99"

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"

#: One page of likes or playlist entries; the API maximum is 50.
PAGE = 50


class SpotifyError(RuntimeError):
    """The API answered with an error status."""


def _post(url: str, body: dict) -> dict:
    data = urllib.parse.urlencode(body).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            return json.load(answer)
    except urllib.error.HTTPError as exc:
        raise SpotifyError(f"{url}: {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}") from exc


def access_token(token_path: Path | str, post=_post) -> tuple[str, dict]:
    """Refresh the stored token; return ``(access_token, refreshed_payload)``."""
    path = Path(token_path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    refresh = str(stored.get("refresh_token") or "")
    if not refresh:
        raise SpotifyError(f"{path} holds no refresh_token; run the authorization again")
    answer = post(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": CLIENT_ID,
        },
    )
    token = str(answer.get("access_token") or "")
    if not token:
        raise SpotifyError(f"token endpoint returned no access_token: {answer}")
    merged = dict(stored)
    merged["access_token"] = token
    if answer.get("refresh_token"):
        merged["refresh_token"] = answer["refresh_token"]
    merged["scope"] = answer.get("scope", stored.get("scope", ""))
    return token, merged


def _get(url: str, token: str, get=None) -> dict:
    """One API call with the bearer token; a bad status becomes SpotifyError."""
    if get is None:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as answer:
                return json.load(answer)
        except urllib.error.HTTPError as exc:
            raise SpotifyError(f"{url}: {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}") from exc
    return get(url, token)


def _entry(item: dict) -> dict | None:
    """One like entry from a saved-track object, or ``None`` when unusable."""
    track = item.get("track") or {}
    spotify_id = str(track.get("id") or "")
    if not spotify_id:
        return None
    artists = [str(a.get("name") or "") for a in (track.get("artists") or [])]
    return {
        "spotify_id": spotify_id,
        "artist": ", ".join(name for name in artists if name),
        "title": str(track.get("name") or ""),
        "album": str((track.get("album") or {}).get("name") or ""),
        "added_at": str(item.get("added_at") or ""),
    }


def _pages(url: str, token: str, get=None):
    """Yield every page object of a paged Spotify collection."""
    while url:
        page = _get(url, token, get)
        yield page
        url = page.get("next") or ""


def likes(token: str, get=None) -> list[dict]:
    """The liked tracks, in Spotify's order, one entry per id."""
    entries: list[dict] = []
    seen: set[str] = set()
    for page in _pages(f"{API}/me/tracks?limit={PAGE}", token, get):
        for item in page.get("items") or []:
            entry = _entry(item)
            if entry is None or entry["spotify_id"] in seen:
                continue
            seen.add(entry["spotify_id"])
            entries.append(entry)
    return entries


def _playlist_tracks(playlist_id: str, token: str, get=None) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    url = f"{API}/playlists/{playlist_id}/tracks?limit={PAGE}"
    for page in _pages(url, token, get):
        for item in page.get("items") or []:
            entry = _entry(item)
            if entry is None or entry["spotify_id"] in seen:
                continue
            seen.add(entry["spotify_id"])
            entries.append(entry)
    return entries


def playlists(token: str, get=None) -> list[dict]:
    """One ``{"name", "tracks"}`` object per playlist that holds tracks."""
    answer: list[dict] = []
    for page in _pages(f"{API}/me/playlists?limit={PAGE}", token, get):
        for item in page.get("items") or []:
            name = str(item.get("name") or "").strip()
            playlist_id = str(item.get("id") or "")
            if not name or not playlist_id:
                continue
            tracks = _playlist_tracks(playlist_id, token, get)
            if tracks:
                answer.append({"name": name, "tracks": tracks})
    return answer


def fetch(token_path: Path | str, get=None, post=_post) -> tuple[list[dict], list[dict]]:
    """Refresh the token and return ``(likes, playlists)`` (AC48, AC49)."""
    token, refreshed = access_token(token_path, post=post)
    return likes(token, get), playlists(token, get)


def store_token(token_path: Path | str, payload: dict) -> None:
    """Write the refreshed token back, keeping the file private (AC49)."""
    import os

    path = Path(token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.chmod(path, 0o600)
