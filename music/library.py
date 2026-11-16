"""Sync orchestration, the summary line and the report table.

``sync`` runs ``SYNC_STEPS`` in the declared order, resolving each step lazily
inside the call: a test may pass ``{"ingest": ..., "resolve": ..., "scan": ...}``
of fakes, and only a step that was not supplied is imported from the pipeline
modules.  That keeps this slice buildable and testable on its own.
"""

import importlib
import shutil
import tempfile
from pathlib import Path

from music import _contract as contract
from music import store

#: Module and attribute a step defaults to when ``steps`` does not supply it.
DEFAULT_STEPS = {
    "ingest": ("music.ingest", "ingest"),
    "resolve": ("music.resolve", "resolve_pending"),
    "scan": ("music.store", "scan"),
}


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


def sync(
    config: dict, dry_run: bool = False, steps: dict | None = None
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

        results = list(resolve_step(run_config, pending) or [])
        for result in results:
            store.record_result(conn, result)
            if result.get("ok"):
                added += 1
            else:
                failed += 1

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
