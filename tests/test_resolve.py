"""AC5-AC11: the resolver call, conversion, placement and the failure path.

Audio fixtures are WAV files generated with ``wave``; the resolver is a small
stand-in script in a temporary ``bin`` dir, so every test runs offline.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from music import _contract as contract
from music import convert
from music import resolve as resolve_module
from music import store
from music._contract import LIKES_KEYS, TRACK_RESULT_KEYS

XTAL = {
    "spotify_id": "sp-xtal",
    "artist": "Aphex Twin",
    "title": "Xtal",
    "album": "Selected Ambient Works 85-92",
    "added_at": "2019-05-01T10:00:00Z",
}

ZEBRA = {
    "spotify_id": "sp-zebra",
    "artist": "Aphex Twin",
    "title": "Zebra",
    "album": "Two Tracks",
    "added_at": "2024-01-01T00:00:00Z",
}

APPLE = {
    "spotify_id": "sp-apple",
    "artist": "Aphex Twin",
    "title": "Apple",
    "album": "Two Tracks",
    "added_at": "2024-02-01T00:00:00Z",
}

GOOD = {
    "spotify_id": "sp-good",
    "artist": "Boards of Canada",
    "title": "Roygbiv",
    "album": "Music Has the Right to Children",
    "added_at": "2020-01-01T00:00:00Z",
}

BAD = {
    "spotify_id": "sp-bad",
    "artist": "Boards of Canada",
    "title": "Bad",
    "album": "Music Has the Right to Children",
    "added_at": "2020-02-01T00:00:00Z",
}


def probe_tags(path: Path) -> dict:
    """The format tags ffprobe reads out of ``path``."""
    done = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format_tags",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(done.stdout)["format"].get("tags", {})


class ResolveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="resolve-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.library_root = self.tmp / "library"
        self.media = self.tmp / "media"
        self.media.mkdir()
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.log = self.tmp / "argv.log"
        self.wav = self.make_wav(self.media / "source.wav", seconds=1.0)
        self.set_likes([XTAL])
        self.resolver = self.fake_resolver(
            {XTAL["title"]: (0, str(self.wav), "")}
        )
        self.config = {
            "library_root": str(self.library_root),
            "state_dir": str(self.state_dir),
            "resolver": f"{sys.executable} {self.resolver}",
            "spotify_export": str(self.tmp / "liked.json"),
        }

    # --- fixtures --------------------------------------------------------

    def make_wav(self, path: Path, seconds: float) -> Path:
        rate = 8000
        frames = b"".join(
            struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(rate * seconds))
        )
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(frames)
        return path

    def fake_resolver(self, plan: dict[str, tuple[int, str, str]]) -> Path:
        """A resolver script: per-title exit code, stdout and stderr.

        ``plan`` maps a title to ``(exit code, stdout, stderr)``; the key
        ``"*"`` is the fallback.
        """
        plan = {
            "*": (2, "", "no plan for this track\n"),
            **plan,
        }
        script = self.bin_dir / "resolver"
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "argv = sys.argv[1:]\n"
            f"with open({str(self.log)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(argv) + '\\n')\n"
            f"plan = json.loads({json.dumps(plan)!r})\n"
            "code, out, err = plan.get(argv[1], plan['*'])\n"
            "if err:\n"
            "    sys.stderr.write(err)\n"
            "if out:\n"
            "    sys.stdout.write(out + '\\n')\n"
            "raise SystemExit(code)\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def set_likes(self, entries: list[dict]) -> None:
        (self.state_dir / "likes.json").write_text(
            json.dumps([{key: entry[key] for key in LIKES_KEYS} for entry in entries]),
            encoding="utf-8",
        )

    def recorded_argv(self) -> list[list[str]]:
        if not self.log.is_file():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # --- AC5 -------------------------------------------------------------

    def test_ac5_the_resolver_is_called_with_artist_title_album_positionally(self):
        result = resolve_module.resolve_track(self.config, XTAL)
        self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
        calls = self.recorded_argv()
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0], ["Aphex Twin", "Xtal", "Selected Ambient Works 85-92"]
        )
        self.assertEqual(
            resolve_module.resolver_argv(self.config),
            [sys.executable, str(self.resolver)],
            "the base argv is the shlex-split resolver string",
        )

    # --- AC6 -------------------------------------------------------------

    def test_ac6_an_exit_zero_puts_the_first_stdout_line_to_work(self):
        self.fake_resolver(
            {XTAL["title"]: (0, f"{self.wav}\nthis second line is ignored", "")}
        )
        result = resolve_module.resolve_track(self.config, XTAL)
        self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["source"], str(self.wav))
        placed = Path(result["path"])
        self.assertTrue(placed.is_file())
        self.assertEqual(placed.name, "01 - Xtal.m4a")
        self.assertEqual(
            placed, self.library_root / "Aphex Twin" / "Selected Ambient Works 85-92" / placed.name
        )

    # --- AC7 -------------------------------------------------------------

    def test_ac7_the_m4a_carries_the_five_tags(self):
        result = resolve_module.resolve_track(self.config, XTAL)
        self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
        placed = Path(result["path"])
        self.assertEqual(placed.suffix, ".m4a")
        codec = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(placed),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(codec, "aac")

        tags = probe_tags(placed)
        self.assertEqual(tags.get("artist"), "Aphex Twin")
        self.assertEqual(tags.get("title"), "Xtal")
        self.assertEqual(tags.get("album"), "Selected Ambient Works 85-92")
        self.assertEqual(tags.get("track"), "1")
        self.assertEqual(tags.get("year"), "2019")

    def test_ac7_an_empty_tag_value_is_not_written(self):
        written = self.tmp / "tags" / "empty.m4a"
        convert.convert_to_m4a(self.config, self.wav, written)
        calls: list[list[str]] = []
        real_run = convert._run

        def recording_run(argv: list[str]) -> str:
            calls.append(list(argv))
            return real_run(argv)

        with mock.patch.object(convert, "_run", side_effect=recording_run):
            convert.write_tags(
                written,
                {
                    "artist": "Aphex Twin",
                    "title": "",
                    "album": "Selected Ambient Works 85-92",
                    "track": "1",
                    "year": "2019",
                },
            )
        arguments = [item for call in calls for item in call]
        self.assertFalse(
            [item for item in arguments if item.startswith("title=")],
            "the empty value never reaches the tool",
        )
        tags = probe_tags(written)
        self.assertEqual(tags.get("artist"), "Aphex Twin")
        self.assertNotIn("title", tags, "an empty value writes no tag at all")
        self.assertEqual(tags.get("track"), "1")
        self.assertEqual(tags.get("year"), "2019")

    # --- AC8 -------------------------------------------------------------

    def test_ac8_two_tracks_of_one_album_are_numbered_by_added_at_then_title(self):
        self.set_likes([ZEBRA, APPLE])
        self.fake_resolver(
            {
                ZEBRA["title"]: (0, str(self.wav), ""),
                APPLE["title"]: (0, str(self.wav), ""),
            }
        )
        # Resolve in the opposite order to the placement order: the number must
        # come from added_at/title, not from the order of the calls.
        apple_result = resolve_module.resolve_track(self.config, APPLE)
        zebra_result = resolve_module.resolve_track(self.config, ZEBRA)
        for result in (apple_result, zebra_result):
            self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
            self.assertTrue(result["ok"])

        album_dir = self.library_root / "Aphex Twin" / "Two Tracks"
        placed = sorted(path.name for path in album_dir.iterdir() if path.suffix == ".m4a")
        self.assertEqual(
            placed,
            ["01 - Zebra.m4a", "02 - Apple.m4a"],
            "added_at 2024-01-01 Zebra is 01 before the earlier title Apple",
        )
        self.assertEqual(Path(zebra_result["path"]).name, "01 - Zebra.m4a")
        self.assertEqual(Path(apple_result["path"]).name, "02 - Apple.m4a")

    # --- AC9 -------------------------------------------------------------

    def test_ac9_one_album_folder_holds_exactly_one_jpeg_cover(self):
        self.set_likes([ZEBRA, APPLE])
        self.fake_resolver(
            {
                ZEBRA["title"]: (0, str(self.wav), ""),
                APPLE["title"]: (0, str(self.wav), ""),
            }
        )
        for track in (ZEBRA, APPLE):
            result = resolve_module.resolve_track(self.config, track)
            self.assertTrue(result["ok"], result["error"])

        album_dir = self.library_root / "Aphex Twin" / "Two Tracks"
        covers = [path for path in album_dir.iterdir() if path.name == "cover.jpg"]
        self.assertEqual(len(covers), 1, "exactly one cover.jpg")
        cover = covers[0]
        data = cover.read_bytes()
        self.assertTrue(data.startswith(b"\xff\xd8\xff"), "JPEG magic")
        self.assertTrue(data.endswith(b"\xff\xd9"), "JPEG end marker")
        self.assertTrue(convert.is_jpeg(cover))
        self.assertEqual(
            [path.name for path in album_dir.iterdir() if path.suffix.lower() in {".jpg", ".jpeg"}],
            ["cover.jpg"],
        )
        # A second ensure_cover run keeps the same, single file.
        self.assertEqual(convert.ensure_cover(album_dir), cover)

    def test_ac9_is_jpeg_needs_both_magic_bytes(self):
        neither = self.tmp / "neither.jpg"
        neither.write_bytes(b"this is not an image at all")
        self.assertFalse(convert.is_jpeg(neither))

        starts_only = self.tmp / "starts_only.jpg"
        starts_only.write_bytes(b"\xff\xd8\xff" + b"payload without an end marker")
        self.assertFalse(
            convert.is_jpeg(starts_only),
            "starting like a JPEG is not enough",
        )

        both = self.tmp / "both.jpg"
        both.write_bytes(b"\xff\xd8\xff" + b"payload" + b"\xff\xd9")
        self.assertTrue(convert.is_jpeg(both))

    def test_ac9_is_jpeg_rejects_a_file_that_only_ends_like_one(self):
        ends_only = self.tmp / "ends_only.jpg"
        ends_only.write_bytes(b"not an image" + b"\xff\xd9")
        self.assertFalse(
            convert.is_jpeg(ends_only),
            "ending like a JPEG is not enough",
        )

    # --- AC10 ------------------------------------------------------------

    def test_ac10_the_duration_ffprobe_reports_for_the_m4a_is_recorded(self):
        written = self.tmp / "probe" / "in.m4a"
        convert.convert_to_m4a(self.config, self.wav, written)
        self.assertTrue(written.is_file())

        source_seconds = convert.probe_duration(self.wav)
        written_seconds = convert.probe_duration(written)
        self.assertAlmostEqual(source_seconds, 1.0, delta=0.05)
        self.assertAlmostEqual(written_seconds, source_seconds, delta=0.12)
        self.assertEqual(
            convert.recorded_duration(written),
            written_seconds,
            "convert_to_m4a records what ffprobe reported for the file it wrote",
        )

        result = resolve_module.resolve_track(self.config, XTAL)
        self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
        placed_seconds = convert.recorded_duration(Path(result["path"]))
        self.assertIsNotNone(placed_seconds, "the placed .m4a has a recorded duration")
        self.assertAlmostEqual(placed_seconds, convert.probe_duration(Path(result["path"])), delta=0.001)

    def test_ac10_result_carries_the_duration_ffprobe_reports(self):
        two_seconds = self.make_wav(self.media / "two-seconds.wav", seconds=2.0)
        self.set_likes([XTAL])
        self.fake_resolver({XTAL["title"]: (0, str(two_seconds), "")})

        result = resolve_module.resolve_track(self.config, XTAL)
        self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
        self.assertTrue(result["ok"], result["error"])
        self.assertIsInstance(result["duration"], float)
        self.assertGreaterEqual(result["duration"], 1.9)
        self.assertLessEqual(result["duration"], 2.2)
        self.assertAlmostEqual(
            result["duration"],
            convert.probe_duration(Path(result["path"])),
            delta=0.001,
        )

        # A second resolve of the same track finds the file gone and converts
        # again; the duration still has to cross the seam.  (A destination that
        # still holds a file is refused instead — AC37.)
        Path(result["path"]).unlink()
        convert.DURATIONS.clear()
        again = resolve_module.resolve_track(self.config, XTAL)
        self.assertEqual(tuple(again), TRACK_RESULT_KEYS)
        self.assertTrue(again["ok"], again["error"])
        self.assertIsInstance(again["duration"], float)
        self.assertGreaterEqual(again["duration"], 1.9)
        self.assertLessEqual(again["duration"], 2.2)

    def test_ac10_a_silent_tool_failure_names_the_exit_code(self):
        with self.assertRaises(RuntimeError) as caught:
            convert._run(["sh", "-c", "exit 3"])
        text = str(caught.exception)
        self.assertIn(
            "exit 3",
            text,
            "a tool that fails and says nothing is named by its exit code",
        )

    # --- AC11 ------------------------------------------------------------

    def test_ac11_a_non_zero_exit_yields_a_failed_result_with_stderr_and_the_run_goes_on(self):
        self.set_likes([BAD, GOOD])
        self.fake_resolver(
            {
                BAD["title"]: (3, "", "resolver could not find Bad\n"),
                GOOD["title"]: (0, str(self.wav), ""),
            }
        )
        results = resolve_module.resolve_pending(self.config, [BAD, GOOD])
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertEqual(tuple(result), TRACK_RESULT_KEYS)

        failed, placed = results
        self.assertEqual(failed["spotify_id"], BAD["spotify_id"])
        self.assertFalse(failed["ok"])
        self.assertIsNone(failed["path"])
        self.assertIsNone(failed["source"])
        self.assertEqual(failed["error"], "resolver could not find Bad")

        self.assertEqual(placed["spotify_id"], GOOD["spotify_id"])
        self.assertTrue(placed["ok"], placed["error"])
        self.assertTrue(Path(placed["path"]).is_file())
        self.assertEqual(len(self.recorded_argv()), 2, "the loop continued")

    # --- AC34 ------------------------------------------------------------

    def test_ac34_a_symlink_at_the_destination_is_refused_and_nothing_lands_outside(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        target = outside / "target.m4a"
        target.write_bytes(b"outside-bytes")

        xtal_dir = self.library_root / "Aphex Twin" / "Selected Ambient Works 85-92"
        xtal_dir.mkdir(parents=True)
        (xtal_dir / "01 - Xtal.m4a").symlink_to(target)

        good_dir = (
            self.library_root
            / "Boards of Canada"
            / "Music Has the Right to Children"
        )
        good_dir.mkdir(parents=True)
        dangling = outside / "never-written.m4a"
        (good_dir / "01 - Roygbiv.m4a").symlink_to(dangling)

        self.set_likes([XTAL, GOOD])
        self.fake_resolver(
            {
                XTAL["title"]: (0, str(self.wav), ""),
                GOOD["title"]: (0, str(self.wav), ""),
            }
        )

        results = resolve_module.resolve_pending(self.config, [XTAL, GOOD])
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
            self.assertFalse(result["ok"], "a symlinked destination is refused")
            self.assertIsNone(result["path"])
            self.assertIn("destination", result["error"])

        self.assertEqual(len(self.recorded_argv()), 2, "the resolver ran anyway")
        self.assertEqual(target.read_bytes(), b"outside-bytes", "the link target is untouched")
        self.assertFalse(
            dangling.exists(),
            "a dangling symlink is not created through",
        )
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["target.m4a"])
        self.assertTrue((xtal_dir / "01 - Xtal.m4a").is_symlink())
        self.assertTrue((good_dir / "01 - Roygbiv.m4a").is_symlink())
        self.assertEqual(sorted(path.name for path in xtal_dir.iterdir()), ["01 - Xtal.m4a"])
        self.assertEqual(
            sorted(path.name for path in good_dir.iterdir()), ["01 - Roygbiv.m4a"]
        )

    # --- AC35 ------------------------------------------------------------

    def test_ac35_a_symlinked_album_component_is_not_followed_and_the_track_fails(self):
        outside = self.tmp / "outside-albums"
        outside.mkdir()
        self.library_root.mkdir(parents=True, exist_ok=True)
        (self.library_root / "Aphex Twin").symlink_to(outside)

        result = resolve_module.resolve_track(self.config, XTAL)
        self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["path"])
        self.assertIn("symlink", result["error"])
        self.assertEqual(
            self.recorded_argv(),
            [["Aphex Twin", "Xtal", "Selected Ambient Works 85-92"]],
            "the resolver is called before the destination is refused",
        )
        self.assertEqual(
            list(outside.iterdir()), [], "nothing was placed through the symlink"
        )

        conn = store.open_index(self.config)
        try:
            store.upsert_track(conn, {key: XTAL[key] for key in LIKES_KEYS})
            store.record_result(conn, result)
            row = store.get_track(conn, XTAL["spotify_id"])
        finally:
            conn.close()
        self.assertEqual(row["status"], contract.STATUS_FAILED)
        self.assertEqual(row["attempts"], 1)
        self.assertIn("symlink", row["error"])
        self.assertIsNone(row["path"])

    # --- AC36 ------------------------------------------------------------

    def test_ac36_a_symlinked_cover_is_replaced_by_the_albums_own_regular_cover(self):
        outside = self.tmp / "outside-art"
        outside.mkdir()
        target = outside / "artwork.jpg"
        original = b"\xff\xd8\xff" + b"artwork bytes" + b"\xff\xd9"
        target.write_bytes(original)

        album_dir = (
            self.library_root / "Aphex Twin" / "Selected Ambient Works 85-92"
        )
        album_dir.mkdir(parents=True)
        (album_dir / "cover.jpg").symlink_to(target)

        result = resolve_module.resolve_track(self.config, XTAL)
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(Path(result["path"]).parent, album_dir)

        cover = album_dir / "cover.jpg"
        self.assertFalse(cover.is_symlink(), "the album has a cover.jpg of its own")
        self.assertTrue(cover.is_file())
        self.assertTrue(convert.is_jpeg(cover), "a regular JPEG in the album")
        self.assertEqual(
            target.read_bytes(), original, "the link's target keeps its bytes"
        )
        self.assertEqual(
            [path.name for path in album_dir.iterdir() if path.name == "cover.jpg"],
            ["cover.jpg"],
        )

    # --- AC37 ------------------------------------------------------------

    def test_ac37_a_file_at_the_destination_is_not_reported_as_the_fetched_track(self):
        album_dir = (
            self.library_root / "Aphex Twin" / "Selected Ambient Works 85-92"
        )
        album_dir.mkdir(parents=True)
        destination = album_dir / "01 - Xtal.m4a"
        destination.write_bytes(b"someone else's file")

        result = resolve_module.resolve_track(self.config, XTAL)
        self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
        self.assertEqual(
            len(self.recorded_argv()), 1, "the resolver is called anyway"
        )
        self.assertFalse(result["ok"], "the pre-existing file never yields ok")
        self.assertIsNone(result["path"])
        self.assertIn("destination", result["error"])
        self.assertEqual(destination.read_bytes(), b"someone else's file")
        self.assertEqual(
            sorted(path.name for path in album_dir.iterdir()),
            ["01 - Xtal.m4a"],
            "nothing was added beside the pre-existing file",
        )

        conn = store.open_index(self.config)
        try:
            store.upsert_track(conn, {key: XTAL[key] for key in LIKES_KEYS})
            store.record_result(conn, result)
            row = store.get_track(conn, XTAL["spotify_id"])
        finally:
            conn.close()
        self.assertEqual(row["status"], contract.STATUS_FAILED)
        self.assertNotEqual(row["status"], contract.STATUS_OK)
        self.assertEqual(row["path"], None)
    # --- AC38 ------------------------------------------------------------

    def test_ac38_tracks_that_differ_only_in_a_rewritten_character_keep_distinct_paths(self):
        slash = {
            "spotify_id": "sp-slash",
            "artist": "AC/DC",
            "title": "Back In Black",
            "album": "Live",
            "added_at": "2024-01-01T00:00:00Z",
        }
        # "AC/DC" and "AC_DC" differ only in a character ``sanitize`` rewrites
        # to ``_``: the second track used to land on the first track's file.
        underscore = {
            "spotify_id": "sp-underscore",
            "artist": "AC_DC",
            "title": "Back In Black",
            "album": "Live",
            "added_at": "2024-01-02T00:00:00Z",
        }
        self.set_likes([slash, underscore])
        self.fake_resolver({slash["title"]: (0, str(self.wav), "")})

        slash_result = resolve_module.resolve_track(self.config, slash)
        underscore_result = resolve_module.resolve_track(self.config, underscore)
        for result in (slash_result, underscore_result):
            self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
            self.assertTrue(result["ok"], result["error"])
        self.assertNotEqual(
            slash_result["path"],
            underscore_result["path"],
            "AC/DC and AC_DC are two names, so two files",
        )
        self.assertNotEqual(
            resolve_module.album_dir_for(self.config, slash),
            resolve_module.album_dir_for(self.config, underscore),
        )
        for track, result in (
            (slash, slash_result),
            (underscore, underscore_result),
        ):
            placed = Path(result["path"])
            self.assertTrue(placed.is_file())
            self.assertEqual(
                probe_tags(placed).get("artist"),
                track["artist"],
                "neither track claims the other's file",
            )

        # The title is a rewritten component too, at one fixed album position.
        one = dict(slash, title="Rock/N/Roll")
        two = dict(slash, spotify_id="sp-underscore-title", title="Rock_N_Roll")
        self.assertNotEqual(
            resolve_module.sanitize(one["title"]),
            resolve_module.sanitize(two["title"]),
            "one name encodes to _u2f_, the other keeps its underscore",
        )
        self.assertNotEqual(
            resolve_module.library_path(self.config, one, 1),
            resolve_module.library_path(self.config, two, 1),
            "a rewritten title character yields a distinct file name",
        )
        self.assertEqual(
            len(
                {
                    resolve_module.sanitize("AC/DC"),
                    resolve_module.sanitize("AC_DC"),
                    resolve_module.sanitize("AC-DC"),
                    resolve_module.sanitize("AC\\\\DC"),
                }
            ),
            4,
            "four spellings sanitize to four different components",
        )

    def test_ac38_an_unavoidable_collision_fails_the_later_track_instead_of_taking_the_file(self):
        first = {
            "spotify_id": "sp-twin-a",
            "artist": "Aphex Twin",
            "title": "Xtal",
            "album": "Selected Ambient Works 85-92",
            "added_at": "2024-01-01T00:00:00Z",
        }
        # Two different tracks whose artist, title and album are identical: no
        # spelling of the name can tell them apart, and both like entries give
        # the same album position.
        second = dict(first, spotify_id="sp-twin-b")
        self.set_likes([second])
        self.fake_resolver({first["title"]: (0, str(self.wav), "")})

        placed = resolve_module.resolve_track(self.config, first)
        self.assertEqual(tuple(placed), TRACK_RESULT_KEYS)
        self.assertTrue(placed["ok"], placed["error"])
        taken = Path(placed["path"])
        self.assertTrue(taken.is_file())
        before = taken.read_bytes()

        later = resolve_module.resolve_track(self.config, second)
        self.assertEqual(tuple(later), TRACK_RESULT_KEYS)
        self.assertEqual(later["spotify_id"], second["spotify_id"])
        self.assertFalse(later["ok"], "the later track never claims the file")
        self.assertIsNone(later["path"])
        self.assertIsNone(later["source"])
        self.assertIsNone(later["duration"])
        # The reason names the refusal, not the word "collision": the destination guard of AC37 answers
        # this case and its message names the path. The behaviour AC38 asks for is the line above —
        # the later track claims nothing and the first track's file is untouched.
        self.assertIn("refusing", later["error"])
        self.assertIn(str(taken), later["error"])
        self.assertEqual(taken.read_bytes(), before, "the first track's file is untouched")
        self.assertEqual(
            sorted(path.name for path in taken.parent.iterdir() if path.suffix == ".m4a"),
            [taken.name],
            "the later track added no file of its own",
        )
        self.assertEqual(len(self.recorded_argv()), 2, "the resolver ran for both")

    # --- the double ------------------------------------------------------

    def test_fake_resolver_is_offline_and_reports_a_miss_as_a_failure(self):
        sources = {
            "\0".join((XTAL["artist"], XTAL["title"], XTAL["album"])): self.wav,
            "\0".join((GOOD["artist"], GOOD["title"], GOOD["album"])): {
                "path": self.wav,
                "spotify_id": GOOD["spotify_id"],
            },
        }
        fake = resolve_module.FakeResolver(self.config, sources)
        hit = fake.resolve(XTAL["artist"], XTAL["title"], XTAL["album"])
        self.assertEqual(tuple(hit), TRACK_RESULT_KEYS)
        self.assertTrue(hit["ok"])
        self.assertEqual(hit["path"], str(self.wav))
        self.assertEqual(hit["source"], str(self.wav))
        self.assertFalse(
            self.library_root.exists(), "the double never touches library_root"
        )

        second = fake.resolve(GOOD["artist"], GOOD["title"], GOOD["album"])
        self.assertEqual(tuple(second), TRACK_RESULT_KEYS)
        self.assertTrue(second["ok"])
        self.assertEqual(second["spotify_id"], GOOD["spotify_id"])

        miss = fake.resolve("Nobody", "Nothing", "Nowhere")
        self.assertEqual(tuple(miss), TRACK_RESULT_KEYS)
        self.assertFalse(miss["ok"])
        self.assertIsNone(miss["path"])
        self.assertTrue(miss["error"])
        self.assertEqual(resolve_module.FakeResolver(self.config).resolve("A", "T", "Al")["ok"], False)


class TestFakeResolverCommand(unittest.TestCase):
    """AC40: ``MUSIC_RESOLVER=fake`` answers without a download tool."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fake-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.library_root = self.tmp / "library"
        self.config = {
            "library_root": str(self.library_root),
            "state_dir": str(self.tmp / "state"),
            "resolver": "fake",
            "spotify_export": str(self.tmp / "liked.json"),
        }

    def test_ac40_the_fake_resolver_places_a_tone(self):
        result = resolve_module.resolve_track(self.config, dict(XTAL))
        self.assertEqual(tuple(result), TRACK_RESULT_KEYS)
        self.assertTrue(result["ok"], result["error"])
        placed = Path(result["path"])
        self.assertTrue(placed.is_file())
        self.assertEqual(placed.suffix, ".m4a")
        self.assertTrue(placed.is_relative_to(self.library_root))
        self.assertGreater(result["duration"] or 0, 0)


