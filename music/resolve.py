"""The resolver seam: the call, its failure path, and ``FakeResolver``.

One command resolves one track: ``<resolver…> <artist> <title> <album>``, three
positional arguments in that order, never through a shell (AC5).  Exit 0 means
line 1 of stdout names the produced audio file (AC6); a non-zero exit means
stderr carries the reason (AC11) and the run keeps going.

On success the named file is converted to AAC ``.m4a`` (AC7), tagged, placed at
``<library_root>/<Artist>/<Album>/<NN - Title>.m4a`` with ``NN`` its position in
the album ordered by ``added_at`` then ``title`` (AC8), and the album folder
gets its one ``cover.jpg`` (AC9).  ``likes.json`` is the ordering source, so the
position matches the order ``ingest`` recorded.

Placement goes through one guard (AC34-AC37): ``convert.destination_refusal``
is the cheap ``lstat`` pre-check, ``convert.reserve`` is the authority that
verifies every directory component below ``library_root`` and claims the file
with ``O_EXCL | O_NOFOLLOW``, and the encoder only ever writes a private file
that is renamed into place.  A taken destination — a regular file or a symlink,
dangling or not — is refused with a reason and the resolver's file is never
reported as the placed track.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from concurrent import futures
from pathlib import Path

from music import convert
from music.ingest import load_likes

#: Suffix of every placed track (AC7, AC8).
AUDIO_SUFFIX = ".m4a"

#: Characters no path component may carry (AC38).
_UNSAFE = ("/", "\\", "\0")

#: Frame of one encoded character in :func:`sanitize`; a literal ``_`` is
#: encoded too, so ``_`` appears in a name only as part of an escape (AC38).
_ESCAPE = "_"

#: The name of an empty value.  No non-empty value encodes to it: every
#: non-empty encoding starts with a literal character other than ``_`` or with
#: ``_u``, never with ``_e`` (AC38).
_EMPTY = "_empty"

#: The one resolver name the pipeline answers itself, so a run works without a
#: network and without a download tool (AC40).
FAKE_RESOLVER = "fake"

#: Length of the tone the fake resolver writes, in seconds (AC40).
FAKE_SECONDS = 4

#: Environment variable that lowers the number of parallel resolver calls
#: (AC42). The cap is fixed: at most :data:`DEFAULT_WORKERS` calls at once.
CONCURRENCY_VAR = "MUSIC_CONCURRENCY"

#: Resolver calls that run at once when the variable is unset, and the ceiling
#: the variable cannot pass (AC42).
DEFAULT_WORKERS = 3
MAX_WORKERS = 3


def workers(env: dict | None = None) -> int:
    """How many resolver calls run at once: 1 to :data:`MAX_WORKERS` (AC42).

    An unset, empty or unreadable value gives :data:`DEFAULT_WORKERS`. A value
    above the ceiling is cut to it, a value below one is raised to one.
    """
    source = os.environ if env is None else env
    raw = str(source.get(CONCURRENCY_VAR) or "").strip()
    if not raw:
        return DEFAULT_WORKERS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WORKERS
    return max(1, min(MAX_WORKERS, value))


def _result(
    spotify_id: str,
    ok: bool,
    path: str | None,
    error: str | None,
    source: str | None,
    duration: float | None,
) -> dict:
    """A dict in exactly ``TRACK_RESULT_KEYS`` order."""
    return {
        "spotify_id": spotify_id,
        "ok": ok,
        "path": path,
        "error": error,
        "source": source,
        "duration": duration,
    }


def _failed(track: dict, reason: str) -> dict:
    return _result(
        str(track.get("spotify_id", "")), False, None, reason, None, None
    )


# --- naming (AC8) ---------------------------------------------------------


def sanitize(value: str) -> str:
    """An injective, path-component-safe form of ``value`` (AC8, AC38).

    Nothing is dropped and nothing is merged, so two different values never
    share a component: a character a path component cannot hold (``/``, ``\\``,
    NUL, a whitespace other than a plain space, a leading ``.``) and a literal
    ``_`` are written ``_u<codepoint>_``, and every other character is kept as
    it is (unicode included).  ``AC/DC`` and ``AC_DC`` therefore stay two
    different names instead of both becoming ``AC_DC`` on one file.
    """
    text = str(value)
    out: list[str] = []
    for index, char in enumerate(text):
        rewritten = (
            char in _UNSAFE
            or char == _ESCAPE
            or (char.isspace() and char != " ")
            or (index == 0 and char == ".")
        )
        out.append(f"{_ESCAPE}u{ord(char):x}_" if rewritten else char)
    return "".join(out) or _EMPTY


def album_likes(config: dict, artist: str, album: str) -> list[dict]:
    """The liked entries of one album, ordered by ``added_at`` then ``title``."""
    entries = [
        entry
        for entry in load_likes(config)
        if entry["artist"] == artist and entry["album"] == album
    ]
    return sorted(entries, key=lambda entry: (entry["added_at"], entry["title"]))


def track_number(config: dict, track: dict, album_dir: Path | None = None) -> int:
    """One-based position of ``track`` in its album (AC8).

    Ordered by ``added_at`` then ``title`` over the album's likes.  A track that
    ``likes.json`` does not hold is appended after what the folder already
    carries, so a partial state still yields a free name.
    """
    artist, album = str(track["artist"]), str(track["album"])
    ids = [entry["spotify_id"] for entry in album_likes(config, artist, album)]
    spotify_id = str(track.get("spotify_id", ""))
    if spotify_id in ids:
        return ids.index(spotify_id) + 1
    if album_dir is not None and album_dir.is_dir():
        placed = sum(
            1 for path in album_dir.iterdir() if path.suffix == AUDIO_SUFFIX
        )
        return placed + 1
    return 1


def album_dir_for(config: dict, track: dict) -> Path:
    """``<library_root>/<Artist>/<Album>`` (AC8)."""
    artist_dir = Path(config["library_root"]) / sanitize(track["artist"])
    return artist_dir / sanitize(track["album"])


def collision_reason(destination: Path) -> str | None:
    """The reason an occupied ``destination`` is refused, or ``None`` (AC38).

    A name that is taken is never reported as this track's own file, so two
    tracks that cannot be told apart by their name keep distinct paths and
    neither claims the other's file.
    """
    if not destination.exists() and not destination.is_symlink():
        return None
    return (
        f"file name collision: destination {destination} already holds a "
        "file, refusing to claim it as this track's"
    )


def library_path(config: dict, track: dict, number: int | None = None) -> Path:
    """``<library_root>/<Artist>/<Album>/<NN - Title>.m4a`` (AC8)."""
    album_dir = album_dir_for(config, track)
    if number is None:
        number = track_number(config, track, album_dir)
    name = f"{number:02d} - {sanitize(track['title'])}{AUDIO_SUFFIX}"
    return album_dir / name


def tags_for(track: dict, number: int) -> dict[str, str]:
    """The five AC7 tags for ``track`` at album position ``number``."""
    added_at = str(track.get("added_at", ""))
    year = added_at[:4] if len(added_at) >= 4 and added_at[:4].isdigit() else ""
    return {
        "artist": str(track["artist"]),
        "title": str(track["title"]),
        "album": str(track["album"]),
        "track": str(number),
        "year": year,
    }


# --- the call (AC5, AC6, AC11) -------------------------------------------


def resolver_argv(config: dict) -> list[str]:
    """``shlex.split(config["resolver"])`` — the base argv, arguments included."""
    argv = shlex.split(config["resolver"])
    if not argv:
        raise ValueError("config['resolver'] is empty")
    return argv


def fake_source(track: dict) -> Path:
    """A generated four-second tone, in place of a download (AC40).

    The file lands in a private temp folder, never under ``library_root``, so
    the placement guard sees a normal source file.
    """
    folder = Path(tempfile.mkdtemp(prefix="music-fake-"))
    name = sanitize(str(track.get("title") or "track")) or "track"
    path = folder / f"{name}.wav"
    convert._run(
        [
            convert.tool(convert.FFMPEG, convert.FFMPEG_VAR),
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={FAKE_SECONDS}",
            "-ac",
            "2",
            str(path),
        ]
    )
    return path


def source_file(config: dict, track: dict) -> tuple[Path | None, str | None]:
    """The file the resolver named for ``track``, or ``(None, reason)``.

    ``MUSIC_RESOLVER=fake`` answers from :func:`fake_source` instead of a
    subprocess (AC40). Every other command runs without a shell, gets
    ``artist``, ``title`` and ``album`` as three positional arguments (AC5),
    and must exit 0 with the file path on line 1 of stdout (AC6, AC11).
    """
    argv = resolver_argv(config) + [
        str(track["artist"]),
        str(track["title"]),
        str(track["album"]),
    ]
    if argv[0] == FAKE_RESOLVER:
        try:
            return fake_source(track), None
        except (OSError, RuntimeError) as exc:
            return None, str(exc)
    try:
        done = subprocess.run(
            argv, check=False, capture_output=True, text=True
        )
    except OSError as exc:
        return None, f"{argv[0]}: {exc}"
    if done.returncode != 0:
        return None, done.stderr.strip() or f"resolver exited {done.returncode}"
    lines = done.stdout.splitlines()
    first = lines[0].strip() if lines else ""
    if not first:
        return None, "resolver printed no file path on stdout"
    path = Path(first)
    if not path.is_file():
        return None, f"resolver named a file that does not exist: {first}"
    return path, None


def resolve_track(config: dict, track: dict) -> dict:
    """Resolve, convert and place one track; never raises for a resolver failure.

    Returns a ``TRACK_RESULT_KEYS`` dict: on success ``path`` is the placed
    ``.m4a``, ``source`` the file the resolver named (AC6) and ``duration``
    what ffprobe reports for it (AC10), on failure ``path``, ``source`` and
    ``duration`` are ``None`` and ``error`` carries the stderr text (AC11).

    The resolver is always called first (AC37).  A destination that already
    holds a file, or a symlink anywhere below ``library_root`` on the way to it,
    yields a failed result naming the reason and writes nothing outside the tree
    (AC34, AC35, AC37).
    """
    spotify_id = str(track.get("spotify_id", ""))
    source, reason = source_file(config, track)
    if source is None:
        return _failed(track, reason or "the resolver named no file")

    try:
        album_dir = album_dir_for(config, track)
        number = track_number(config, track, album_dir)
        destination = library_path(config, track, number)
        # Cheap pre-check: one lstat, so a taken name fails without an encoder
        # run and the pre-existing entry is never reported as this track
        # (AC34, AC37).
        refusal = convert.destination_refusal(destination)
        if refusal:
            return _failed(track, refusal)
        # The authority: the album chain is verified (AC35) and the name is
        # claimed with O_EXCL | O_NOFOLLOW before anything is written (AC34).
        destination = convert.reserve(config, destination)
        try:
            convert.convert_to_m4a(config, source, destination)
            convert.write_tags(destination, tags_for(track, number))
            convert.ensure_cover(
                destination.parent, convert.cover_source_for(source)
            )
        except BaseException:
            convert.discard(destination)
            raise
        recorded = convert.recorded_duration(destination)
        duration = float(
            recorded
            if recorded is not None
            else convert.probe_duration(destination)
        )
    except (OSError, RuntimeError) as exc:
        return _failed(track, str(exc))

    return _result(
        spotify_id, True, str(destination), None, str(source), duration
    )


def resolve_pending(config: dict, tracks: list[dict]) -> list[dict]:
    """Resolve every track; one failure never stops the rest (AC11).

    Up to :func:`workers` resolver calls run at once (AC42). Returns one
    ``TRACK_RESULT_KEYS`` dict per input track, in input order, whatever order
    the calls finish in.
    """

    def one(track: dict) -> dict:
        try:
            return resolve_track(config, track)
        except Exception as exc:  # noqa: BLE001 - a bad track must not abort the run
            return _failed(track, f"{type(exc).__name__}: {exc}")

    if len(tracks) < 2:
        return [one(track) for track in tracks]
    slots: list[dict | None] = [None] * len(tracks)
    with futures.ThreadPoolExecutor(max_workers=workers()) as pool:
        running = {
            pool.submit(one, track): index for index, track in enumerate(tracks)
        }
        for done in futures.as_completed(running):
            slots[running[done]] = done.result()
    return [result for result in slots if result is not None]


# --- the double ----------------------------------------------------------


class FakeResolver:
    """Offline resolver double for the tests and the library lane.

    ``sources`` maps ``"artist\\0title\\0album"`` (the contract's example key
    shape) to the file that stands in for the download: a ``Path``/``str``, or
    a dict with ``path`` and an optional ``spotify_id``.  A missing key yields
    ``ok=False`` with an error string, so the retry and parking paths (AC21-23)
    are testable without ffmpeg or a network.  It never writes to
    ``library_root``: ``path`` is the source file itself and ``source`` the same
    file, as the resolver contract allows for a stand-in.
    """

    def __init__(
        self, config: dict, sources: dict[str, Path] | None = None
    ) -> None:
        self.config = dict(config or {})
        self.sources = dict(sources or {})

    def resolve(self, artist: str, title: str, album: str) -> dict:
        key = "\0".join((str(artist), str(title), str(album)))
        value = self.sources.get(key)
        if value is None:
            return _result(
                "",
                False,
                None,
                f"FakeResolver has no source for {artist} - {title} ({album})",
                None,
                None,
            )
        spotify_id = ""
        raw: object = value
        if isinstance(value, dict):
            spotify_id = str(value.get("spotify_id") or "")
            raw = value.get("path")
        source = Path(str(raw))
        if not source.is_file():
            return _result(
                spotify_id,
                False,
                None,
                f"FakeResolver source is missing: {source}",
                None,
                None,
            )
        return _result(spotify_id, True, str(source), None, str(source), None)


