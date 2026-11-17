"""Sync orchestration, the summary line and the report table.

``sync`` runs ``SYNC_STEPS`` in the declared order, resolving each step lazily
inside the call: a test may pass ``{"ingest": ..., "resolve": ..., "scan": ...}``
of fakes, and only a step that was not supplied is imported from the pipeline
modules.  That keeps this slice buildable and testable on its own.
"""

import importlib
import shutil
import sys
import tempfile
from pathlib import Path

from music import _contract as contract
from music import convert, resolve, store

#: Module and attribute a step defaults to when ``steps`` does not supply it.
DEFAULT_STEPS = {
    "ingest": ("music.ingest", "ingest"),
    "resolve": ("music.resolve", "resolve_pending"),
    "scan": ("music.store", "scan"),
}


def adopt_placed(config: dict, conn, pending: list[dict]) -> list[dict]:
    """Record the pending tracks whose destination already holds a file (AC47).

    A run that a reboot, a kill or a lost session interrupted leaves placed
    files behind while the index still holds those tracks as pending. The next
    run would treat each of those destinations as taken and fail the track, so
    the caller opts in with ``--adopt``: the file at the computed destination
    is taken as this track's own and the row is recorded ``ok`` with the
    duration ffprobe reads from it. Returns the tracks that still need a
    resolver call.
    """
    remaining = []
    for entry in pending:
        destination = resolve.library_path(config, entry)
        if not store.path_is_placed(str(destination)):
            remaining.append(entry)
            continue
        try:
            duration = float(convert.probe_duration(destination))
        except (OSError, RuntimeError):
            duration = None
        store.record_result(
            conn,
            {
                "spotify_id": entry["spotify_id"],
                "ok": True,
                "path": str(destination),
                "error": None,
                "source": None,
                "duration": duration,
            },
        )
    return remaining


def _step(steps: dict | None, name: str):
    """The callable for one step: the override first, else the module default."""
    if steps is not None and steps.get(name) is not None:
        return steps[name]
    module_name, attribute = DEFAULT_STEPS[name]
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


def summary_line(summary: dict) -> str:
    """The one summary line of AC12/AC13, exactly."""
    return (
        f"added {summary['added']}, "
        f"missing {summary['missing']}, "
        f"failed {summary['failed']}"
    )


def _cell(value) -> str:
    return "" if value is None else str(value)


#: The one line that closes the report table (AC17).
TOTAL_LABEL = "TOTAL"


def report_lines(rows: list[dict]) -> list[str]:
    """Header, one aligned row per album, then the TOTAL row (AC16, AC17)."""
    header = tuple(contract.REPORT_ROW_KEYS)
    body = [
        (
            _cell(row["artist"]),
            _cell(row["album"]),
            str(row["tracks"]),
            str(row["seconds"]),
        )
        for row in rows
    ]
    total = (
        TOTAL_LABEL,
        "",
        str(sum(int(row["tracks"]) for row in rows)),
        str(sum(int(row["seconds"]) for row in rows)),
    )
    table = [tuple(str(name) for name in header)] + body
    widths = [max(len(line[index]) for line in table) for index in range(len(header))]

    def render(cells: tuple[str, ...]) -> str:
        padded = (
            cell.ljust(width) for cell, width in zip(cells, widths)
        )
        return "  ".join(padded).rstrip()

    return [render(table[0])] + [render(cells) for cells in body] + [render(total)]


def _count_missing(conn, likes: list[dict]) -> int:
    """Liked tracks without a placed file in the library after the run (AC12).

    A regular file is a placed track; a symlink at the stored path is not, so a
    link can never stand in for a fetched track (AC34, AC35).
    """
    missing = 0
    seen: set[str] = set()
    for entry in likes:
        spotify_id = entry.get("spotify_id")
        if not spotify_id or spotify_id in seen:
            continue
        seen.add(spotify_id)
        row = store.get_track(conn, spotify_id)
        if row is None or row["status"] != contract.STATUS_OK:
            missing += 1
            continue
        if not store.path_is_placed(row["path"]):
            missing += 1
    return missing


