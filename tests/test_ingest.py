"""AC1-AC4: the Spotify adapter and the merge into ``<state_dir>/likes.json``."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music import ingest as ingest_module
from music._contract import INGEST_RESULT_KEYS, LIKES_KEYS

XTAL = {
    "spotify_id": "sp-xtal",
    "artist": "Aphex Twin",
    "title": "Xtal",
    "album": "Selected Ambient Works 85-92",
    "added_at": "2019-05-01T10:00:00Z",
}

THA = {
    "spotify_id": "sp-tha",
    "artist": "Aphex Twin",
    "title": "Tha",
    "album": "Selected Ambient Works 85-92",
    "added_at": "2019-06-02T10:00:00Z",
}

CLI_ONLY = {
    "spotify_id": "sp-cli",
    "artist": "Boards of Canada",
    "title": "Roygbiv",
    "album": "Music Has the Right to Children",
    "added_at": "2020-01-01T00:00:00Z",
}

EXPORT_ONLY = {
    "spotify_id": "sp-export",
    "artist": "Autechre",
    "title": "Amber",
    "album": "Amber",
    "added_at": "2021-01-01T00:00:00Z",
}


class IngestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ingest-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.export = self.tmp / "liked.json"
        self.write_export([EXPORT_ONLY, XTAL, THA])
        self.config = {
            "library_root": str(self.tmp / "library"),
            "state_dir": str(self.state_dir),
            "resolver": "true",
            "spotify_export": str(self.export),
        }
        # A PATH that holds nothing but our own bin dir: no real spotify.
        patcher = mock.patch.dict(
            os.environ, {"PATH": str(self.bin_dir)}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- helpers ---------------------------------------------------------

    def write_export(self, entries: list[dict]) -> None:
        self.export.write_text(json.dumps(entries), encoding="utf-8")

    def fake_spotify_cli(self, entries: list[dict]) -> Path:
        script = self.bin_dir / "spotify"
        payload = json.dumps(json.dumps(entries))
        script.write_text(
            "#!/bin/sh\n"
            f"if [ \"$1\" = 'liked' ] && [ \"$2\" = '--json' ]; then\n"
            f"  printf '%s\\n' {payload}\n"
            "  exit 0\n"
            "fi\n"
            "echo 'usage: spotify liked --json' >&2\n"
            "exit 2\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def likes_json(self) -> list[dict]:
        return json.loads(
            (self.state_dir / "likes.json").read_text(encoding="utf-8")
        )

    # --- AC1 -------------------------------------------------------------

    def test_ac1_without_a_spotify_cli_the_export_file_is_read(self):
        self.assertIsNone(ingest_module.find_spotify_cli())
        result = ingest_module.ingest(self.config)
        self.assertEqual(tuple(result), INGEST_RESULT_KEYS)
        self.assertEqual(
            [entry["spotify_id"] for entry in result["likes"]],
            ["sp-export", "sp-xtal", "sp-tha"],
        )
        self.assertEqual(
            result["likes"][1],
            {key: XTAL[key] for key in LIKES_KEYS},
        )
        # The export really is the source: point it elsewhere and see the change.
        self.write_export([EXPORT_ONLY])
        again = ingest_module.ingest(self.config)
        self.assertEqual(
            [entry["spotify_id"] for entry in again["likes"]],
            ["sp-export", "sp-xtal", "sp-tha"],
            "likes.json keeps what a previous run merged",
        )
        self.assertEqual(again["total"], 1)
        self.assertEqual(again["added"], 0)

    def test_ac1_a_nested_spotify_dump_is_normalised(self):
        self.write_export(
            [
                {
                    "added_at": "2022-02-02T00:00:00Z",
                    "track": {
                        "id": "sp-nested",
                        "name": "Teardrop",
                        "artists": [{"name": "Massive Attack"}],
                        "album": {"name": "Mezzanine"},
                    },
                }
            ]
        )
        result = ingest_module.ingest(self.config)
        self.assertEqual(
            result["likes"],
            [
                {
                    "spotify_id": "sp-nested",
                    "artist": "Massive Attack",
                    "title": "Teardrop",
                    "album": "Mezzanine",
                    "added_at": "2022-02-02T00:00:00Z",
                }
            ],
        )
        self.assertEqual(tuple(self.likes_json()[0]), LIKES_KEYS)

    def test_ac1_an_entry_with_neither_id_nor_title_is_refused(self):
        self.write_export([{"track": {"album": {"name": "Nowhere"}}}])
        with self.assertRaises(ValueError):
            ingest_module.ingest(self.config)

    # --- AC2 -------------------------------------------------------------

    def test_ac2_with_a_spotify_cli_its_stdout_is_read(self):
        self.fake_spotify_cli([CLI_ONLY])
        self.assertIsNotNone(ingest_module.find_spotify_cli())
        result = ingest_module.ingest(self.config)
        self.assertEqual(tuple(result), INGEST_RESULT_KEYS)
        self.assertEqual(
            [entry["spotify_id"] for entry in result["likes"]],
            ["sp-cli"],
            "the CLI stdout wins over the export file",
        )
        on_disk = self.likes_json()
        self.assertEqual([entry["spotify_id"] for entry in on_disk], ["sp-cli"])
        self.assertEqual(tuple(on_disk[0]), LIKES_KEYS)

    # --- AC3 -------------------------------------------------------------

    def test_ac3_likes_json_holds_objects_with_exactly_the_five_keys(self):
        ingest_module.ingest(self.config)
        on_disk = self.likes_json()
        self.assertEqual(len(on_disk), 3)
        for entry in on_disk:
            self.assertEqual(tuple(entry), LIKES_KEYS)
            for key in LIKES_KEYS:
                self.assertIsInstance(entry[key], str)
        self.assertEqual(on_disk[1]["artist"], XTAL["artist"])
        self.assertEqual(on_disk[1]["added_at"], XTAL["added_at"])

    # --- AC4 -------------------------------------------------------------

    def test_ac4_a_second_ingest_keeps_the_count_and_no_duplicate_id(self):
        first = ingest_module.ingest(self.config)
        self.assertEqual(tuple(first), INGEST_RESULT_KEYS)
        self.assertEqual((first["total"], first["added"]), (3, 3))

        second = ingest_module.ingest(self.config)
        self.assertEqual(tuple(second), INGEST_RESULT_KEYS)
        self.assertEqual(
            (second["total"], second["added"]), (3, 0), "nothing new"
        )
        on_disk = self.likes_json()
        self.assertEqual(len(on_disk), 3, "entry count unchanged")
        ids = [entry["spotify_id"] for entry in on_disk]
        self.assertEqual(len(ids), len(set(ids)), "no duplicate spotify_id")

        # A new like appended on the third run counts as the only addition.
        self.write_export([EXPORT_ONLY, XTAL, THA, CLI_ONLY])
        third = ingest_module.ingest(self.config)
        self.assertEqual((third["total"], third["added"]), (4, 1))
        self.assertEqual(len(self.likes_json()), 4)


if __name__ == "__main__":
    unittest.main()
