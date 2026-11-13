"""Library reconciliation: scan the file tree, sync likes into it, report.

Pipeline modules (``music.ingest``, ``music.resolve``, ``music.convert``) are
imported lazily inside the functions that need them so that ``scan`` and
``report`` keep working when the pipeline is not installed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

from . import store


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _rows_readonly(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read the index without creating anything; missing index means empty."""
    try:
        conn = store.connect(config, readonly=True)
    except FileNotFoundError:
        return []
    try:
        return store.list_tracks(conn)
    finally:
        conn.close()


def _walk_m4a(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".m4a"
    )


def _file_key(path: Path, root: Path) -> tuple[str, str, str] | None:
    """Map ``<root>/<Artist>/<Album>/<NN - Title>.m4a`` to a lookup key."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) != 3:
        return None
    artist, album, name = relative.parts
    stem = Path(name).stem
    title = stem.split(" - ", 1)[1] if " - " in stem else stem
    return (
        artist.strip().casefold(),
        album.strip().casefold(),
        title.strip().casefold(),
    )


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("artist", "")).strip().casefold(),
        str(row.get("album", "")).strip().casefold(),
        str(row.get("title", "")).strip().casefold(),
    )


def _path_of(row: Mapping[str, Any]) -> Path | None:
    raw = row.get("path")
    return Path(raw) if raw else None


def _row_file_exists(row: Mapping[str, Any]) -> bool:
    path = _path_of(row)
    return path is not None and path.is_file()


def _probe_seconds(path: Path) -> float | None:
    """Ask the pipeline for a duration; absence of the pipeline is not fatal."""
    try:
        from music.convert import probe_duration
    except ImportError:
        return None
    try:
        return float(probe_duration(str(path)))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def scan(
    config: Mapping[str, Any],
    dry_run: bool = False,
    on_stray: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Reconcile ``config["root"]`` with the index.

    ``added`` and ``missing`` count rows that change state *during this call*,
    so a second scan of an unchanged tree reports zeros.  ``failed`` is always
    zero here: scan never resolves or downloads anything.
    """
    root = Path(config["root"])
    conn: sqlite3.Connection | None = None
    if dry_run:
        rows = _rows_readonly(config)
    else:
        conn = store.connect(config)
        rows = store.list_tracks(conn)

    try:
        files = _walk_m4a(root)
        by_path: dict[str, Path] = {}
        by_key: dict[tuple[str, str, str], Path] = {}
        for path in files:
            by_path[str(path.resolve())] = path
            key = _file_key(path, root)
            if key is not None:
                by_key.setdefault(key, path)

        claimed: set[str] = set()
        added = 0
        missing = 0
        failed = 0

        for row in rows:
            spotify_id = row["spotify_id"]
            match: Path | None = None
            path = _path_of(row)
            if path is not None:
                match = by_path.get(str(path.resolve()))
                if match is None and path.is_file():
                    match = path
            if match is None:
                match = by_key.get(_row_key(row))

            if match is not None:
                claimed.add(str(match.resolve()))
                res = str(match.resolve())
                updates: dict[str, Any] = {}
                if row["status"] != "ok":
                    added += 1
                    updates["status"] = "ok"
                if row.get("path") != res:
                    updates["path"] = res
                if row.get("error") is not None:
                    updates["error"] = None
                if not dry_run:
                    seconds = _probe_seconds(match)
                    if seconds is not None and abs(
                        seconds - float(row.get("seconds") or 0.0)
                    ) > 1e-9:
                        updates["seconds"] = seconds
                    if updates:
                        store.set_result(conn, spotify_id, **updates)
                continue

            if row.get("path") or row["status"] in ("ok", "missing"):
                if row["status"] != "missing":
                    missing += 1
                    if not dry_run:
                        store.set_result(conn, spotify_id, status="missing", error=None)
                elif not dry_run and row.get("error") is not None:
                    store.set_result(conn, spotify_id, error=None)

        for path in files:
            if str(path.resolve()) not in claimed and on_stray is not None:
                on_stray(str(path))

        return {"added": added, "missing": missing, "failed": failed}
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def _make_resolver(config: Mapping[str, Any]) -> Any:
    try:
        from music.resolve import make_resolver
    except ImportError:
        return None
    return make_resolver(config)


