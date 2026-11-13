"""Turn a liked track into a file in the library.

The resolver is a plain seam: :class:`FakeResolver` writes a local sine WAV and
never leaves the machine, :class:`CommandResolver` runs whatever command was
configured with the search query as its last argument.  Failures are values
(:class:`ResolveResult`), never exceptions escaping a batch.
"""

from __future__ import annotations

import math
import os
import pathlib
import re
import struct
import subprocess
import wave
from dataclasses import dataclass

from .convert import place_track
from .ingest import read_likes

_STDERR_TAIL = 500
_EMPTY_NAMES = {"", ".", ".."}
_SINE_SECONDS = 2.0
_SINE_RATE = 44100
_SINE_FREQ = 440.0
_SINE_AMPLITUDE = 6000  # quiet, well inside 16-bit range


@dataclass
class ResolveResult:
    ok: bool
    path: str | None = None
    error: str | None = None
    source: str | None = None


def _tail(text: str | None, limit: int = _STDERR_TAIL) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        text = text[-limit:]
    return text or "no diagnostic output"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return (slug or "track")[:120]


def _write_sine(path: pathlib.Path, seconds: float = _SINE_SECONDS) -> None:
    frames = bytearray()
    total = int(seconds * _SINE_RATE)
    for index in range(total):
        sample = int(
            _SINE_AMPLITUDE * math.sin(2.0 * math.pi * _SINE_FREQ * index / _SINE_RATE)
        )
        frames += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SINE_RATE)
        handle.writeframes(bytes(frames))


class FakeResolver:
    """Offline resolver: generates a 2 s mono 16-bit WAV for tests and demos.

    The file lands in ``workdir`` — the state directory — and never inside the library root:
    Navidrome scans that tree, and a stray ``.wav`` there becomes a phantom album.
    """

    def __init__(self, root: str, fail_titles: tuple[str, ...] = (), workdir: str | None = None):
        self.root = pathlib.Path(str(root))
        self.workdir = pathlib.Path(str(workdir)) if workdir else self.root / ".fakes"
        self.fail_titles = tuple(fail_titles)

    def resolve(self, artist, title, album) -> ResolveResult:
        if title in self.fail_titles:
            return ResolveResult(
                ok=False,
                path=None,
                error=f"fake resolver: no match for {title!r} by {artist!r}",
                source="fake",
            )
        path = self.workdir / (_slug(f"{artist}-{title}-{album}") + ".wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_sine(path)
        return ResolveResult(ok=True, path=str(path), error=None, source="fake")


class CommandResolver:
    """Generic command seam: ``[command, *extra, "ytsearch1:<artist> - <title>"]``."""

    def __init__(self, command: str, extra: list[str] | tuple = ()):
        self.command = str(command)
        self.extra = [str(item) for item in extra]

    def resolve(self, artist, title, album) -> ResolveResult:
        query = f"ytsearch1:{artist} - {title}"
        cmd = [self.command, *self.extra, query]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as exc:
            return ResolveResult(
                ok=False,
                path=None,
                error=f"could not run {self.command}: {exc}",
                source=self.command,
            )
        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        reported = lines[-1] if lines else ""
        if proc.returncode != 0:
            return ResolveResult(
                ok=False,
                path=None,
                error=(
                    f"{pathlib.Path(self.command).name} exited with {proc.returncode}: "
                    f"{_tail(proc.stderr)}"
                ),
                source=self.command,
            )
        if not reported:
            return ResolveResult(
                ok=False,
                path=None,
                error="resolver wrote no file path to stdout",
                source=self.command,
            )
        path = pathlib.Path(reported).expanduser()
        if not path.exists():
            return ResolveResult(
                ok=False,
                path=None,
                error=f"resolver reported a path that does not exist: {path}",
                source=self.command,
            )
        return ResolveResult(ok=True, path=str(path), error=None, source=self.command)


def make_resolver(config):
    """Build the resolver named by ``config["resolver"]``."""
    name = str((config or {}).get("resolver") or "yt-dlp")
    if name == "fake":
        state = (config or {}).get("state") or (config or {}).get("state_dir") or ""
        return FakeResolver(str((config or {}).get("root") or ""), workdir=str(state) or None)
    extra = (config or {}).get("resolver_args") or []
    return CommandResolver(name, extra)


def _sanitise(name) -> str:
    text = "" if name is None else str(name)
    text = text.replace("\x00", "").replace("/", "_")
    text = " ".join(text.split())
    return text.strip(".").strip()


def _root(config) -> pathlib.Path:
    if config and config.get("root"):
        return pathlib.Path(str(config["root"]))
    return pathlib.Path(os.environ.get("MUSIC_ROOT") or (pathlib.Path.home() / "Music"))


def _album_position(config, track) -> int:
    artist = _sanitise(track.get("artist"))
    album = _sanitise(track.get("album"))
    title = _sanitise(track.get("title"))
    spotify_id = track.get("spotify_id")
    try:
        likes = read_likes(config)
    except RuntimeError:
        return 1
    group = [
        item
        for item in likes
        if _sanitise(item.get("artist")) == artist and _sanitise(item.get("album")) == album
    ]
    if spotify_id:
        for index, item in enumerate(group, start=1):
            if item.get("spotify_id") == spotify_id:
                return index
    for index, item in enumerate(group, start=1):
        if _sanitise(item.get("title")) == title:
            return index
    return 1


def target_path(config, track) -> pathlib.Path:
    """``<root>/<Artist>/<Album>/<NN - Title>.m4a``, deterministic for a track."""
    root = _root(config)
    artist = _sanitise(track.get("artist")) or "Unknown Artist"
    album = _sanitise(track.get("album")) or "Unknown Album"
    title = _sanitise(track.get("title")) or "Untitled"
    number = _album_position(config, track)
    return root / artist / album / f"{number:02d} - {title}.m4a"


def resolve_one(config, track, resolver=None) -> ResolveResult:
    """Resolve, convert, tag and place one track; a failure is returned, not raised."""
    resolver = resolver or make_resolver(config)
    target = target_path(config, track)
    if target.exists():
        return ResolveResult(ok=True, path=str(target), error=None, source=None)

    result = resolver.resolve(track.get("artist", ""), track.get("title", ""), track.get("album", ""))
    if not result.ok:
        return result
    if not result.path:
        return ResolveResult(
            ok=False,
            path=None,
            error="resolver reported success without a file path",
            source=result.source,
        )
    try:
        final = place_track(config, track, result.path)
    except (RuntimeError, OSError) as exc:
        return ResolveResult(
            ok=False,
            path=None,
            error=f"could not place {track.get('title')!r}: {exc}",
            source=result.source,
        )
    return ResolveResult(ok=True, path=str(final), error=None, source=result.source)
