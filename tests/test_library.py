"""Tests for the library slice: index, sync, CLI, report (AC12-AC25).

The pipeline modules (``music.ingest``, ``music.resolve``) belong to another
lane.  Every sync here injects fakes — through ``steps=`` for the library API
and through ``sys.modules`` for the CLI — so this test module needs neither
those modules nor m4a/ffmpeg.
"""

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from music import _contract as contract
from music import cli, library, store


# --- fakes ----------------------------------------------------------------


class FakeIngest:
    """Returns the same likes list on every call and records the config."""

    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries
        self.calls: list[dict] = []

    def __call__(self, config: dict) -> dict:
        self.calls.append(config)
        return {
            "total": len(self.entries),
            "added": len(self.entries),
            "likes": [dict(entry) for entry in self.entries],
        }


class FakeResolve:
    """Places a fake audio file under the config's library_root, or fails.

    A track whose title is in ``fail_titles`` yields a failed result carrying
    the error text, so the retry/parking path is exercised without ffmpeg.
    """

    def __init__(
        self, fail_titles: tuple[str, ...] = (), touch_index: bool = False
    ) -> None:
        self.fail_titles = set(fail_titles)
        self.touch_index = touch_index
        self.calls: list[list[str]] = []
        self.tracks: list[dict] = []

    def __call__(self, config: dict, tracks: list[dict]) -> list[dict]:
        self.calls.append([track["spotify_id"] for track in tracks])
        self.tracks.extend(tracks)
        if self.touch_index:
            # A resolver that records into the index itself must still leave the
            # real state/library.db alone on a dry run (AC14).
            (Path(config["state_dir"]) / "library.db").write_bytes(b"touch")
        results = []
        for track in tracks:
            if track["title"] in self.fail_titles:
                results.append(
                    {
                        "spotify_id": track["spotify_id"],
                        "ok": False,
                        "path": None,
                        "error": f"resolver failed for {track['title']}",
                        "source": None,
                    }
                )
                continue
            target = (
                Path(config["library_root"])
                / str(track["artist"])
                / str(track["album"])
                / f"{track['title']}.m4a"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-audio")
            results.append(
                {
                    "spotify_id": track["spotify_id"],
                    "ok": True,
                    "path": str(target),
                    "error": None,
                    "source": str(target),
                }
            )
        return results


class LibraryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.library_root = self.root / "library"
        self.state_dir = self.root / "state"
        self.export = self.root / "liked.json"

    # -- fixtures ----------------------------------------------------------

    def config(self, **overrides) -> dict:
        config = {
            "library_root": str(self.library_root),
            "state_dir": str(self.state_dir),
            "resolver": "fake-resolver --flag",
            "spotify_export": str(self.export),
        }
        config.update(overrides)
        return config

    def env(self, **overrides) -> dict:
        env = {
            contract.ENV_VARS["library_root"]: str(self.library_root),
            contract.ENV_VARS["state_dir"]: str(self.state_dir),
            contract.ENV_VARS["resolver"]: "fake-resolver --flag",
            contract.ENV_VARS["spotify_export"]: str(self.export),
        }
        env.update(overrides)
        return env

    def like(
        self, spotify_id: str, artist: str, title: str, album: str,
        added_at: str = "2024-01-01T00:00:00Z",
    ) -> dict:
        return {
            "spotify_id": spotify_id,
            "artist": artist,
            "title": title,
            "album": album,
            "added_at": added_at,
        }

    def steps(self, entries: list[dict], fail_titles: tuple[str, ...] = ()):
        ingest = FakeIngest(entries)
        resolve = FakeResolve(fail_titles)
        steps = {"ingest": ingest, "resolve": resolve, "scan": store.scan}
        return steps, ingest, resolve

    @contextlib.contextmanager
    def fake_modules(self, ingest: FakeIngest, resolve: FakeResolve):
        """Install the pipeline modules as fakes for a CLI-driven sync."""
        ingest_module = types.ModuleType("music.ingest")
        ingest_module.ingest = ingest
        resolve_module = types.ModuleType("music.resolve")
        resolve_module.resolve_pending = resolve
        with mock.patch.dict(
            sys.modules,
            {"music.ingest": ingest_module, "music.resolve": resolve_module},
        ):
            yield

    def run_main(self, argv: list[str], env: dict | None = None):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv, self.env() if env is None else env)
        return code, out.getvalue(), err.getvalue()

    def run_sync_cli(self, argv: list[str], ingest, resolve, env=None):
        with self.fake_modules(ingest, resolve):
            return self.run_main(argv, env)

    def snapshot(self, root: Path) -> dict[str, bytes]:
        if not root.exists():
            return {}
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def db_path(self) -> Path:
        return self.state_dir / store.DB_NAME

    def seed_track(self, config: dict | None = None, **fields) -> None:
        conn = store.open_index(config or self.config())
        try:
            store.upsert_track(conn, fields)
        finally:
            conn.close()

    def row(self, spotify_id: str, config: dict | None = None) -> dict:
        conn = store.open_index(config or self.config())
        try:
            return store.get_track(conn, spotify_id)
        finally:
            conn.close()

    def results(self, conn: sqlite3.Connection) -> list[dict]:
        rows = store.report_rows(conn)
        for row in rows:
            self.assertEqual(tuple(row), contract.REPORT_ROW_KEYS)
        return rows


