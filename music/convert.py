"""ffmpeg / ffprobe / tag / cover wrappers.

The three external calls of the pipeline live here so the rest of the code
deals in ``Path`` objects only:

* ``convert_to_m4a``  source audio -> AAC in an ``.m4a`` container (AC7),
  and it records the duration ffprobe reports for the file it just wrote
  (AC10);
* ``write_tags``      the five AC7 tags into an existing ``.m4a``;
* ``probe_duration``  the duration of any file ffprobe can read.
* ``ensure_cover``    the one ``cover.jpg`` of an album folder (AC9).

It also holds the one destination guard of the pipeline (AC34-AC37):
``verified_dir`` and ``reserve`` are the single place where a path under
``library_root`` is computed, checked against ``os.path.realpath(library_root)``
and created with ``O_EXCL | O_NOFOLLOW``, and every write in this module goes
to a private file of that kind which is then renamed into place.

``ffmpeg`` and ``ffprobe`` are taken from ``PATH``; no other dependency is
declared, and no network is touched.
"""

from __future__ import annotations

import base64
import os
import stat
import subprocess
from pathlib import Path

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

#: Optional environment overrides for the two external tools (AC39).
FFMPEG_VAR = "MUSIC_FFMPEG"
FFPROBE_VAR = "MUSIC_FFPROBE"


def tool(name: str, variable: str) -> str:
    """The executable to call: the environment value, else ``name`` (AC39)."""
    return os.environ.get(variable) or name

#: The AC7 tags, in the order ``write_tags`` writes them.
TAG_KEYS = ("artist", "title", "album", "track", "year")

#: Duration in seconds ffprobe reported for every file ``convert_to_m4a``
#: wrote, keyed by resolved path (AC10).
DURATIONS: dict[str, float] = {}

#: A valid 16x16 baseline JPEG (magic ``FF D8 FF E0`` ... ``FF D9``), used
#: when a source carries no artwork but AC9 still demands a real JPEG.
_PLACEHOLDER_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYwLjMxLjEwMgD/2wBDAAgoKC8oLzc3Nzc3"
    "N0E8QUNDQ0FBQUFDQ0NISEhVVVVISEhDQ0hIUFBVVVxfXFdXVVdfX2RkZHh4c3OMjJGsrM//"
    "xABLAAEBAAAAAAAAAAAAAAAAAAAABwEBAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAA"
    "AAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIABAAEAMBIgACEQADEQD/2gAMAwEAAhEDEQA/"
    "AJUAD//Z"
)

_JPEG_MAGIC = b"\xff\xd8\xff"
_JPEG_END = b"\xff\xd9"

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def is_jpeg(path: Path | str) -> bool:
    """True when ``path`` holds a JPEG image, judged by its magic bytes.

    Only the first three and the last two bytes are read, so an oversized or
    hostile ``cover.jpg`` cannot be pulled into memory (AC36).
    """
    try:
        with open(str(path), "rb") as handle:
            if handle.read(3) != _JPEG_MAGIC:
                return False
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size < 4:
                return False
            handle.seek(size - 2)
            return handle.read(2) == _JPEG_END
    except OSError:
        return False


# --- the one destination guard (AC34-AC37) --------------------------------


def _exclusive_temp(path: Path, suffix: str) -> Path:
    """Create a private file beside ``path`` with ``O_EXCL | O_NOFOLLOW``."""
    for _ in range(100):
        candidate = path.parent / f".{path.name}.{os.urandom(6).hex()}{suffix}"
        try:
            handle = os.open(
                str(candidate),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _O_NOFOLLOW
                | _O_CLOEXEC,
                0o666,
            )
        except FileExistsError:
            continue
        os.close(handle)
        return candidate
    raise RuntimeError(f"no free temporary name beside {path}")


def discard(path: Path) -> None:
    """Remove a file this run created; never a symlink, never a directory."""
    try:
        info = os.lstat(str(path))
    except OSError:
        return
    if not stat.S_ISREG(info.st_mode):
        return
    try:
        os.unlink(str(path))
    except OSError:
        pass


def _is_regular_file(path: Path) -> bool:
    """True for a regular file, judged by ``lstat`` so a symlink is not one."""
    try:
        return stat.S_ISREG(os.lstat(str(path)).st_mode)
    except OSError:
        return False


def destination_refusal(destination: Path) -> str:
    """Why nothing may be written at ``destination``, or ``""`` (AC34, AC37).

    The cheap pre-check before the expensive conversion: one ``lstat``, which
    also catches a symlink whose target does not exist — ``Path.exists()`` is
    ``False`` for exactly that case, which is how a dangling link used to be
    created through.
    """
    try:
        info = os.lstat(str(destination))
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"cannot inspect the destination {destination}: {exc.strerror}"
    if stat.S_ISLNK(info.st_mode):
        return f"refusing a symlink at the destination: {destination}"
    return f"refusing to overwrite a file at the destination: {destination}"