def _table(rows: list[tuple[str, ...]], widths: list[int] | None = None) -> list[str]:
    """Render rows as aligned columns, the last one unpadded."""
    if not rows:
        return []
    if widths is None:
        widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]) - 1)]
    lines = []
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row[:-1], widths)]
        lines.append("  ".join(cells + [row[-1]]).rstrip())
    return lines


def status_lines(counts: list[dict], faults: list[dict]) -> list[str]:
    """The count table, then one line per failed or parked track (AC43)."""
    if not counts:
        return ["no index yet"]
    count_rows = [tuple(contract.STATUS_ROW_KEYS)] + [
        (str(row["status"]), str(row["tracks"])) for row in counts
    ]
    lines = _table(count_rows)
    if not faults:
        return lines
    fault_rows = [tuple(contract.FAULT_ROW_KEYS)] + [
        (
            _cell(row["artist"]),
            _cell(row["title"]),
            _cell(row["album"]),
            _cell(row["status"]),
            str(row["attempts"]),
            _cell(row["error"]),
        )
        for row in faults
    ]
    lines.extend(_table(fault_rows))
    return lines


def sync(
    config: dict,
    dry_run: bool = False,
    steps: dict | None = None,
    adopt: bool = False,
) -> dict:
    """Run ingest, resolve and scan; return ``SYNC_SUMMARY_KEYS`` numbers.

    Always all three steps, in ``SYNC_STEPS`` order, on a dry run too (AC25).
    A dry run computes the summary a real run on the same state would print
    (AC13) while writing neither the library tree nor ``state/library.db``
    (AC14): the index is a private in-memory copy, the resolver is pointed at a
    throw-away ``library_root``, and ``likes.json`` may still be written by
    ingest.  A failed track never fails the run (AC15).
    """
    ingest_step = _step(steps, "ingest")
    resolve_step = _step(steps, "resolve")
    scan_step = _step(steps, "scan")

    tempdir = None
    run_config = config
    if dry_run:
        # A private sandbox: the resolver may write files and an index here,
        # but the real tree and state/library.db stay untouched (AC14).
        tempdir = tempfile.mkdtemp(prefix="music-dry-run-")
        run_config = dict(config)
        run_config["library_root"] = str(Path(tempdir) / "library")
        run_config["state_dir"] = str(Path(tempdir) / "state")
        Path(run_config["library_root"]).mkdir(parents=True, exist_ok=True)
        Path(run_config["state_dir"]).mkdir(parents=True, exist_ok=True)
        source_db = store.index_path(config)
        if source_db.exists():
            shutil.copyfile(source_db, store.index_path(run_config))

    conn = None
    added = 0
    failed = 0
    missing = 0
    try:
        ingest_result = ingest_step(config)
        likes = list(ingest_result.get("likes") or [])

        conn = store.in_memory_index(config) if dry_run else store.open_index(config)
        pending = store.pending_tracks(conn, likes)
        for entry in pending:
            store.upsert_track(conn, entry)

        if adopt and not dry_run:
            before = len(pending)
            pending = adopt_placed(run_config, conn, pending)
            adopted = before - len(pending)
            if adopted:
                added += adopted
                print(f"adopted {adopted} placed track(s)", file=sys.stderr)

        stored: set[str] = set()

        def record(result: dict) -> None:
            """Store one result the moment it arrives (AC46)."""
            nonlocal added, failed
            stored.add(str(result.get("spotify_id")))
            store.record_result(conn, result)
            if result.get("ok"):
                added += 1
            else:
                failed += 1

        results = list(resolve_step(run_config, pending, on_result=record) or [])
        for result in results:
            # A step double may return its list without calling ``record``;
            # storing the unseen ones keeps the summary and the index right.
            if str(result.get("spotify_id")) not in stored:
                record(result)

        scan_step(config, conn)
        missing = _count_missing(conn, likes)
        if not dry_run:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()
        if tempdir is not None:
            shutil.rmtree(tempdir, ignore_errors=True)

    return {"added": added, "missing": missing, "failed": failed}
