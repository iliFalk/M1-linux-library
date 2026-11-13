"""Tests for the library lane: store, scan/sync/report and the CLI.

The pipeline is never imported for real.  ``music.ingest``, ``music.resolve``
and ``music.convert`` are replaced with stubs in ``sys.modules`` so the suite
is offline, standard-library only, and never shells out to ffmpeg.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import music  # noqa: E402  (namespace package)
from music import cli, library, store  # noqa: E402


def a_track(
    spotify_id: str,
    artist: str,
    title: str,
    album: str,
    added_at: str = "2024-01-01T00:00:00+00:00",
) -> dict[str, str]:
    return {
        "spotify_id": spotify_id,
        "artist": artist,
        "title": title,
        "album": album,
        "added_at": added_at,
    }


class ResolveResult:
    def __init__(self, ok: bool, path: str | None, error: str | None) -> None:
        self.ok = ok
        self.path = path
        self.error = error


class FakeResolver:
    def __init__(self, owner: "LibraryTestCase") -> None:
        self.owner = owner

    def resolve(self, artist: str, title: str, album: str) -> None:
        self.owner.resolver_calls.append((artist, title, album))


class LibraryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "Music"
        self.state = base / "state"
        self.root.mkdir()
        self.config = {
            "root": str(self.root),
            "state": str(self.state),
            "likes_export": None,
            "resolver": "fake",
            "resolver_args": [],
            "ffmpeg": "ffmpeg",
            "ffprobe": "ffprobe",
            "max_attempts": 3,
        }
        self.likes: list[dict[str, str]] = []
        self.failing: set[str] = set()
        self.resolver_calls: list[tuple[str, str, str]] = []
        self.resolve_one_calls: list[str] = []
        self.probe_calls: list[str] = []
        self.ingest_calls: list[str] = []
        self.resolver = FakeResolver(self)
        self._install_pipeline()

    # -- pipeline stubs ---------------------------------------------------

    def _install_module(self, name: str, module: types.ModuleType) -> None:
        previous = sys.modules.get(name)
        child = name.split(".", 1)[1]
        had_attr = hasattr(music, child)
        previous_attr = getattr(music, child, None)
        sys.modules[name] = module
        setattr(music, child, module)

        def restore() -> None:
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
            if had_attr:
                setattr(music, child, previous_attr)
            else:
                try:
                    delattr(music, child)
                except AttributeError:
                    pass

        self.addCleanup(restore)

    def _install_pipeline(self) -> None:
        ingest = types.ModuleType("music.ingest")
        ingest.collect_likes = lambda config: {
            "source": "cli",
            "likes": [dict(track) for track in self.likes],
        }
        ingest.merge_likes = lambda config, likes: 0
        ingest.read_likes = lambda config: [dict(track) for track in self.likes]

        def ingest_all(config):
            self.ingest_calls.append("ingest")
            return {
                "source": "cli",
                "liked": len(self.likes),
                "added": 0,
                "total": len(self.likes),
            }

        ingest.ingest = ingest_all

        resolve = types.ModuleType("music.resolve")

        def make_resolver(config):
            return self.resolver

        def resolve_one(config, track, resolver=None):
            (resolver or self.resolver).resolve(
                track["artist"], track["title"], track["album"]
            )
            self.resolve_one_calls.append(track["spotify_id"])
            if track["spotify_id"] in self.failing:
                return ResolveResult(False, None, "not found")
            path = (
                self.root
                / track["artist"]
                / track["album"]
                / f"01 - {track['title']}.m4a"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake audio:" + track["spotify_id"].encode())
            return ResolveResult(True, str(path), None)

        resolve.make_resolver = make_resolver
        resolve.resolve_one = resolve_one
        resolve.ResolveResult = ResolveResult

        convert = types.ModuleType("music.convert")

        def probe_duration(path):
            self.probe_calls.append(str(path))
            return 123.5

        convert.probe_duration = probe_duration

        self._install_module("music.ingest", ingest)
        self._install_module("music.resolve", resolve)
        self._install_module("music.convert", convert)

    # -- helpers ----------------------------------------------------------

    def _make_file(self, track: dict[str, str]) -> Path:
        path = (
            self.root
            / track["artist"]
            / track["album"]
            / f"01 - {track['title']}.m4a"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake audio:" + track["spotify_id"].encode())
        return path

    def _seed(self, tracks: list[dict[str, str]]) -> None:
        conn = store.connect(self.config)
        try:
            for track in tracks:
                store.upsert_track(conn, track)
        finally:
            conn.close()

    def _rows(self) -> dict[str, dict]:
        conn = store.connect(self.config, readonly=True)
        try:
            return {row["spotify_id"]: row for row in store.list_tracks(conn)}
        finally:
            conn.close()

    def _snapshot(self, *directories: Path) -> dict[str, tuple[str, int, int]]:
        snapshot: dict[str, tuple[str, int, int]] = {}
        for directory in directories:
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    stat = path.stat()
                    snapshot[str(path)] = (
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        stat.st_mtime_ns,
                        stat.st_size,
                    )
        return snapshot

    # -- 1. rescan is idempotent -----------------------------------------

    def test_rescan_is_idempotent(self) -> None:
        tracks = [
            a_track("id-1", "Artist A", "Song One", "Album One"),
            a_track("id-2", "Artist B", "Song Two", "Album Two"),
        ]
        for track in tracks:
            self._make_file(track)
        self._seed(tracks)

        first = library.scan(self.config)
        self.assertEqual(first, {"added": 2, "missing": 0, "failed": 0})
        before = self._rows()
        self.assertEqual({row["status"] for row in before.values()}, {"ok"})
        expected_paths = {
            str(
                (self.root / track["artist"] / track["album"] / f"01 - {track['title']}.m4a").resolve()
            )
            for track in tracks
        }
        self.assertEqual({row["path"] for row in before.values()}, expected_paths)
        self.assertEqual({row["seconds"] for row in before.values()}, {123.5})

        second = library.scan(self.config)
        self.assertEqual(second["added"], 0)
        after = self._rows()
        self.assertEqual(set(before), set(after))
        for spotify_id, row in before.items():
            self.assertEqual(row["status"], after[spotify_id]["status"])
            self.assertEqual(row["path"], after[spotify_id]["path"])
            self.assertEqual(row["updated_at"], after[spotify_id]["updated_at"])

    # -- 2. hand-deleted file leaves the row, status missing ---------------

    def test_deleted_file_marks_row_missing_and_keeps_it(self) -> None:
        keep = a_track("id-keep", "Artist A", "Keep Me", "Album One")
        gone = a_track("id-gone", "Artist B", "Delete Me", "Album Two")
        for track in (keep, gone):
            self._make_file(track)
        self._seed([keep, gone])
        library.scan(self.config)

        gone_path = self.root / gone["artist"] / gone["album"] / f"01 - {gone['title']}.m4a"
        gone_path.unlink()

        result = library.scan(self.config)
        self.assertEqual(result, {"added": 0, "missing": 1, "failed": 0})
        rows = self._rows()
        self.assertIn("id-gone", rows)
        self.assertEqual(rows["id-gone"]["status"], "missing")
        self.assertEqual(rows["id-gone"]["path"], str(gone_path.resolve()))
        self.assertEqual(rows["id-keep"]["status"], "ok")
        self.assertEqual(len(rows), 2)

    # -- 3. an ok track is never resolved twice ----------------------------

    def test_ok_track_is_not_resolved_again(self) -> None:
        track = a_track("id-1", "Artist A", "Song One", "Album One")
        self.likes = [track]

        first = library.sync(self.config)
        self.assertEqual(first, {"added": 1, "missing": 0, "failed": 0})
        self.assertEqual(len(self.resolver_calls), 1)
        self.assertEqual(self.resolve_one_calls, ["id-1"])
        self.assertEqual(self._rows()["id-1"]["status"], "ok")

        second = library.sync(self.config)
        self.assertEqual(second, {"added": 0, "missing": 0, "failed": 0})
        self.assertEqual(len(self.resolver_calls), 1)
        self.assertEqual(self.resolve_one_calls, ["id-1"])

    # -- 4. three failures park the track, the batch still proceeds --------

    def test_three_failures_park_and_later_sync_skips(self) -> None:
        bad = a_track("id-bad", "Artist A", "Broken", "Album One")
        good = a_track("id-good", "Artist B", "Fine", "Album Two")
        self.likes = [bad, good]
        self.failing = {"id-bad"}

        first = library.sync(self.config)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(first["added"], 1)
        self.assertEqual(self._rows()["id-good"]["status"], "ok")  # batch went on
        self.assertEqual(self._rows()["id-bad"]["status"], "failed")
        self.assertEqual(self._rows()["id-bad"]["attempts"], 1)

        library.sync(self.config)
        library.sync(self.config)
        parked = self._rows()["id-bad"]
        self.assertEqual(parked["status"], "parked")
        self.assertEqual(parked["attempts"], 3)
        self.assertEqual(parked["error"], "not found")

        bad_calls = [sid for sid in self.resolve_one_calls if sid == "id-bad"]
        good_calls = [sid for sid in self.resolve_one_calls if sid == "id-good"]
        self.assertEqual(len(bad_calls), 3)
        self.assertEqual(len(good_calls), 1)

        later = library.sync(self.config)
        self.assertEqual(later, {"added": 0, "missing": 0, "failed": 0})
        self.assertEqual(
            len([sid for sid in self.resolve_one_calls if sid == "id-bad"]), 3
        )
        self.assertEqual(self._rows()["id-bad"]["status"], "parked")
        self.assertEqual(self._rows()["id-bad"]["error"], "not found")

    # -- 5. dry run touches nothing ----------------------------------------

    def test_dry_run_changes_nothing_at_all(self) -> None:
        track = a_track("id-1", "Artist A", "Song One", "Album One")
        self.likes = [track]
        library.sync(self.config)

        snapshot_before = self._snapshot(self.root, self.state)
        rows_before = self._rows()
        count_before = len(rows_before)
        self.ingest_calls.clear()
        self.resolve_one_calls.clear()
        self.probe_calls.clear()

        result = library.sync(self.config, dry_run=True)
        self.assertEqual(result, {"added": 0, "missing": 0, "failed": 0})
        self.assertEqual(self._snapshot(self.root, self.state), snapshot_before)
        self.assertEqual(len(self._rows()), count_before)
        self.assertEqual(self.ingest_calls, [])  # no likes write
        self.assertEqual(self.resolve_one_calls, [])  # no download
        self.assertEqual(self.probe_calls, [])  # no ffprobe

        library.scan(self.config, dry_run=True)
        self.assertEqual(self._snapshot(self.root, self.state), snapshot_before)

        # A state dir that does not exist yet must stay non-existent.
        fresh = dict(self.config, state=str(Path(self.tmp.name) / "state-2"))
        self.likes = [a_track("id-2", "Artist B", "Song Two", "Album Two")]
        library.sync(fresh, dry_run=True)
        self.assertFalse(Path(fresh["state"]).exists())
        self.assertFalse((Path(fresh["state"]) / "library.db").exists())

    # -- 6. lazy pipeline imports ------------------------------------------

    def test_scan_and_report_work_without_pipeline(self) -> None:
        track = a_track("id-1", "Artist A", "Song One", "Album One")
        path = self._make_file(track)
        self._seed([track])

        for name in ("music.ingest", "music.resolve", "music.convert"):
            sys.modules.pop(name, None)
            child = name.split(".", 1)[1]
            if hasattr(music, child):
                delattr(music, child)

        result = library.scan(self.config)
        self.assertEqual(result["added"], 1)
        self.assertEqual(self._rows()["id-1"]["status"], "ok")
        self.assertEqual(self._rows()["id-1"]["path"], str(path.resolve()))
        self.assertEqual(library.report(self.config), [
            {"artist": "Artist A", "album": "Album One", "tracks": 1, "seconds": 0.0}
        ])

    # -- CLI --------------------------------------------------------------

    def _config_file(self) -> Path:
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        return path

    def test_cli_sync_prints_exact_summary_line(self) -> None:
        self.likes = [
            a_track("id-1", "Artist A", "Song One", "Album One"),
            a_track("id-2", "Artist B", "Song Two", "Album Two"),
        ]
        config_file = self._config_file()

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["sync", "--config", str(config_file)])

        self.assertEqual(code, 0)
        self.assertEqual(
            stdout.getvalue().strip().splitlines(),
            ["added 2, missing 0, failed 0"],
        )

    def test_cli_dry_run_prints_summary_and_touches_nothing(self) -> None:
        self.likes = [a_track("id-1", "Artist A", "Song One", "Album One")]
        config_file = self._config_file()
        library.sync(self.config)
        snapshot = self._snapshot(self.root, self.state)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["sync", "--dry-run", "--config", str(config_file)])

        self.assertEqual(code, 0)
        self.assertEqual(
            stdout.getvalue().strip().splitlines(),
            ["added 0, missing 0, failed 0"],
        )
        self.assertEqual(self._snapshot(self.root, self.state), snapshot)

    def test_cli_report_prints_header_and_total(self) -> None:
        self.likes = [
            a_track("id-1", "Artist A", "Song One", "Album One"),
            a_track("id-2", "Artist A", "Song Two", "Album One"),
        ]
        config_file = self._config_file()
        library.sync(self.config)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["report", "--config", str(config_file)])

        self.assertEqual(code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[0].split(), ["artist", "album", "tracks", "seconds"])
        self.assertTrue(lines[-1].startswith("TOTAL"))
        self.assertIn("Album One", lines[1])
        self.assertEqual(library.report(self.config), [
            {
                "artist": "Artist A",
                "album": "Album One",
                "tracks": 2,
                "seconds": 247.0,
            }
        ])

    def test_cli_bad_config_exits_1_with_reason(self) -> None:
        config_file = Path(self.tmp.name) / "broken.json"
        config_file.write_text("{not json", encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(["report", "--config", str(config_file)])
        self.assertEqual(code, 1)
        self.assertIn("invalid JSON", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