def _parts_below(anchors: tuple[str, ...], directory: Path) -> list[str]:
    """``directory`` as components below one of ``anchors``, else raise.

    Two anchors are accepted: the configured ``library_root`` exactly as written
    (it may itself be a symlink to a mounted disk — that is the root, not a
    component under it) and its ``realpath``.
    """
    target = os.path.abspath(str(directory))
    for anchor in anchors:
        relative = os.path.relpath(target, anchor)
        if os.path.isabs(relative):
            continue
        if relative != os.curdir and relative.split(os.sep)[0] == os.pardir:
            continue
        parts = [] if relative == os.curdir else relative.split(os.sep)
        for part in parts:
            if (
                part in ("", os.curdir, os.pardir)
                or "\0" in part
                or "/" in part
            ):
                raise RuntimeError(
                    f"unsafe path component {part!r} in {directory}"
                )
        return parts
    raise RuntimeError(
        f"refusing to write outside library_root: {directory}"
    )


def _mkdir_open(dir_fd: int, name: str) -> int:
    """Create ``name`` below ``dir_fd`` when absent, open it without following."""
    try:
        os.mkdir(name, 0o777, dir_fd=dir_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise RuntimeError(
            f"cannot create directory {name}: {exc.strerror}"
        ) from exc
    try:
        return os.open(
            name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        raise RuntimeError(
            "refusing to follow a symlinked directory component: "
            f"{name} ({exc.strerror})"
        ) from exc


def verified_dir(config: dict, directory: Path) -> Path:
    """Create and verify an album directory below ``library_root`` (AC35).

    Walks ``<library_root>/<Artist>/<Album>`` one component at a time from an
    ``O_NOFOLLOW | O_DIRECTORY`` handle, so a symlinked component is refused
    instead of followed, and then requires the directory's ``realpath`` to stay
    under ``os.path.realpath(library_root)``.  Returns the real path.
    """
    configured = os.path.abspath(str(config["library_root"]))
    root = os.path.realpath(configured)
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"library_root {root}: {exc.strerror}") from exc
    parts = _parts_below((configured, root), Path(directory))
    handle = os.open(
        root, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    )
    try:
        for part in parts:
            child = _mkdir_open(handle, part)
            os.close(handle)
            handle = child
    finally:
        os.close(handle)
    real = os.path.realpath(str(directory))
    if os.path.commonpath((root, real)) != root:
        raise RuntimeError(
            f"refusing to write outside library_root: {directory}"
        )
    return Path(real)


def reserve(config: dict, destination: Path) -> Path:
    """Claim ``destination`` exclusively below a verified directory (AC34, AC37).

    Creates the album directory (``verified_dir``, AC35) and then the
    destination itself with ``O_EXCL | O_NOFOLLOW``: an existing regular file and
    a symlink, dangling or not, both fail with the reason instead of being
    written through.  Returns the claimed destination; the caller fills it and
    removes it with ``discard`` when the placement does not finish.
    """
    requested = Path(destination)
    album = verified_dir(config, requested.parent)
    destination = album / requested.name
    handle = os.open(
        str(album), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    )
    try:
        try:
            created = os.open(
                destination.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _O_NOFOLLOW
                | _O_CLOEXEC,
                0o666,
                dir_fd=handle,
            )
        except FileExistsError as exc:
            raise RuntimeError(
                destination_refusal(destination)
                or f"refusing to overwrite {destination}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"cannot create {destination}: {exc.strerror}"
            ) from exc
        os.close(created)
    finally:
        os.close(handle)
    return destination


def _create_dir_chain(directory: Path) -> None:
    """Create ``directory``, refusing to follow a symlinked component (AC35)."""
    directory = Path(os.path.abspath(str(directory)))
    missing: list[Path] = []
    current = directory
    while not current.exists() or current.is_symlink():
        if current.is_symlink():
            raise RuntimeError(f"refusing to follow a symlink: {current}")
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for candidate in reversed(missing):
        try:
            os.mkdir(str(candidate))
        except FileExistsError:
            if candidate.is_symlink():
                raise RuntimeError(
                    f"refusing to follow a symlink: {candidate}"
                ) from None


def _write_replacing(path: Path, data: bytes) -> None:
    """Write ``data`` to a private file and rename it onto ``path`` (AC36)."""
    tmp = _exclusive_temp(path, ".part")
    try:
        with open(str(tmp), "wb") as handle:
            handle.write(data)
        os.replace(str(tmp), str(path))
    finally:
        discard(tmp)


def _run(argv: list[str]) -> str:
    """Run one external tool, return its stdout, raise on a non-zero exit."""
    try:
        done = subprocess.run(
            argv, check=False, capture_output=True, text=True
        )
    except OSError as exc:  # tool missing, not executable, ...
        raise RuntimeError(f"{argv[0]}: {exc}") from exc
    if done.returncode != 0:
        reason = done.stderr.strip() or f"exit {done.returncode}"
        raise RuntimeError(f"{argv[0]} failed: {reason}")
    return done.stdout


def probe_duration(path: Path) -> float:
    """Return the duration in seconds ffprobe reports for ``path`` (AC10)."""
    path = Path(path)
    stdout = _run(
        [
            tool(FFPROBE, FFPROBE_VAR),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    text = stdout.strip()
    try:
        return float(text)
    except ValueError as exc:
        raise RuntimeError(
            f"ffprobe reported no duration for {path}: {text!r}"
        ) from exc


def convert_to_m4a(config: dict, src: Path, dst: Path) -> None:
    """Encode ``src`` as AAC in the ``.m4a`` container ``dst`` (AC7).

    ``dst`` itself is never handed to ffmpeg: the encoder writes a private file
    created with ``O_EXCL | O_NOFOLLOW`` in ``dst``'s directory, and that file is
    renamed into place, so a symlink at ``dst`` is replaced rather than written
    through (AC34).

    The duration ffprobe reports for the written file is stored in
    :data:`DURATIONS` under the resolved ``dst`` (AC10); that is the value the
    library lane persists, because ``TRACK_RESULT_KEYS`` must not grow.
    """
    src, dst = Path(src), Path(dst)
    _create_dir_chain(dst.parent)
    tmp = _exclusive_temp(dst, ".part")
    try:
        _run(
            [
                tool(FFMPEG, FFMPEG_VAR),
                "-nostdin",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-vn",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-f",
                "ipod",
                str(tmp),
            ]
        )
        DURATIONS[str(dst.resolve())] = probe_duration(tmp)
        os.replace(str(tmp), str(dst))
    finally:
        discard(tmp)


def recorded_duration(path: Path) -> float | None:
    """The duration recorded for ``path``, or ``None`` (AC10)."""
    return DURATIONS.get(str(Path(path).resolve()))


def write_tags(path: Path, tags: dict[str, str]) -> None:
    """Write ``tags`` (``TAG_KEYS``, AC7) into the ``.m4a`` at ``path``.

    The audio stream is copied, not re-encoded, and no other metadata is
    carried over.  ``use_metadata_tags`` is what makes an explicit ``year``
    survive into the mp4 ``mdta`` box; without it the muxer drops the key.
    """
    path = Path(path)
    tmp = _exclusive_temp(path, ".tags")
    argv = [
        tool(FFMPEG, FFMPEG_VAR),
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a",
        "-c",
        "copy",
        "-map_metadata",
        "-1",
        "-movflags",
        "use_metadata_tags+faststart",
    ]
    for key in TAG_KEYS:
        value = tags.get(key)
        if not value:
            continue
        argv += ["-metadata", f"{key}={value}"]
    argv += ["-f", "ipod", str(tmp)]
    try:
        _run(argv)
        os.replace(str(tmp), str(path))
    finally:
        discard(tmp)


def ensure_cover(album_dir: Path, cover_src: str | None = None) -> Path:
    """Return ``<album_dir>/cover.jpg``, a real JPEG, creating it if needed.

    Idempotent (AC9): an existing *regular* JPEG is returned untouched, so two
    tracks of one album leave exactly one ``cover.jpg``.

    An existing ``cover.jpg`` that is not a regular file — a symlink in
    particular — is never read through and never written through: the folder
    gets a new regular ``cover.jpg`` of its own, written to a private file and
    renamed over the link, so whatever the link pointed at keeps its bytes
    (AC36).  ``cover_src`` may be a JPEG file, another image ffmpeg can read, or
    ``None`` / anything that is not a local file (a URL is not fetched — the
    tests are offline), in which case the placeholder JPEG is written.
    """
    album_dir = Path(album_dir)
    _create_dir_chain(album_dir)
    dest = album_dir / "cover.jpg"
    if _is_regular_file(dest) and is_jpeg(dest):
        return dest

    src = Path(cover_src) if cover_src else None
    if src is not None and src.is_file():
        if is_jpeg(src):
            _write_replacing(dest, src.read_bytes())
            return dest
        tmp = _exclusive_temp(dest, ".part")
        try:
            _run(
                [
                    tool(FFMPEG, FFMPEG_VAR),
                    "-nostdin",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(src),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    "-f",
                    "image2",
                    "-c:v",
                    "mjpeg",
                    str(tmp),
                ]
            )
        except RuntimeError:
            pass
        if is_jpeg(tmp):
            os.replace(str(tmp), str(dest))
            return dest
        discard(tmp)

    _write_replacing(dest, _PLACEHOLDER_JPEG)
    return dest


def cover_source_for(src: Path) -> str | None:
    """A sibling image of ``src`` with the same stem, if there is one."""
    src = Path(src)
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = src.with_suffix(suffix)
        if candidate.is_file():
            return str(candidate)
    return None