# --- sync summary and exit code ------------------------------------------


class TestSyncSummary(LibraryTestCase):
    def test_ac12_summary_line_counts_added_missing_failed(self):
        entries = [
            self.like("sp1", "ArtistA", "One", "AlbumX"),
            self.like("sp2", "ArtistA", "Two", "AlbumX"),
            self.like("sp3", "ArtistB", "Bad", "AlbumY"),
        ]
        ingest = FakeIngest(entries)
        resolve = FakeResolve(fail_titles=("Bad",))
        code, out, _err = self.run_sync_cli(["sync"], ingest, resolve)
        self.assertEqual(code, 0)
        self.assertEqual(
            [line for line in out.splitlines() if line.strip()],
            ["added 2, missing 1, failed 1"],
        )

        other = self.config(
            library_root=str(self.root / "library2"),
            state_dir=str(self.root / "state2"),
        )
        steps, _ingest, _resolve = self.steps(entries, fail_titles=("Bad",))
        summary = library.sync(other, steps=steps)
        self.assertEqual(tuple(summary), contract.SYNC_SUMMARY_KEYS)
        self.assertEqual(summary["added"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["missing"], 1)

    def test_ac13_dry_run_prints_the_summary_a_real_run_would_print(self):
        entries = [
            self.like("sp1", "ArtistA", "One", "AlbumX"),
            self.like("sp2", "ArtistB", "Bad", "AlbumY"),
        ]
        ingest = FakeIngest(entries)
        resolve = FakeResolve(fail_titles=("Bad",))

        code, dry_out, _err = self.run_sync_cli(
            ["sync", "--dry-run"], ingest, resolve
        )
        self.assertEqual(code, 0)
        self.assertFalse(self.db_path().exists())
        self.assertFalse(self.library_root.exists())

        code, real_out, _err = self.run_sync_cli(["sync"], ingest, resolve)
        self.assertEqual(code, 0)
        self.assertEqual(dry_out, real_out)
        self.assertEqual(dry_out.strip(), "added 1, missing 1, failed 1")

    def test_ac14_dry_run_leaves_tree_and_index_byte_identical(self):
        entries = [
            self.like("sp1", "ArtistA", "One", "AlbumX"),
            self.like("sp2", "ArtistB", "Bad", "AlbumY"),
        ]
        ingest = FakeIngest(entries)
        resolve = FakeResolve(fail_titles=("Bad",))
        code, _out, _err = self.run_sync_cli(["sync"], ingest, resolve)
        self.assertEqual(code, 0)
        resolve.touch_index = True

        db_before = self.db_path().read_bytes()
        tree_before = self.snapshot(self.library_root)
        self.assertTrue(tree_before)

        code, _out, _err = self.run_sync_cli(
            ["sync", "--dry-run"], ingest, resolve
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.db_path().read_bytes(), db_before)
        self.assertEqual(self.snapshot(self.library_root), tree_before)

    def test_ac15_one_failed_track_still_exits_zero(self):
        entries = [self.like("sp1", "ArtistB", "Bad", "AlbumY")]
        ingest = FakeIngest(entries)
        resolve = FakeResolve(fail_titles=("Bad",))
        code, out, _err = self.run_sync_cli(["sync"], ingest, resolve)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "added 0, missing 1, failed 1")
        row = self.row("sp1")
        self.assertEqual(row["status"], contract.STATUS_FAILED)
        self.assertEqual(row["attempts"], 1)


