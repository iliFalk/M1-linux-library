"""ffmpeg-backed conversion, tagging, cover extraction and placement.

Every function here drives the real ``ffmpeg``/``ffprobe`` binaries named by
``config["ffmpeg"]`` / ``config["ffprobe"]``.  There is no bundled tagger and no
fallback implementation: a missing or failing binary is an error the caller sees.
"""

from __future__ import annotations

import os
import pathlib
import sys
import subprocess

_DEFAULT_FFMPEG = "ffmpeg"
_DEFAULT_FFPROBE = "ffprobe"
_STDERR_TAIL = 500


def _tool(config, key: str) -> str:
    """Resolve a binary name from the config, the env, then the default."""
    if config:
        value = config.get(key)
        if value:
            return str(value)
    return os.environ.get("MUSIC_" + key.upper(), key)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(f"could not run {cmd[0]}: {exc}") from exc


def _tail(text: str | None, limit: int = _STDERR_TAIL) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        text = text[-limit:]
    return text or "no diagnostic output"


def probe_duration(path, config=None) -> float:
    """Real container duration in seconds, read with ffprobe."""
    path = pathlib.Path(path)
    cmd = [
        _tool(config, "ffprobe"),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        str(path),
    ]
    result = _run(cmd)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe could not read a duration from {path.name}: {_tail(result.stderr)}"
        )
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    value = lines[-1] if lines else ""
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(
            f"ffprobe returned no usable duration for {path.name}: {value!r}"
        ) from exc


def convert_to_m4a(config, src, dst) -> None:
    """Transcode the audio of ``src`` to AAC in an ``.m4a`` container at ``dst``.

    Refuses to overwrite an existing ``dst``; a failed ffmpeg run leaves no
    half-written file behind.
    """
    src = pathlib.Path(src)
    dst = pathlib.Path(dst)
    if dst.exists():
        raise FileExistsError(f"refusing to overwrite {dst}")
    if not src.exists():
        raise RuntimeError(f"source file does not exist: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _tool(config, "ffmpeg"),
        "-y", "-v", "error",
        "-i", str(src),
        "-vn", "-map", "0:a:0",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]
    result = _run(cmd)
    if result.returncode != 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg could not convert {src.name} to AAC/m4a: {_tail(result.stderr)}"
        )
    if not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg produced no output for {src.name}")


def write_tags(config, path, *, artist, title, album, track, year) -> None:
    """Write artist/title/album/track/year onto ``path`` via a copy and a rename."""
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"cannot tag a missing file: {path}")
    tmp = path.with_name(f".{path.stem}.tag-{os.getpid()}{path.suffix}")
    cmd = [
        _tool(config, "ffmpeg"),
        "-y", "-v", "error",
        "-i", str(path),
        "-c", "copy",
        "-metadata", f"artist={artist}",
        "-metadata", f"title={title}",
        "-metadata", f"album={album}",
    ]
    if track not in (None, ""):
        cmd += ["-metadata", f"track={track}"]
    if year not in (None, ""):
        # The mp4/m4a muxer stores the year in the (c)day atom, which ffprobe
        # reports as "date"; pass both so either spelling lands.
        cmd += ["-metadata", f"year={year}", "-metadata", f"date={year}"]
    cmd.append(str(tmp))
    try:
        result = _run(cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg could not tag {path.name}: {_tail(result.stderr)}"
            )
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced no tagged output for {path.name}")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_cover(config, album_dir, src) -> str | None:
    """Write one ``cover.jpg`` per album from ``src``; ``None`` when there is no art.

    An existing cover is reused untouched.  The source is read with ffmpeg, so
    an embedded picture is extracted and a video frame is the fallback; an
    audio-only source simply yields ``None``.
    """
    album_dir = pathlib.Path(album_dir)
    src = pathlib.Path(src)
    cover = album_dir / "cover.jpg"
    if cover.exists():
        return str(cover)
    album_dir.mkdir(parents=True, exist_ok=True)
    tmp = album_dir / f".cover-{os.getpid()}.jpg"
    cmd = [
        _tool(config, "ffmpeg"),
        "-y", "-v", "error",
        "-i", str(src),
        "-map", "0:v:0", "-frames:v", "1",
        "-q:v", "2",
        str(tmp),
    ]
    try:
        result = _run(cmd)
        if result.returncode != 0:
            return None
        if not tmp.exists() or tmp.stat().st_size == 0:
            return None
        os.replace(tmp, cover)
        return str(cover)
    finally:
        tmp.unlink(missing_ok=True)


def place_track(config, track, src) -> pathlib.Path:
    """Move a resolved source into the library: cover, convert, tag, verify."""
    from .resolve import target_path  # local import keeps resolve<->convert acyclic

    target = target_path(config, track)
    target.parent.mkdir(parents=True, exist_ok=True)

    if write_cover(config, target.parent, src) is None:
        # Silence here reads as "there was no art to want"; say it instead, once per placement.
        print(f"note: no cover for {target.parent.name}/{target.stem}: the source carries no picture",
              file=sys.stderr)
    convert_to_m4a(config, src, target)

    stem = target.stem
    prefix = stem.split(" - ", 1)[0]
    number = int(prefix) if prefix.isdigit() else 1

    added_at = str(track.get("added_at") or "")
    year = added_at[:4] if len(added_at) >= 4 and added_at[:4].isdigit() else None

    try:
        write_tags(
            config,
            target,
            artist=track.get("artist", ""),
            title=track.get("title", ""),
            album=track.get("album", ""),
            track=number,
            year=year,
        )
        duration = probe_duration(target, config=config)
        if duration <= 0:
            raise RuntimeError(f"produced file has no playable duration: {target.name}")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target
