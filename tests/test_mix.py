"""Mixes from audio features and artist genres (AC50, AC51)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music import _contract as contract, features, mix, store

LIKES = [
    ("sp1", "Aphex Twin", "Xtal", "SAW 85-92"),
    ("sp2", "Slayer", "Raining Blood", "Reign In Blood"),
    ("sp3", "Lofi Girl", "Rain", "Chill"),
    ("sp4", "Bee Gees", "How Deep Is Your Love", "Greatest"),
    ("sp5", "Slayer", "No Features", "Reign In Blood"),
]

NUMBERS = {
    "sp1": {"energy": 0.3, "tempo": 100, "valence": 0.2, "speechiness": 0.05},
    "sp2": {"energy": 0.95, "tempo": 190, "valence": 0.1, "speechiness": 0.1},
    "sp3": {"energy": 0.2, "tempo": 80, "valence": 0.4, "speechiness": 0.02},
    "sp4": {"energy": 0.5, "tempo": 105, "valence": 0.67, "speechiness": 0.03},
}

GENRES = {
    "Aphex Twin": ["idm", "electronic"],
    "Slayer": ["thrash metal", "speed metal"],
    "Lofi Girl": ["lo-fi"],
    "Bee Gees": ["disco"],
}


class FeaturesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = {
            "library_root": str(self.root / "library"),
            "state_dir": str(self.root / "state"),
            "resolver": "fake",
            "spotify_export": str(self.root / "likes.json"),
        }
        conn = store.open_index(self.config)
        try:
            features.ensure(conn)
            for spotify_id, artist, title, album in LIKES:
                store.upsert_track(
                    conn,
                    {
                        "spotify_id": spotify_id,
                        "artist": artist,
                        "title": title,
                        "album": album,
                        "added_at": "2024-01-01T00:00:00Z",
                    },
                )
                target = Path(self.config["library_root"]) / artist / album / f"{title}.m4a"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"audio")
                status = contract.STATUS_OK if spotify_id in NUMBERS else contract.STATUS_PENDING
                conn.execute(
                    "UPDATE tracks SET status = ?, path = ? WHERE spotify_id = ?",
                    (status, str(target) if spotify_id in NUMBERS else None, spotify_id),
                )
            conn.commit()
            rows = [
                {"id": spotify_id, **values} for spotify_id, values in NUMBERS.items()
            ]
            features.store_features(conn, rows)
            features.store_genres(conn, GENRES)
            conn.commit()
        finally:
            conn.close()

    def rules(self, **rules) -> dict:
        path = self.root / "mixes.json"
        path.write_text(json.dumps({"playlists": {"Mix": rules}}), encoding="utf-8")
        return mix.load(path)

    def test_ac50_the_tables_hold_what_was_stored(self):
        conn = store.open_index(self.config)
        try:
            self.assertEqual(set(features.read_features(conn)), set(NUMBERS))
            self.assertEqual(features.read_genres(conn)["Slayer"][0], "speed metal")
        finally:
            conn.close()

    def test_ac51_a_range_selects_and_a_limit_caps(self):
        rules = self.rules(require={"energy": [0.6, 1.0]}, limit=5)["Mix"]
        chosen = mix.select(self.config, rules)
        self.assertEqual([track["spotify_id"] for track in chosen], ["sp2"])

        rules = self.rules(require={"energy": [0.0, 1.0]}, sort=[["energy", "desc"]], limit=2)["Mix"]
        chosen = mix.select(self.config, rules)
        self.assertEqual([track["spotify_id"] for track in chosen], ["sp2", "sp4"])

    def test_ac51_genres_allow_and_deny(self):
        rules = self.rules(require={"energy": [0.0, 1.0]}, genres_any=["metal"])["Mix"]
        self.assertEqual([t["spotify_id"] for t in mix.select(self.config, rules)], ["sp2"])

        rules = self.rules(require={"energy": [0.0, 1.0]}, genres_none=["lo-fi"])["Mix"]
        chosen = [t["spotify_id"] for t in mix.select(self.config, rules)]
        self.assertNotIn("sp3", chosen)

    def test_ac51_a_track_without_features_never_enters(self):
        rules = self.rules(require={"energy": [0.0, 1.0]})["Mix"]
        chosen = [t["spotify_id"] for t in mix.select(self.config, rules)]
        self.assertNotIn("sp5", chosen, "a pending track without numbers is out")

    def test_ac51_a_typo_in_the_rule_file_is_named(self):
        with self.assertRaises(mix.MixError) as caught:
            self.rules(requirement={"energy": [0, 1]})
        self.assertIn("unknown key", str(caught.exception))

        with self.assertRaises(mix.MixError) as caught:
            self.rules(require={"nonsense": [0, 1]})
        self.assertIn("unknown feature", str(caught.exception))

    def test_ac51_build_writes_one_playlist_per_mix(self):
        path = self.root / "mixes.json"
        path.write_text(
            json.dumps(
                {
                    "playlists": {
                        "Training": {"require": {"energy": [0.6, 1.0]}},
                        "Evening": {"require": {"energy": [0.0, 0.35]}},
                    }
                }
            ),
            encoding="utf-8",
        )
        rows = mix.build(self.config, path)
        self.assertEqual([row["name"] for row in rows], ["Training", "Evening"])
        self.assertEqual(
            {row["name"]: row["tracks"] for row in rows},
            {"Training": 1, "Evening": 2},
            "sp2 is the loud one, sp1 and sp3 are the quiet pair",
        )
        for row in rows:
            text = Path(row["path"]).read_text(encoding="utf-8")
            self.assertIn("../", text, "entries stay relative to the playlist")


if __name__ == "__main__":
    unittest.main()
