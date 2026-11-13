"""Tests for resolving, converting, tagging and placing tracks (offline, real ffmpeg)."""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

from music import convert, ingest, resolve
from music.resolve import ResolveResult

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

TRACKS = [
    {
        "spotify_id": "t-alpha",
        "artist": "Nova",
        "title": "Alpha",
        "album": "First Light",
        "added_at": "2024-01-02T03:04:05Z",
    },
    {
        "spotify_id": "t-beta",
        "artist": "Nova",
        "title": "Beta",
        "album": "First Light",
        "added_at": "2024-01-03T00:00:00Z",
    },
    {
        "spotify_id": "t-gamma",
        "artist": "Nova",
        "title": "Gamma",
        "album": "Second Sky",
        "added_at": "2024-02-04T00:00:00Z",
    },
]


class StaticResolver:
    """Resolver stub that reports a fixed local file; no network, no download."""

    def __init__(self, path):
        self.path = str(path)

    def resolve(self, artist, title, album):
        return ResolveResult(ok=True, path=self.path, error=None, source="static")


def write_wav(path: pathlib.Path, freq: float, seconds: float = 2.0) -> pathlib.Path:
    rate = 44100
    samples = bytearray()
    for index in range(int(rate * seconds)):
        value = int(6000 * math.sin(2.0 * math.pi * freq * index / rate))
        samples += struct.pack("<h", value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(samples))
    return path


