"""TuneMyMusic export → likes.json and playlists.json (AC44-AC45 inputs)."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from music import tmc

ROWS = [
    {
        "Track name": "Xtal",
        "Artist name": "Aphex Twin",
        "Album": "Selected Ambient Works 85-92",
        "Playlist name": "Favorite Songs",
        "Type": "Favorite",
        "ISRC": "GBAKX9200027",
        "Spotify - id": "sp-xtal",
    },
    {
        "Track name": "Tha",
        "Artist name": "Aphex Twin",
        "Album": "Selected Ambient Works 85-92",
        "Playlist name": "Favorite Songs",
        "Type": "Favorite",
        "ISRC": "GBAKX9200028",
        "Spotify - id": "sp-tha",
    },
    {
        "Track name": "Xtal",
        "Artist name": "Aphex Twin",
        "Album": "Selected Ambient Works 85-92",
        "Playlist name": "Ambient",
        "Type": "Playlist",
        "ISRC": "GBAKX9200027",
        "Spotify - id": "sp-xtal",
    },
    {
        "Track name": "Xtal",
        "Artist name": "Aphex Twin",
        "Album": "Selected Ambient Works 85-92",
        "Playlist name": "Ambient",
        "Type": "Playlist",
        "ISRC": "GBAKX9200027",
        "Spotify - id": "sp-xtal",
    },
    {
        "Track name": "Teardrop",
        "Artist name": "Massive Attack",
        "Album": "Mezzanine",
        "Playlist name": "Ambient",
        "Type": "Playlist",
        "ISRC": "GBAAA9800616",
        "Spotify - id": "sp-teardrop",
    },
    {
        "Track name": "Mezzanine",
        "Artist name": "Massive Attack",
        "Album": "Mezzanine",
        "Playlist name": "Favorite Albums",
        "Type": "Album",
        "ISRC": "GBAAA9800600",
        "Spotify - id": "sp-album",
    },
    {
        "Track name": "Massive Attack",
        "Artist name": "",
        "Album": "",
        "Playlist name": "Favorite Artists",
        "Type": "Artist",
        "ISRC": "",
        "Spotify - id": "sp-artist",
    },
]


class TmcTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.export = self.root / "library.csv"
        self.write(ROWS)

    def write(self, rows: list[dict]) -> None:
        with self.export.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(tmc.COLUMNS))
            writer.writeheader()
            writer.writerows(rows)

    def test_favorites_become_likes_in_file_order(self):
        likes, _playlists = tmc.convert(self.export)
        self.assertEqual([entry["spotify_id"] for entry in likes], ["sp-xtal", "sp-tha"])
        self.assertEqual(tuple(likes[0]), ("spotify_id", "artist", "title", "album", "added_at"))
        self.assertLess(likes[0]["added_at"], likes[1]["added_at"])

    def test_playlists_group_and_dedupe(self):
        _likes, playlists = tmc.convert(self.export)
        self.assertEqual([p["name"] for p in playlists], ["Ambient"])
        self.assertEqual(
            [track["spotify_id"] for track in playlists[0]["tracks"]],
            ["sp-xtal", "sp-teardrop"],
            "a repeated id enters its playlist once",
        )

    def test_albums_and_artists_are_dropped(self):
        likes, playlists = tmc.convert(self.export)
        ids = {entry["spotify_id"] for entry in likes}
        ids |= {track["spotify_id"] for playlist in playlists for track in playlist["tracks"]}
        self.assertNotIn("sp-album", ids)
        self.assertNotIn("sp-artist", ids)

    def test_a_missing_column_is_named(self):
        with self.export.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["Track name", "Type"])
            writer.writeheader()
            writer.writerow({"Track name": "Xtal", "Type": "Favorite"})
        with self.assertRaises(ValueError) as caught:
            tmc.convert(self.export)
        self.assertIn("Spotify - id", str(caught.exception))

    def test_write_produces_both_files_readable_by_the_pipeline(self):
        likes, playlists = tmc.convert(self.export)
        likes_path, playlists_path = tmc.write(self.root / "out", likes, playlists)
        self.assertEqual(json.loads(likes_path.read_text(encoding="utf-8"))[0]["spotify_id"], "sp-xtal")
        self.assertEqual(json.loads(playlists_path.read_text(encoding="utf-8"))[0]["name"], "Ambient")


if __name__ == "__main__":
    unittest.main()