# --- the ingest command ----------------------------------------------------


class TestIngestCommand(LibraryTestCase):
    def test_ac41_ingest_prints_total_and_added(self):
        self.export.write_text(
            json.dumps(
                [
                    {
                        "spotify_id": "sp1",
                        "artist": "ArtistA",
                        "title": "One",
                        "album": "AlbumX",
                        "added_at": "2024-01-01T00:00:00Z",
                    }
                ]
            ),
            encoding="utf-8",
        )
        code, out, _err = self.run_main(["ingest"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "total 1, added 1")
        code, out, _err = self.run_main(["ingest"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "total 1, added 0", "a second run adds nothing")
        self.assertFalse(
            self.library_root.exists(), "ingest resolves nothing"
        )


# --- the status command ----------------------------------------------------


class TestStatusCommand(LibraryTestCase):
    def seed(self, statuses: list[tuple[str, str, str]]) -> None:
        conn = store.open_index(self.config())
        try:
            for index, (spotify_id, status, error) in enumerate(statuses):
                store.upsert_track(
                    conn,
                    self.like(spotify_id, "ArtistA", f"Track{index}", "AlbumX"),
                )
                conn.execute(
                    "UPDATE tracks SET status = ?, attempts = ?, error = ? "
                    "WHERE spotify_id = ?",
                    (status, 1 if error else 0, error, spotify_id),
                )
            conn.commit()
        finally:
            conn.close()

    def test_ac43_status_counts_and_lists_the_faults(self):
        self.seed(
            [
                ("sp1", contract.STATUS_OK, None),
                ("sp2", contract.STATUS_OK, None),
                ("sp3", contract.STATUS_FAILED, "resolver exited 1"),
                ("sp4", contract.STATUS_PARKED, "no source"),
            ]
        )
        code, out, _err = self.run_main(["status"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), list(contract.STATUS_ROW_KEYS))
        self.assertEqual(lines[1].split(), [contract.STATUS_PENDING, "0"])
        self.assertEqual(lines[2].split(), [contract.STATUS_OK, "2"])
        self.assertEqual(lines[3].split(), [contract.STATUS_FAILED, "1"])
        self.assertEqual(lines[4].split(), [contract.STATUS_PARKED, "1"])
        self.assertEqual(lines[5].split(), list(contract.FAULT_ROW_KEYS))
        first = lines[6].split()
        self.assertEqual(first[:5], ["ArtistA", "Track2", "AlbumX", "failed", "1"])
        self.assertEqual(" ".join(first[5:]), "resolver exited 1")
        second = lines[7].split()
        self.assertEqual(second[:5], ["ArtistA", "Track3", "AlbumX", "parked", "1"])
        self.assertEqual(" ".join(second[5:]), "no source")

    def test_ac43_status_says_so_when_no_index_exists(self):
        code, out, _err = self.run_main(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "no index yet")


# --- report ---------------------------------------------------------------


class TestReport(LibraryTestCase):
    def test_ac16_report_header_is_the_four_columns(self):
        self.seed_track(
            spotify_id="sp1",
            artist="ArtistA",
            title="One",
            album="AlbumX",
            path=str(self.library_root / "ArtistA" / "AlbumX" / "01 - One.m4a"),
            status=contract.STATUS_OK,
            attempts=0,
            error=None,
            added_at="2024-01-01T00:00:00Z",
            duration=120,
        )
        code, out, _err = self.run_main(["report"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines)
        self.assertEqual(lines[0].split(), ["artist", "album", "tracks", "seconds"])
        self.assertEqual(lines[0].split(), list(contract.REPORT_ROW_KEYS))

    def test_ac17_report_rows_come_from_the_index_with_whole_seconds(self):
        self.seed_track(
            spotify_id="sp1", artist="ArtistA", title="One", album="AlbumX",
            path=None, status=contract.STATUS_OK, attempts=0, error=None,
            added_at="2024-01-01T00:00:00Z", duration=100,
        )
        self.seed_track(
            spotify_id="sp2", artist="ArtistA", title="Two", album="AlbumX",
            path=None, status=contract.STATUS_OK, attempts=0, error=None,
            added_at="2024-01-02T00:00:00Z", duration=60,
        )
        self.seed_track(
            spotify_id="sp3", artist="ArtistB", title="Three", album="AlbumY",
            path=None, status=contract.STATUS_OK, attempts=0, error=None,
            added_at="2024-01-03T00:00:00Z", duration=45,
        )
        self.seed_track(
            spotify_id="sp4", artist="ArtistC", title="Bad", album="AlbumZ",
            path=None, status=contract.STATUS_FAILED, attempts=1,
            error="boom", added_at="2024-01-04T00:00:00Z", duration=0,
        )

        conn = store.open_index(self.config())
        try:
            rows = self.results(conn)
        finally:
            conn.close()
        self.assertEqual(
            rows,
            [
                {"artist": "ArtistA", "album": "AlbumX", "tracks": 2, "seconds": 160},
                {"artist": "ArtistB", "album": "AlbumY", "tracks": 1, "seconds": 45},
            ],
        )
        for row in rows:
            self.assertIsInstance(row["seconds"], int)

        code, out, _err = self.run_main(["report"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), list(contract.REPORT_ROW_KEYS))
        self.assertEqual(lines[1].split(), ["ArtistA", "AlbumX", "2", "160"])
        self.assertEqual(lines[2].split(), ["ArtistB", "AlbumY", "1", "45"])
        self.assertEqual(lines[3].split(), ["TOTAL", "3", "205"])
        self.assertEqual(len(lines), 4)

    def test_ac17_record_result_stores_the_duration_a_resolver_reported(self):
        conn = store.open_index(self.config())
        try:
            store.upsert_track(
                conn,
                {
                    "spotify_id": "sp9",
                    "artist": "ArtistA",
                    "title": "Nine",
                    "album": "AlbumX",
                    "path": None,
                    "status": contract.STATUS_PENDING,
                    "attempts": 0,
                    "error": None,
                    "added_at": "2024-01-05T00:00:00Z",
                },
            )
            store.record_result(
                conn,
                {
                    "spotify_id": "sp9",
                    "ok": True,
                    "path": str(
                        self.library_root / "ArtistA" / "AlbumX" / "09 - Nine.m4a"
                    ),
                    "error": None,
                    "source": "/tmp/nine.wav",
                    "duration": 60.5,
                },
            )
            rows = self.results(conn)
            self.assertEqual(
                rows,
                [
                    {
                        "artist": "ArtistA",
                        "album": "AlbumX",
                        "tracks": 1,
                        "seconds": 61,
                    }
                ],
            )

            # A failed result must not write a duration.
            store.record_result(
                conn,
                {
                    "spotify_id": "sp10",
                    "ok": False,
                    "path": None,
                    "error": "boom",
                    "source": None,
                    "duration": 99.0,
                },
            )
            self.assertEqual(self.results(conn), rows)
        finally:
            conn.close()


# --- scan -----------------------------------------------------------------


class TestScan(LibraryTestCase):
    def test_ac18_scan_keeps_the_row_of_a_deleted_file(self):
        placed = self.library_root / "ArtistA" / "AlbumX" / "One.m4a"
        placed.parent.mkdir(parents=True, exist_ok=True)
        placed.write_bytes(b"audio")
        self.seed_track(
            spotify_id="sp1", artist="ArtistA", title="One", album="AlbumX",
            path=str(placed), status=contract.STATUS_OK, attempts=0,
            error=None, added_at="2024-01-01T00:00:00Z",
        )
        placed.unlink()

        conn = store.open_index(self.config())
        try:
            result = store.scan(self.config(), conn)
            self.assertEqual(tuple(result), contract.SCAN_RESULT_KEYS)
            self.assertEqual(result["missing"], 1)
            self.assertEqual(result["indexed"], 0)
        finally:
            conn.close()

        row = self.row("sp1")
        self.assertIsNotNone(row)
        self.assertEqual(row["spotify_id"], "sp1")
        self.assertEqual(row["status"], contract.STATUS_OK)
        self.assertIsNone(row["path"])

    def test_ac19_scan_twice_leaves_the_index_unchanged_and_deduplicated(self):
        placed = self.library_root / "ArtistA" / "AlbumX" / "One.m4a"
        placed.parent.mkdir(parents=True, exist_ok=True)
        placed.write_bytes(b"audio")
        self.seed_track(
            spotify_id="sp1", artist="ArtistA", title="One", album="AlbumX",
            path=str(placed), status=contract.STATUS_OK, attempts=0,
            error=None, added_at="2024-01-01T00:00:00Z",
        )
        gone = self.library_root / "ArtistB" / "AlbumY" / "Two.m4a"
        self.seed_track(
            spotify_id="sp2", artist="ArtistB", title="Two", album="AlbumY",
            path=str(gone), status=contract.STATUS_OK, attempts=0,
            error=None, added_at="2024-01-02T00:00:00Z",
        )

        code, _out, _err = self.run_main(["scan"])
        self.assertEqual(code, 0)
        first = self.db_path().read_bytes()

        code, _out, _err = self.run_main(["scan"])
        self.assertEqual(code, 0)
        second = self.db_path().read_bytes()
        self.assertEqual(second, first)

        conn = sqlite3.connect(str(self.db_path()))
        try:
            total, distinct = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT spotify_id) FROM tracks"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(total, 2)
        self.assertEqual(distinct, 2)

    def test_ac34_scan_does_not_count_a_symlinked_path_as_a_placed_track(self):
        outside = self.root / "outside.m4a"
        outside.write_bytes(b"foreign bytes")
        placed = self.library_root / "ArtistA" / "AlbumX" / "One.m4a"
        placed.parent.mkdir(parents=True, exist_ok=True)
        placed.symlink_to(outside)
        self.seed_track(
            spotify_id="sp1", artist="ArtistA", title="One", album="AlbumX",
            path=str(placed), status=contract.STATUS_OK, attempts=0,
            error=None, added_at="2024-01-01T00:00:00Z",
        )

        conn = store.open_index(self.config())
        try:
            result = store.scan(self.config(), conn)
            self.assertEqual(result["indexed"], 0, "a link is not a placed track")
            self.assertEqual(result["missing"], 1)
        finally:
            conn.close()

        row = self.row("sp1")
        self.assertIsNotNone(row, "the row is kept (AC18)")
        self.assertEqual(row["status"], contract.STATUS_OK)
        self.assertIsNone(row["path"])
        self.assertEqual(outside.read_bytes(), b"foreign bytes")


# --- index lifecycle ------------------------------------------------------


class TestIndexLifecycle(LibraryTestCase):
    def test_ac20_an_indexed_track_is_not_resolved_or_overwritten(self):
        placed = self.library_root / "ArtistA" / "AlbumX" / "One.m4a"
        placed.parent.mkdir(parents=True, exist_ok=True)
        placed.write_bytes(b"original-file")
        self.seed_track(
            spotify_id="sp1", artist="ArtistA", title="One", album="AlbumX",
            path=str(placed), status=contract.STATUS_OK, attempts=0,
            error=None, added_at="2024-01-01T00:00:00Z",
        )
        entries = [
            self.like("sp1", "ArtistA", "One", "AlbumX"),
            self.like("sp2", "ArtistA", "Two", "AlbumX"),
        ]
        ingest = FakeIngest(entries)
        resolve = FakeResolve()
        code, _out, _err = self.run_sync_cli(["sync"], ingest, resolve)
        self.assertEqual(code, 0)

        seen = [spotify_id for call in resolve.calls for spotify_id in call]
        self.assertNotIn("sp1", seen)
        self.assertIn("sp2", seen)
        self.assertEqual(placed.read_bytes(), b"original-file")
        row = self.row("sp1")
        self.assertEqual(row["status"], contract.STATUS_OK)
        self.assertEqual(row["path"], str(placed))

    def test_ac21_a_failure_is_stored_with_error_and_attempts_1(self):
        entries = [self.like("sp1", "ArtistB", "Bad", "AlbumY")]
        steps, _ingest, _resolve = self.steps(entries, fail_titles=("Bad",))
        summary = library.sync(self.config(), steps=steps)
        self.assertEqual(tuple(summary), contract.SYNC_SUMMARY_KEYS)
        self.assertEqual(summary["failed"], 1)

        row = self.row("sp1")
        self.assertEqual(row["status"], contract.STATUS_FAILED)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["error"], "resolver failed for Bad")
        self.assertIsNone(row["path"])

    def test_ac22_a_failed_track_below_three_attempts_is_retried(self):
        entries = [self.like("sp1", "ArtistB", "Bad", "AlbumY")]
        steps, _ingest, resolve = self.steps(entries, fail_titles=("Bad",))
        library.sync(self.config(), steps=steps)
        self.assertEqual(resolve.calls, [["sp1"]])

        library.sync(self.config(), steps=steps)
        self.assertEqual(resolve.calls, [["sp1"], ["sp1"]])
        row = self.row("sp1")
        self.assertEqual(row["status"], contract.STATUS_FAILED)
        self.assertEqual(row["attempts"], 2)

    def test_ac23_three_attempts_parks_the_track(self):
        entries = [self.like("sp1", "ArtistB", "Bad", "AlbumY")]
        steps, _ingest, resolve = self.steps(entries, fail_titles=("Bad",))
        for _ in range(contract.MAX_ATTEMPTS):
            library.sync(self.config(), steps=steps)

        row = self.row("sp1")
        self.assertEqual(row["status"], contract.STATUS_PARKED)
        self.assertEqual(row["attempts"], contract.MAX_ATTEMPTS)

        library.sync(self.config(), steps=steps)
        self.assertEqual(resolve.calls, [["sp1"], ["sp1"], ["sp1"], []])
        row = self.row("sp1")
        self.assertEqual(row["status"], contract.STATUS_PARKED)
        self.assertEqual(row["attempts"], contract.MAX_ATTEMPTS)


# --- configuration and ordering ------------------------------------------


class TestConfigAndOrder(LibraryTestCase):
    def test_ac24_config_comes_from_the_music_environment_variables(self):
        entries = [self.like("sp1", "ArtistA", "One", "AlbumX")]
        ingest = FakeIngest(entries)
        resolve = FakeResolve()
        env = self.env()
        code, out, _err = self.run_sync_cli(["sync"], ingest, resolve, env)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "added 1, missing 0, failed 0")

        self.assertEqual(len(ingest.calls), 1)
        received = ingest.calls[0]
        self.assertEqual(tuple(received), contract.CONFIG_KEYS)
        expected = {
            key: env[contract.ENV_VARS[key]] for key in contract.CONFIG_KEYS
        }
        self.assertEqual(received, expected)
        self.assertEqual(received["resolver"], "fake-resolver --flag")
        self.assertEqual(received["spotify_export"], str(self.export))

        code, _out, err = self.run_main(
            ["sync"], env={contract.ENV_VARS["state_dir"]: str(self.state_dir)}
        )
        self.assertEqual(code, 2)
        self.assertIn(contract.ENV_VARS["resolver"], err)

        config_file = self.root / "music.json"
        config_file.write_text(json.dumps(self.config()), encoding="utf-8")
        ingest = FakeIngest([])
        code, _out, _err = self.run_sync_cli(
            ["--config", str(config_file), "sync"], ingest, FakeResolve()
        )
        self.assertEqual(code, 0)
        self.assertEqual(tuple(ingest.calls[0]), contract.CONFIG_KEYS)
        self.assertEqual(ingest.calls[0], self.config())

    def test_ac24_usage_error_exits_2(self):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["bogus"], self.env()), 2)
        self.assertIn("bogus", err.getvalue())

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["sync", "--nonsense"], self.env()), 2)
        self.assertIn("--nonsense", err.getvalue())

    def test_ac25_sync_calls_ingest_resolve_scan_in_that_order(self):
        entries = [self.like("sp1", "ArtistA", "One", "AlbumX")]
        order: list[str] = []
        steps, _ingest, _resolve = self.steps(entries)

        def recording(name: str, step):
            def inner(*args, **kwargs):
                order.append(name)
                return step(*args, **kwargs)

            return inner

        wrapped = {
            name: recording(name, steps[name]) for name in contract.SYNC_STEPS
        }
        summary = library.sync(self.config(), steps=wrapped)
        self.assertEqual(tuple(summary), contract.SYNC_SUMMARY_KEYS)
        self.assertEqual(order, ["ingest", "resolve", "scan"])
        self.assertEqual(order, list(contract.SYNC_STEPS))

        order.clear()
        library.sync(self.config(), dry_run=True, steps=wrapped)
        self.assertEqual(order, ["ingest", "resolve", "scan"])
        self.assertEqual(order, list(contract.SYNC_STEPS))


if __name__ == "__main__":
    unittest.main()
