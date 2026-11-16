"""Contract tests: one test per name declared in ``music._contract``.

Each test asserts three things and nothing less:
1. ``inspect.signature`` matches the declared parameters exactly — names,
   order, defaults and annotations (``self`` included for methods);
2. calling the name raises ``NotImplementedError`` (declaration only);
3. for a name returning a dict, the ``*_KEYS`` tuple exists and holds the
   documented keys in the documented order.
"""

import inspect
import sqlite3
import unittest
from pathlib import Path

from music import _contract as contract

# Namespace in which the expected signatures are written out literally, so the
# expectation does not come from the module under test.
_TYPES = {
    "Path": Path,
    "sqlite3": sqlite3,
}


def _expected(spec: str) -> inspect.Signature:
    """Build the expected ``inspect.Signature`` from a literal def spec."""
    params, _, returns = spec.partition(" -> ")
    namespace = dict(_TYPES)
    exec(f"def _f({params.strip('()')}) -> {returns}:\n    pass\n", namespace)
    return inspect.signature(namespace["_f"])


class ContractTestCase(unittest.TestCase):
    def assertSignature(self, obj, spec: str) -> None:
        self.assertEqual(
            inspect.signature(obj),
            _expected(spec),
            f"{getattr(obj, '__qualname__', obj)} signature drifted",
        )

    def assertNotImplemented(self, fn, *args, **kwargs) -> None:
        with self.assertRaises(NotImplementedError):
            fn(*args, **kwargs)

    def assertKeys(self, name: str, expected: tuple) -> None:
        self.assertTrue(
            hasattr(contract, name), f"music._contract.{name} is missing"
        )
        keys = getattr(contract, name)
        self.assertIsInstance(keys, tuple)
        self.assertEqual(tuple(keys), tuple(expected))


class TestVocabulary(ContractTestCase):
    def test_LIKES_KEYS(self) -> None:
        self.assertKeys(
            "LIKES_KEYS",
            ("spotify_id", "artist", "title", "album", "added_at"),
        )

    def test_TRACK_COLUMNS(self) -> None:
        self.assertKeys(
            "TRACK_COLUMNS",
            (
                "spotify_id",
                "artist",
                "title",
                "album",
                "path",
                "status",
                "attempts",
                "error",
                "added_at",
            ),
        )

    def test_CONFIG_KEYS(self) -> None:
        self.assertKeys(
            "CONFIG_KEYS",
            ("library_root", "state_dir", "resolver", "spotify_export"),
        )

    def test_ENV_VARS(self) -> None:
        self.assertEqual(
            contract.ENV_VARS,
            {
                "library_root": "MUSIC_LIBRARY_ROOT",
                "state_dir": "MUSIC_STATE_DIR",
                "resolver": "MUSIC_RESOLVER",
                "spotify_export": "MUSIC_SPOTIFY_EXPORT",
            },
        )
        self.assertEqual(tuple(contract.ENV_VARS), contract.CONFIG_KEYS)

    def test_SYNC_STEPS(self) -> None:
        self.assertKeys("SYNC_STEPS", ("ingest", "resolve", "scan"))

    def test_STATUS_PENDING(self) -> None:
        self.assertEqual(contract.STATUS_PENDING, "pending")

    def test_STATUS_OK(self) -> None:
        self.assertEqual(contract.STATUS_OK, "ok")

    def test_STATUS_FAILED(self) -> None:
        self.assertEqual(contract.STATUS_FAILED, "failed")

    def test_STATUS_PARKED(self) -> None:
        self.assertEqual(contract.STATUS_PARKED, "parked")

    def test_STATUS_VALUES_ARE_DECLARED_SET(self) -> None:
        self.assertEqual(
            {
                contract.STATUS_PENDING,
                contract.STATUS_OK,
                contract.STATUS_FAILED,
                contract.STATUS_PARKED,
            },
            {"pending", "ok", "failed", "parked"},
        )

    def test_MAX_ATTEMPTS(self) -> None:
        self.assertEqual(contract.MAX_ATTEMPTS, 3)

    def test_INGEST_RESULT_KEYS(self) -> None:
        self.assertKeys("INGEST_RESULT_KEYS", ("total", "added", "likes"))

    def test_TRACK_RESULT_KEYS(self) -> None:
        self.assertKeys(
            "TRACK_RESULT_KEYS",
            ("spotify_id", "ok", "path", "error", "source", "duration"),
        )

    def test_SCAN_RESULT_KEYS(self) -> None:
        self.assertKeys("SCAN_RESULT_KEYS", ("indexed", "missing"))

    def test_SYNC_SUMMARY_KEYS(self) -> None:
        self.assertKeys("SYNC_SUMMARY_KEYS", ("added", "missing", "failed"))

    def test_REPORT_ROW_KEYS(self) -> None:
        self.assertKeys(
            "REPORT_ROW_KEYS", ("artist", "album", "tracks", "seconds")
        )