def _apply_success(
    conn: sqlite3.Connection, spotify_id: str, raw_path: str
) -> None:
    path = Path(raw_path)
    kwargs: dict[str, Any] = {"status": "ok", "path": str(path), "error": None}
    seconds = _probe_seconds(path)
    if seconds is not None:
        kwargs["seconds"] = seconds
    store.set_result(conn, spotify_id, **kwargs)


def _apply_failure(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    reason: str,
    max_attempts: int,
) -> str:
    next_attempts = int(row.get("attempts") or 0) + 1
    status = "parked" if next_attempts >= max_attempts else "failed"
    store.set_result(
        conn, row["spotify_id"], status=status, error=reason, bump_attempts=True
    )
    return status


def _sync_dry_run(config: Mapping[str, Any]) -> dict[str, int]:
    from music.ingest import collect_likes

    collected = collect_likes(config) or {}
    likes = list(collected.get("likes") or [])
    index = {row["spotify_id"]: row for row in _rows_readonly(config)}

    added = 0
    failed = 0
    for track in likes:
        row = index.get(str(track["spotify_id"]))
        if row is None:
            added += 1
            continue
        if row["status"] == "parked":
            continue
        if _row_file_exists(row):
            if row["status"] != "ok":
                added += 1
            continue
        if row["status"] == "failed":
            failed += 1
        added += 1

    scanned = scan(config, dry_run=True)
    return {"added": added, "missing": scanned["missing"], "failed": failed}


def sync(config: Mapping[str, Any], dry_run: bool = False) -> dict[str, int]:
    """Ingest likes, place anything not on disk yet, then rescan.

    A failure on one track is recorded on that row and never aborts the batch.
    Parked rows are skipped until someone clears them.  Dry runs are strictly
    read-only: no likes file, no download, no probe, no database write, no
    directory creation.
    """
    if dry_run:
        return _sync_dry_run(config)

    from music.ingest import ingest, read_likes
    from music.resolve import resolve_one

    max_attempts = int(config["max_attempts"])
    ingest(config)
    likes = list(read_likes(config) or [])

    conn = store.connect(config)
    try:
        for track in likes:
            store.upsert_track(conn, track)

        resolver = _make_resolver(config)
        added = 0
        failed = 0

        for track in likes:
            spotify_id = str(track["spotify_id"])
            row = store.get_track(conn, spotify_id)
            if row is None:
                continue
            if row["status"] == "parked":
                continue
            if _row_file_exists(row):
                # The file is already there: never resolve it a second time.
                if row["status"] != "ok":
                    _apply_success(conn, spotify_id, str(row["path"]))
                    added += 1
                continue

            try:
                if resolver is not None:
                    result = resolve_one(config, track, resolver)
                else:
                    result = resolve_one(config, track)
                ok = bool(getattr(result, "ok", False))
                resolved_path = getattr(result, "path", None)
                reason = getattr(result, "error", None)
                if ok and resolved_path and Path(resolved_path).is_file():
                    _apply_success(conn, spotify_id, str(resolved_path))
                    added += 1
                    continue
                if ok:
                    reason = reason or f"resolved path does not exist: {resolved_path!r}"
                else:
                    reason = reason or "resolve failed"
            except Exception as exc:  # one bad track must not stop the batch
                reason = f"{type(exc).__name__}: {exc}"

            _apply_failure(conn, row, str(reason), max_attempts)
            failed += 1

        scanned = scan(config)
        added += scanned["added"]
        missing = scanned["missing"]
        return {"added": added, "missing": missing, "failed": failed}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def report(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Album totals straight out of ``state/library.db``; never the network."""
    try:
        conn = store.connect(config, readonly=True)
    except FileNotFoundError:
        return []
    try:
        return store.album_rows(conn)
    finally:
        conn.close()
