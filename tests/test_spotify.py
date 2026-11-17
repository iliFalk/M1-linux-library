"""Spotify Web API source: token refresh, likes and playlists (AC48, AC49)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music import spotify

TOKEN = {
    "access_token": "old",
    "refresh_token": "refresh-token-value-1234567890",
    "expires_at": 1,
    "scope": "user-library-read playlist-read-private playlist-read-collaborative",
}


def saved(track_id: str, name: str, artist: str, album: str, added: str) -> dict:
    return {
        "added_at": added,
        "track": {
            "id": track_id,
            "name": name,
            "artists": [{"name": artist}],
            "album": {"name": album},
        },
    }


class SpotifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.token_path = Path(self._tmp.name) / "spotify.json"
        self.token_path.write_text(json.dumps(TOKEN), encoding="utf-8")

    def test_ac49_the_token_is_refreshed_and_written_back_private(self):
        posted = {}

        def post(url, body):
            posted.update(body)
            return {"access_token": "fresh", "expires_in": 3600, "scope": TOKEN["scope"]}

        token, refreshed = spotify.access_token(self.token_path, post=post)
        self.assertEqual(token, "fresh")
        self.assertEqual(posted["grant_type"], "refresh_token")
        self.assertEqual(posted["client_id"], spotify.CLIENT_ID)
        spotify.store_token(self.token_path, refreshed)
        self.assertEqual(json.loads(self.token_path.read_text())["access_token"], "fresh")
        self.assertEqual(oct(self.token_path.stat().st_mode)[-3:], "600")

    def test_ac49_a_token_file_without_refresh_names_the_problem(self):
        self.token_path.write_text(json.dumps({"access_token": "x"}), encoding="utf-8")
        with self.assertRaises(spotify.SpotifyError) as caught:
            spotify.access_token(self.token_path, post=lambda url, body: {})
        self.assertIn("refresh_token", str(caught.exception))

    def test_ac48_likes_come_paged_and_deduped(self):
        pages = {
            f"{spotify.API}/me/tracks?limit={spotify.PAGE}": {
                "items": [
                    saved("sp1", "One", "ArtistA", "AlbumX", "2024-01-01T00:00:00Z"),
                    saved("sp1", "One", "ArtistA", "AlbumX", "2024-01-01T00:00:00Z"),
                ],
                "next": "https://api.spotify.com/v1/me/tracks?offset=50",
            },
            "https://api.spotify.com/v1/me/tracks?offset=50": {
                "items": [saved("sp2", "Two", "ArtistB", "AlbumY", "2024-02-01T00:00:00Z")],
                "next": None,
            },
        }

        def get(url, token):
            return pages[url]

        liked = spotify.likes("fresh", get=get)
        self.assertEqual([entry["spotify_id"] for entry in liked], ["sp1", "sp2"])
        self.assertEqual(
            liked[0],
            {
                "spotify_id": "sp1",
                "artist": "ArtistA",
                "title": "One",
                "album": "AlbumX",
                "added_at": "2024-01-01T00:00:00Z",
            },
        )

    def test_ac48_a_playlist_keeps_its_order_and_drops_duplicates(self):
        pages = {
            f"{spotify.API}/me/playlists?limit={spotify.PAGE}": {
                "items": [
                    {"id": "p1", "name": "Ambient"},
                    {"id": "p2", "name": "Empty"},
                ],
                "next": None,
            },
            f"{spotify.API}/playlists/p1/tracks?limit={spotify.PAGE}": {
                "items": [
                    saved("sp1", "One", "ArtistA", "AlbumX", "2024-01-01T00:00:00Z"),
                    saved("sp1", "One", "ArtistA", "AlbumX", "2024-01-01T00:00:00Z"),
                    saved("sp2", "Two", "ArtistB", "AlbumY", "2024-02-01T00:00:00Z"),
                ],
                "next": None,
            },
            f"{spotify.API}/playlists/p2/tracks?limit={spotify.PAGE}": {
                "items": [],
                "next": None,
            },
        }

        lists = spotify.playlists("fresh", get=lambda url, token: pages[url])
        self.assertEqual([p["name"] for p in lists], ["Ambient"], "an empty playlist is dropped")
        self.assertEqual([t["spotify_id"] for t in lists[0]["tracks"]], ["sp1", "sp2"])


if __name__ == "__main__":
    unittest.main()
