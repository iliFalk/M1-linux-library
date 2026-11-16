"""AC44-AC45: Spotify playlists written as Navidrome ``.m3u`` files."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music import _contract as contract
from music import cli, playlists, store

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
ABSENT = {
    "spotify_id": "sp-absent",
    "artist": "Autechre",
    "title": "Amber",
    "album": "Amber",
    "added_at": "2021-01-01T00:00:00Z",
}


class PlaylistTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.library_root = self.root / "library"
        self.state_dir = self.root / "state"
        self.export = self.root / "playlists.json"
        self.config = {
            "library_root": str(self.library_root),
            "state_dir": str(self.state_dir),
            "resolver": "fake",
            "spotify_export": str(self.root / "liked.json"),
        }

    # -- fixtures ----------------------------------------------------------

    def place(self, track: dict) -> str:
        """Put a file in the tree and a matching row in the index."""
        path = (
            self.library_root
            / track["artist"]
            / track["album"]
            / f"01 - {track['title']}.m4a"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        conn = store.open_index(self.config)
        try:
            store.upsert_track(conn, dict(track))
            conn.execute(
                "UPDATE tracks SET status = ?, path = ? WHERE spotify_id = ?",
                (contract.STATUS_OK, str(path), track["spotify_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        return str(path)

    def write_export(self, payload) -> None:
        self.export.write_text(json.dumps(payload), encoding="utf-8")

    # -- AC44 --------------------------------------------------------------

    def test_ac44_a_playlist_becomes_one_m3u_below_the_library(self):
        self.place(XTAL)
        self.place(THA)
        self.write_export(
            [{"name": "Ambient", "tracks": [XTAL, THA, ABSENT]}]
        )
        rows = playlists.build(self.config, str(self.export))
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), contract.PLAYLIST_KEYS)
        self.assertEqual(rows[0]["name"], "Ambient")
        self.assertEqual(rows[0]["tracks"], 2)
        self.assertEqual(rows[0]["missing"], 1)

        target = Path(rows[0]["path"])
        self.assertEqual(
            target, self.library_root / playlists.PLAYLISTS_DIR / "Ambient.m3u"
        )
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertEqual(
            lines[1:],
            [
                "Aphex Twin/Selected Ambient Works 85-92/01 - Xtal.m4a",
                "Aphex Twin/Selected Ambient Works 85-92/01 - Tha.m4a",
            ],
            "entries are relative to the music folder (AC45)",
        )

    def test_ac44_the_export_shapes_are_read(self):
        self.write_export({"playlists": [{"name": "A", "tracks": [XTAL]}]})
        self.assertEqual([p["name"] for p in playlists.parse_export(self.export)], ["A"])

        self.write_export({"Mine": [XTAL, THA]})
        parsed = playlists.parse_export(self.export)
        self.assertEqual([p["name"] for p in parsed], ["Mine"])
        self.assertEqual([t["spotify_id"] for t in parsed[0]["tracks"]], ["sp-xtal", "sp-tha"])

        self.write_export(
            [
                {
                    "name": "Nested",
                    "tracks": [
                        {
                            "added_at": "2022-02-02T00:00:00Z",
                            "track": {
                                "id": "sp-nested",
                                "name": "Teardrop",
                                "artists": [{"name": "Massive Attack"}],
                                "album": {"name": "Mezzanine"},
                            },
                        }
                    ],
                }
            ]
        )
        nested = playlists.parse_export(self.export)[0]["tracks"][0]
        self.assertEqual(nested["spotify_id"], "sp-nested")
        self.assertEqual(nested["artist"], "Massive Attack")

    def test_ac44_a_playlist_without_a_name_is_refused(self):
        self.write_export([{"tracks": [XTAL]}])
        with self.assertRaises(ValueError):
            playlists.parse_export(self.export)

    # -- AC45 --------------------------------------------------------------

    def test_ac45_only_placed_tracks_are_written(self):
        self.place(XTAL)
        conn = store.open_index(self.config)
        try:
            store.upsert_track(conn, dict(THA))
            conn.commit()
        finally:
            conn.close()
        self.write_export([{"name": "Half", "tracks": [XTAL, THA]}])
        rows = playlists.build(self.config, str(self.export))
        self.assertEqual(rows[0]["tracks"], 1)
        self.assertEqual(rows[0]["missing"], 1)
        text = Path(rows[0]["path"]).read_text(encoding="utf-8")
        self.assertNotIn("Tha", text, "a pending track never reaches the playlist")

    def test_ac45_the_file_name_is_sanitized(self):
        self.write_export([{"name": "Night / Drive", "tracks": []}])
        rows = playlists.build(self.config, str(self.export))
        target = Path(rows[0]["path"])
        self.assertEqual(target.parent, self.library_root / playlists.PLAYLISTS_DIR)
        self.assertNotIn("/", target.name)
        self.assertTrue(target.is_file())

    def test_ac45_the_source_comes_from_the_variable_when_the_flag_is_absent(self):
        self.write_export([{"name": "Env", "tracks": [XTAL]}])
        with mock.patch.dict(os.environ, {"MUSIC_PLAYLISTS_EXPORT": str(self.export)}):
            rows = playlists.build(self.config)
        self.assertEqual(rows[0]["name"], "Env")

    def test_ac45_no_source_names_the_gap(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as caught:
                playlists.build(self.config)
        self.assertIn("MUSIC_PLAYLISTS_EXPORT", str(caught.exception))


class PlaylistCommandTest(PlaylistTestCase):
    def run_main(self, argv: list[str]):
        out = io.StringIO()
        err = io.StringIO()
        env = {
            contract.ENV_VARS["library_root"]: self.config["library_root"],
            contract.ENV_VARS["state_dir"]: self.config["state_dir"],
            contract.ENV_VARS["resolver"]: self.config["resolver"],
            contract.ENV_VARS["spotify_export"]: self.config["spotify_export"],
        }
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv, env)
        return code, out.getvalue(), err.getvalue()

    def test_ac44_the_command_prints_one_line_per_playlist(self):
        self.place(XTAL)
        self.write_export([{"name": "Ambient", "tracks": [XTAL, ABSENT]}])
        code, out, _err = self.run_main(["playlists", "--from", str(self.export)])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "Ambient: 1 tracks, 1 missing")

    def test_ac44_a_missing_source_exits_2(self):
        env_patch = mock.patch.dict(os.environ, {}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        code, _out, err = self.run_main(["playlists"])
        self.assertEqual(code, 2)
        self.assertIn("MUSIC_PLAYLISTS_EXPORT", err)


if __name__ == "__main__":
    unittest.main()
