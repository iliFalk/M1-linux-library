"""Tests for the likes adapter and state/likes.json merging (offline)."""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from music import ingest


class IngestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        self.state = self.base / "state"
        self.config = {
            "root": str(self.base / "Music"),
            "state": str(self.state),
            "likes_export": None,
            "resolver": "fake",
            "resolver_args": [],
            "ffmpeg": "ffmpeg",
            "ffprobe": "ffprobe",
            "max_attempts": 3,
        }

        self.export_only_bin = self.base / "no-spotify"
        self.export_only_bin.mkdir()

    # -- helpers ---------------------------------------------------------
    def export_path(self):
        """PATH on which no `spotify` exists, so the export is the only source."""
        return mock.patch.dict(os.environ, {"PATH": str(self.export_only_bin)})

    def export(self, payload) -> str:
        path = self.base / "export.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def spotify_cli(self, payload) -> pathlib.Path:
        bindir = self.base / "bin"
        bindir.mkdir()
        script = bindir / "spotify"
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "if sys.argv[1:] != ['liked', '--json']:\n"
            "    sys.exit('unexpected args: ' + repr(sys.argv[1:]))\n"
            f"print(json.dumps({payload!r}))\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return bindir

    # -- tests -----------------------------------------------------------
    def test_export_shape_is_normalised_and_merged_once(self):
        export = self.export(
            {
                "tracks": [
                    {
                        "id": "t1",
                        "artists": [{"name": "Nova"}, {"name": "Guest"}],
                        "name": "Alpha",
                        "album": {"name": "First Light"},
                        "addedAt": "2024-01-02T03:04:05Z",
                    },
                    {
                        "id": "t2",
                        "artist": "Nova",
                        "title": "Beta",
                        "album": "First Light",
                        "added": "2024-02-03T00:00:00Z",
                    },
                    {  # title but no id -> kept with a stable local id
                        "artists": ["Nova"],
                        "name": "No Id",
                        "album": {"name": "First Light"},
                    },
                    {"album": {"name": "Nothing"}},  # neither id nor title -> ignored
                ]
            }
        )
        self.config["likes_export"] = export

        with self.export_path():
            collected = ingest.collect_likes(self.config)
            self.assertEqual(collected["source"], "export")
            self.assertEqual(len(collected["likes"]), 3)
            first = collected["likes"][0]
            self.assertEqual(
                set(first), {"spotify_id", "artist", "title", "album", "added_at"}
            )
            self.assertEqual(first["artist"], "Nova, Guest")
            self.assertEqual(first["title"], "Alpha")
            self.assertEqual(first["album"], "First Light")
            self.assertEqual(first["added_at"], "2024-01-02T03:04:05Z")
            # added_at is never None, even when the row omits it
            for track in collected["likes"]:
                self.assertIsInstance(track["added_at"], str)
                self.assertTrue(track["added_at"])
                datetime.datetime.fromisoformat(track["added_at"])

            self.assertEqual(ingest.merge_likes(self.config, collected["likes"]), 3)
            path = ingest.likes_path(self.config)
            self.assertEqual(path, self.state / "likes.json")
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(stored, list)
            self.assertEqual(len(stored), 3)

            # second, identical run: nothing added, file keeps its length
            again = ingest.collect_likes(self.config)
            self.assertEqual(ingest.merge_likes(self.config, again["likes"]), 0)
            stored_again = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored_again), len(stored))
            self.assertEqual(stored_again, stored)

    def test_ingest_reports_counts_and_is_idempotent(self):
        self.config["likes_export"] = self.export(
            [
                {
                    "id": "x1",
                    "artists": ["Nova"],
                    "name": "Alpha",
                    "album": {"name": "First Light"},
                },
                {
                    "id": "x2",
                    "artist": "Nova",
                    "title": "Beta",
                    "album": "First Light",
                    "added_at": "2024-05-06T07:08:09Z",
                },
            ]
        )
        with self.export_path():
            first = ingest.ingest(self.config)
            self.assertEqual(first, {"source": "export", "liked": 2, "added": 2, "total": 2})
            second = ingest.ingest(self.config)
            self.assertEqual(second, {"source": "export", "liked": 2, "added": 0, "total": 2})

    def test_merge_only_counts_new_entries(self):
        self.assertEqual(
            ingest.merge_likes(
                self.config,
                [
                    {"spotify_id": "a", "artist": "Nova", "title": "Alpha",
                     "album": "First", "added_at": "2024-01-01T00:00:00Z"},
                    {"spotify_id": "b", "artist": "Nova", "title": "Beta",
                     "album": "First", "added_at": "2024-01-01T00:00:00Z"},
                    {"spotify_id": "b", "artist": "Nova", "title": "Beta",
                     "album": "First", "added_at": "2024-01-01T00:00:00Z"},
                ],
            ),
            2,
        )
        self.assertEqual(
            ingest.merge_likes(
                self.config,
                [
                    {"spotify_id": "b", "artist": "Nova", "title": "Beta",
                     "album": "First", "added_at": "2024-01-01T00:00:00Z"},
                    {"spotify_id": "c", "artist": "Nova", "title": "Gamma",
                     "album": "Second", "added_at": "2024-01-01T00:00:00Z"},
                ],
            ),
            1,
        )
        self.assertEqual(len(ingest.read_likes(self.config)), 3)

    def test_spotify_cli_adapter_is_used_when_on_path(self):
        bindir = self.spotify_cli(
            {
                "items": [
                    {
                        "id": "cli-1",
                        "artists": [{"name": "Nova"}],
                        "name": "Alpha",
                        "album": {"name": "First Light"},
                        "added_at": "2024-03-04T05:06:07Z",
                    }
                ]
            }
        )
        with mock.patch.dict(os.environ, {"PATH": str(bindir)}):
            collected = ingest.collect_likes(self.config)
        self.assertEqual(collected["source"], "cli")
        self.assertEqual(len(collected["likes"]), 1)
        self.assertEqual(collected["likes"][0]["spotify_id"], "cli-1")
        self.assertEqual(collected["likes"][0]["title"], "Alpha")

    def test_no_source_names_the_missing_input(self):
        empty = self.base / "empty-bin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            with self.assertRaises(RuntimeError) as ctx:
                ingest.collect_likes(self.config)
        message = str(ctx.exception)
        self.assertIn("spotify", message)
        self.assertIn("MUSIC_LIKES_EXPORT", message)

    def test_missing_export_file_raises(self):
        self.config["likes_export"] = str(self.base / "nope.json")
        with mock.patch.dict(os.environ, {"PATH": str(self.base / "empty-bin")}):
            (self.base / "empty-bin").mkdir(exist_ok=True)
            with self.assertRaises(RuntimeError) as ctx:
                ingest.collect_likes(self.config)
        self.assertIn("nope.json", str(ctx.exception))

    def test_read_likes_missing_file_is_empty(self):
        self.assertEqual(ingest.read_likes(self.config), [])
        self.assertFalse(ingest.likes_path(self.config).exists())

    def test_cli_failure_falls_back_to_export(self):
        self.config["likes_export"] = self.export(
            [{"id": "e1", "artists": ["Nova"], "name": "Alpha", "album": {"name": "A"}}]
        )
        bindir = self.base / "bad-bin"
        bindir.mkdir()
        script = bindir / "spotify"
        script.write_text(
            f"#!{sys.executable}\nimport sys\nsys.stderr.write('boom\\n')\nsys.exit(4)\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(bindir)}):
            collected = ingest.collect_likes(self.config)
        self.assertEqual(collected["source"], "export")
        self.assertEqual(collected["likes"][0]["spotify_id"], "e1")


if __name__ == "__main__":
    unittest.main()