class TestConfigAndSpotify(ContractTestCase):
    def test_load_config(self) -> None:
        self.assertSignature(
            contract.load_config,
            "path: str | None = None, env: dict[str, str] | None = None"
            " -> dict",
        )
        self.assertKeys(
            "CONFIG_KEYS",
            ("library_root", "state_dir", "resolver", "spotify_export"),
        )
        self.assertNotImplemented(contract.load_config)
        self.assertNotImplemented(
            contract.load_config, "/tmp/music.json", {"MUSIC_RESOLVER": "x"}
        )

    def test_find_spotify_cli(self) -> None:
        self.assertSignature(contract.find_spotify_cli, "() -> str | None")
        self.assertNotImplemented(contract.find_spotify_cli)

    def test_parse_spotify_cli(self) -> None:
        self.assertSignature(
            contract.parse_spotify_cli, "stdout: str -> list[dict]"
        )
        self.assertNotImplemented(contract.parse_spotify_cli, "[]")

    def test_parse_export(self) -> None:
        self.assertSignature(
            contract.parse_export, "path: Path -> list[dict]"
        )
        self.assertNotImplemented(contract.parse_export, Path("liked.json"))


class TestIngest(ContractTestCase):
    def test_ingest(self) -> None:
        self.assertSignature(contract.ingest, "config: dict -> dict")
        self.assertKeys("INGEST_RESULT_KEYS", ("total", "added", "likes"))
        self.assertNotImplemented(contract.ingest, {})


class TestResolver(ContractTestCase):
    def test_resolver_argv(self) -> None:
        self.assertSignature(
            contract.resolver_argv, "config: dict -> list[str]"
        )
        self.assertNotImplemented(contract.resolver_argv, {})

    def test_resolve_track(self) -> None:
        self.assertSignature(
            contract.resolve_track, "config: dict, track: dict -> dict"
        )
        self.assertKeys(
            "TRACK_RESULT_KEYS",
            ("spotify_id", "ok", "path", "error", "source", "duration"),
        )
        self.assertNotImplemented(contract.resolve_track, {}, {})

    def test_resolve_pending(self) -> None:
        self.assertSignature(
            contract.resolve_pending,
            "config: dict, tracks: list[dict] -> list[dict]",
        )
        self.assertKeys(
            "TRACK_RESULT_KEYS",
            ("spotify_id", "ok", "path", "error", "source", "duration"),
        )
        self.assertNotImplemented(contract.resolve_pending, {}, [])

    def test_convert_to_m4a(self) -> None:
        self.assertSignature(
            contract.convert_to_m4a,
            "config: dict, src: Path, dst: Path -> None",
        )
        self.assertNotImplemented(
            contract.convert_to_m4a, {}, Path("in.flac"), Path("out.m4a")
        )

    def test_probe_duration(self) -> None:
        self.assertSignature(contract.probe_duration, "path: Path -> float")
        self.assertNotImplemented(contract.probe_duration, Path("out.m4a"))

    def test_write_tags(self) -> None:
        self.assertSignature(
            contract.write_tags, "path: Path, tags: dict[str, str] -> None"
        )
        self.assertNotImplemented(
            contract.write_tags, Path("out.m4a"), {"artist": "A"}
        )

    def test_ensure_cover(self) -> None:
        self.assertSignature(
            contract.ensure_cover,
            "album_dir: Path, cover_src: str | None = None -> Path",
        )
        self.assertNotImplemented(contract.ensure_cover, Path("Artist/Album"))
        self.assertNotImplemented(
            contract.ensure_cover, Path("Artist/Album"), "https://x/cover.jpg"
        )

    def test_FakeResolver(self) -> None:
        self.assertSignature(
            contract.FakeResolver,
            "config: dict, sources: dict[str, Path] | None = None -> None",
        )
        self.assertNotImplemented(contract.FakeResolver, {})
        self.assertNotImplemented(
            contract.FakeResolver, {}, {"artist\0title\0album": Path("a.mp3")}
        )

    def test_FakeResolver___init__(self) -> None:
        self.assertSignature(
            contract.FakeResolver.__init__,
            "self, config: dict, sources: dict[str, Path] | None = None"
            " -> None",
        )
        self.assertNotImplemented(
            contract.FakeResolver.__init__, object.__new__(contract.FakeResolver), {}
        )
        self.assertNotImplemented(
            contract.FakeResolver.__init__,
            object.__new__(contract.FakeResolver),
            {},
            {"artist\0title\0album": Path("a.mp3")},
        )

    def test_FakeResolver_resolve(self) -> None:
        self.assertSignature(
            contract.FakeResolver.resolve,
            "self, artist: str, title: str, album: str -> dict",
        )
        self.assertKeys(
            "TRACK_RESULT_KEYS",
            ("spotify_id", "ok", "path", "error", "source", "duration"),
        )
        uninitialised = contract.FakeResolver.__new__(contract.FakeResolver)
        self.assertNotImplemented(uninitialised.resolve, "A", "T", "Al")