class TestParallelResolve(unittest.TestCase):
    """AC42: at most three resolver calls at once, results in input order."""

    SLEEP = 0.3
    COUNT = 6

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="parallel-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.library_root = self.tmp / "library"
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.log = self.tmp / "calls.jsonl"
        self.wav = self.tmp / "source.wav"
        ResolveTestCase.make_wav(self, self.wav, seconds=1.0)
        self.tracks = [
            {
                "spotify_id": f"sp-{index}",
                "artist": "Aphex Twin",
                "title": f"Track {index}",
                "album": "Selected Ambient Works 85-92",
                "added_at": f"2024-01-0{index + 1}T00:00:00Z",
            }
            for index in range(self.COUNT)
        ]
        (self.state_dir / "likes.json").write_text(
            json.dumps(self.tracks), encoding="utf-8"
        )
        resolver = self.bin_dir / "resolver"
        resolver.write_text(
            f"#!{sys.executable}\n"
            "import json, sys, time\n"
            "start = time.time()\n"
            f"time.sleep({self.SLEEP})\n"
            f"with open({str(self.log)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps({'start': start, 'end': time.time()}) + '\\n')\n"
            f"print({str(self.wav)!r})\n",
            encoding="utf-8",
        )
        resolver.chmod(0o755)
        self.config = {
            "library_root": str(self.library_root),
            "state_dir": str(self.state_dir),
            "resolver": str(resolver),
            "spotify_export": str(self.tmp / "liked.json"),
        }

    def overlap(self) -> int:
        calls = [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]
        return max(
            sum(1 for call in calls if call["start"] <= other["start"] < call["end"])
            for other in calls
        )

    def test_ac42_at_most_three_calls_run_at_once(self):
        results = resolve_module.resolve_pending(self.config, self.tracks)
        self.assertEqual(len(results), self.COUNT)
        self.assertEqual(
            [result["spotify_id"] for result in results],
            [track["spotify_id"] for track in self.tracks],
            "the results keep the input order",
        )
        self.assertTrue(all(result["ok"] for result in results))
        peak = self.overlap()
        self.assertLessEqual(peak, resolve_module.MAX_WORKERS)
        self.assertGreater(peak, 1, "the calls really run in parallel")

    def test_ac42_one_worker_runs_alone(self):
        with mock.patch.dict(os.environ, {resolve_module.CONCURRENCY_VAR: "1"}):
            resolve_module.resolve_pending(self.config, self.tracks[:2])
        self.assertEqual(self.overlap(), 1)

    def test_ac42_workers_reads_the_variable_and_caps_it(self):
        self.assertEqual(resolve_module.workers({}), resolve_module.DEFAULT_WORKERS)
        self.assertEqual(resolve_module.workers({resolve_module.CONCURRENCY_VAR: "1"}), 1)
        self.assertEqual(resolve_module.workers({resolve_module.CONCURRENCY_VAR: "2"}), 2)
        self.assertEqual(
            resolve_module.workers({resolve_module.CONCURRENCY_VAR: "99"}),
            resolve_module.MAX_WORKERS,
            "the ceiling holds whatever the variable says",
        )
        self.assertEqual(resolve_module.workers({resolve_module.CONCURRENCY_VAR: "0"}), 1)
        self.assertEqual(
            resolve_module.workers({resolve_module.CONCURRENCY_VAR: "nonsense"}),
            resolve_module.DEFAULT_WORKERS,
        )


if __name__ == "__main__":
    unittest.main()