def ffprobe_json(path) -> dict:
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"ffprobe failed on {path}: {result.stderr}")
    return json.loads(result.stdout)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        self.root = self.base / "Music"
        self.state = self.base / "state"
        self.config = {
            "root": str(self.root),
            "state": str(self.state),
            "likes_export": None,
            "resolver": "fake",
            "resolver_args": [],
            "ffmpeg": FFMPEG,
            "ffprobe": FFPROBE,
            "max_attempts": 3,
        }
        # Two 2-second WAV fixtures generated here, no fixture files on disk.
        self.wav_a = write_wav(self.base / "tone-a.wav", 440.0)
        self.wav_b = write_wav(self.base / "tone-b.wav", 660.0)
        ingest.merge_likes(self.config, TRACKS)
        self.track_a, self.track_b, self.track_c = TRACKS

    # -- helpers ---------------------------------------------------------
    def target(self, track) -> pathlib.Path:
        return resolve.target_path(self.config, track)

    def source_with_art(self) -> pathlib.Path:
        """A real m4a with an attached picture, built with the real ffmpeg."""
        out = self.base / "with-art.m4a"
        result = subprocess.run(
            [
                FFMPEG, "-y", "-v", "error",
                "-i", str(self.wav_a),
                "-f", "lavfi", "-i", "color=c=navy:s=64x64:d=2",
                "-map", "0:a:0", "-map", "1:v:0",
                "-c:a", "aac", "-c:v", "mjpeg",
                "-disposition:v", "attached_pic", "-shortest",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return out

    # -- tests -----------------------------------------------------------
    def test_target_path_contract_is_deterministic(self):
        path = self.target(self.track_a)
        self.assertEqual(path, self.root / "Nova" / "First Light" / "01 - Alpha.m4a")
        self.assertEqual(self.target(self.track_b).name, "02 - Beta.m4a")
        self.assertEqual(self.target(self.track_c).name, "01 - Gamma.m4a")
        self.assertEqual(path, resolve.target_path(self.config, dict(self.track_a)))

        odd = {
            "spotify_id": "t-odd",
            "artist": "AC/DC",
            "title": "Back/In Black",
            "album": "Live/Night",
            "added_at": "2024-01-01T00:00:00Z",
        }
        odd_path = resolve.target_path(self.config, odd)
        self.assertEqual(odd_path.parent.parent.name, "AC_DC")
        self.assertEqual(odd_path.parent.name, "Live_Night")
        self.assertEqual(odd_path.name, "01 - Back_In Black.m4a")
        self.assertEqual(odd_path, resolve.target_path(self.config, odd))

        # no likes.json at all -> still a deterministic "01"
        empty_config = dict(self.config, state=str(self.base / "no-state"))
        fallback = resolve.target_path(empty_config, odd)
        self.assertEqual(fallback.name, "01 - Back_In Black.m4a")

    def test_probe_duration_reads_the_real_wav(self):
        self.assertAlmostEqual(convert.probe_duration(self.wav_a), 2.0, delta=0.05)

    def test_resolve_one_converts_places_and_tags(self):
        result = resolve.resolve_one(
            self.config, self.track_a, resolve.FakeResolver(str(self.root))
        )
        self.assertTrue(result.ok, result.error)
        final = pathlib.Path(result.path)
        self.assertEqual(final, self.target(self.track_a))
        self.assertTrue(final.exists())
        self.assertEqual(final.suffix, ".m4a")

        info = ffprobe_json(final)
        self.assertIn("m4a", info["format"]["format_name"])
        audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]["codec_name"], "aac")
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0, delta=0.3)

        tags = {key.lower(): value for key, value in info["format"]["tags"].items()}
        self.assertEqual(tags["artist"], "Nova")
        self.assertEqual(tags["title"], "Alpha")
        self.assertEqual(tags["album"], "First Light")
        self.assertEqual(int(str(tags["track"]).split("/")[0]), 1)
        self.assertEqual(tags.get("date"), "2024")

        # The cover comes from the album, and a .fakes source has no art: no file.
        self.assertFalse((final.parent / "cover.jpg").exists())

    def test_cover_once_per_album_and_skipped_without_art(self):
        art_src = self.source_with_art()

        first = resolve.resolve_one(self.config, self.track_a, StaticResolver(art_src))
        self.assertTrue(first.ok, first.error)
        album_dir = self.target(self.track_a).parent
        cover = album_dir / "cover.jpg"
        self.assertTrue(cover.exists())
        self.assertEqual(cover.read_bytes()[:2], b"\xff\xd8")  # a JPEG
        self.assertGreater(cover.stat().st_size, 0)

        cover_mtime = cover.stat().st_mtime_ns
        cover_size = cover.stat().st_size

        second = resolve.resolve_one(self.config, self.track_b, StaticResolver(art_src))
        self.assertTrue(second.ok, second.error)
        self.assertTrue(self.target(self.track_b).exists())
        self.assertEqual(cover.stat().st_mtime_ns, cover_mtime)
        self.assertEqual(cover.stat().st_size, cover_size)

        # audio-only source: no art is not a failure, and no cover is invented
        silent_album = resolve.resolve_one(
            self.config, self.track_c, resolve.FakeResolver(str(self.root))
        )
        self.assertTrue(silent_album.ok, silent_album.error)
        other_dir = self.target(self.track_c).parent
        self.assertTrue(other_dir.exists())
        self.assertFalse((other_dir / "cover.jpg").exists())

    def test_failed_resolve_is_a_value_and_batch_continues(self):
        resolver = resolve.FakeResolver(str(self.root), fail_titles=("Alpha",))
        results = [
            resolve.resolve_one(self.config, track, resolver)
            for track in (self.track_a, self.track_b)
        ]
        first, second = results
        self.assertFalse(first.ok)
        self.assertIsNone(first.path)
        self.assertTrue(first.error)
        self.assertIn("Alpha", first.error)
        self.assertTrue(second.ok, second.error)
        self.assertTrue(pathlib.Path(second.path).exists())
        self.assertFalse(self.target(self.track_a).exists())

    def test_existing_target_is_never_overwritten(self):
        first = resolve.resolve_one(
            self.config, self.track_a, resolve.FakeResolver(str(self.root))
        )
        self.assertTrue(first.ok, first.error)
        target = pathlib.Path(first.path)
        before = target.stat()
        before_bytes = target.read_bytes()

        # A different source would produce different bytes if it were placed.
        second = resolve.resolve_one(self.config, self.track_a, StaticResolver(self.wav_b))
        self.assertTrue(second.ok, second.error)
        self.assertEqual(second.path, str(target))
        after = target.stat()
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                         hashlib.sha256(before_bytes).hexdigest())

    def test_convert_refuses_an_existing_destination(self):
        dst = self.base / "already.m4a"
        dst.write_bytes(b"keep me")
        with self.assertRaises(FileExistsError):
            convert.convert_to_m4a(self.config, self.wav_a, dst)
        self.assertEqual(dst.read_bytes(), b"keep me")

    def test_write_cover_returns_none_for_audio_only_source(self):
        album_dir = self.base / "NoArtAlbum"
        self.assertIsNone(convert.write_cover(self.config, album_dir, self.wav_a))
        self.assertFalse((album_dir / "cover.jpg").exists())

    def test_command_resolver_success_and_failure(self):
        ok = resolve.CommandResolver(
            sys.executable, ["-c", f"import sys; print({str(self.wav_a)!r})"]
        ).resolve("Nova", "Alpha", "First Light")
        self.assertTrue(ok.ok, ok.error)
        self.assertEqual(ok.path, str(self.wav_a))
        self.assertEqual(ok.source, sys.executable)

        failing = resolve.CommandResolver(
            sys.executable,
            ["-c", "import sys; sys.stderr.write('boom: nothing found\\n'); sys.exit(3)"],
        ).resolve("Nova", "Alpha", "First Light")
        self.assertFalse(failing.ok)
        self.assertIsNone(failing.path)
        self.assertIn("boom: nothing found", failing.error)
        self.assertIn("3", failing.error)

        missing = resolve.CommandResolver(
            sys.executable, ["-c", "print('/definitely/not/here.m4a')"]
        ).resolve("Nova", "Alpha", "First Light")
        self.assertFalse(missing.ok)
        self.assertIn("does not exist", missing.error)

    def test_make_resolver_selects_fake_and_command(self):
        fake = resolve.make_resolver(self.config)
        self.assertIsInstance(fake, resolve.FakeResolver)
        self.assertEqual(fake.root, self.root)

        command = resolve.make_resolver(
            dict(self.config, resolver="yt-dlp", resolver_args=["--no-playlist"])
        )
        self.assertIsInstance(command, resolve.CommandResolver)
        self.assertEqual(command.command, "yt-dlp")
        self.assertEqual(command.extra, ["--no-playlist"])


if __name__ == "__main__":
    unittest.main()