class TestIndex(ContractTestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_open_index(self) -> None:
        self.assertSignature(
            contract.open_index, "config: dict -> sqlite3.Connection"
        )
        self.assertNotImplemented(contract.open_index, {})

    def test_init_index(self) -> None:
        self.assertSignature(
            contract.init_index, "conn: sqlite3.Connection -> None"
        )
        self.assertNotImplemented(contract.init_index, self.conn)

    def test_upsert_track(self) -> None:
        self.assertSignature(
            contract.upsert_track,
            "conn: sqlite3.Connection, track: dict -> None",
        )
        self.assertNotImplemented(contract.upsert_track, self.conn, {})

    def test_get_track(self) -> None:
        self.assertSignature(
            contract.get_track,
            "conn: sqlite3.Connection, spotify_id: str -> dict | None",
        )
        self.assertNotImplemented(contract.get_track, self.conn, "id")

    def test_pending_tracks(self) -> None:
        self.assertSignature(
            contract.pending_tracks,
            "conn: sqlite3.Connection, likes: list[dict] -> list[dict]",
        )
        self.assertNotImplemented(contract.pending_tracks, self.conn, [])

    def test_record_result(self) -> None:
        self.assertSignature(
            contract.record_result,
            "conn: sqlite3.Connection, result: dict -> None",
        )
        self.assertNotImplemented(contract.record_result, self.conn, {})

    def test_scan(self) -> None:
        self.assertSignature(
            contract.scan, "config: dict, conn: sqlite3.Connection -> dict"
        )
        self.assertKeys("SCAN_RESULT_KEYS", ("indexed", "missing"))
        self.assertNotImplemented(contract.scan, {}, self.conn)

    def test_report_rows(self) -> None:
        self.assertSignature(
            contract.report_rows,
            "conn: sqlite3.Connection -> list[dict]",
        )
        self.assertKeys(
            "REPORT_ROW_KEYS", ("artist", "album", "tracks", "seconds")
        )
        self.assertNotImplemented(contract.report_rows, self.conn)


class TestOrchestration(ContractTestCase):
    def test_sync(self) -> None:
        self.assertSignature(
            contract.sync,
            "config: dict, dry_run: bool = False, steps: dict | None = None"
            " -> dict",
        )
        self.assertKeys(
            "SYNC_SUMMARY_KEYS", ("added", "missing", "failed")
        )
        self.assertNotImplemented(contract.sync, {})
        self.assertNotImplemented(contract.sync, {}, True, {"ingest": None})

    def test_main(self) -> None:
        self.assertSignature(
            contract.main,
            "argv: list[str] | None = None, env: dict[str, str] | None = None"
            " -> int",
        )
        self.assertNotImplemented(contract.main)
        self.assertNotImplemented(contract.main, ["sync"], {})


if __name__ == "__main__":
    unittest.main()
