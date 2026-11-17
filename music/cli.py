"""Command line entry point: ``bin/music [--config <path>] <sync|scan|report>``.

All parsing lives here so the CLI is testable without a subprocess; ``bin/music``
is only a launcher.  Config values come from ``--config`` when given, otherwise
from the four ``MUSIC_*`` environment variables (AC24).
"""

import argparse
import json
import os
import sys
from pathlib import Path

from music import _contract as contract
from music import features, ingest, library, mix, playlists, store


class ConfigError(Exception):
    """A config file or the environment does not name every config key."""


def load_config(
    path: str | None = None, env: dict[str, str] | None = None
) -> dict:
    """Build the config dict keyed by ``CONFIG_KEYS``.

    ``path`` wins when given; otherwise every key comes from its ``ENV_VARS``
    variable.  A missing key is an error, not a default (AC24).
    """
    if path is not None:
        source = Path(path)
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read config file {source}: {exc}") from exc
        except ValueError as exc:
            raise ConfigError(f"config file {source} is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"config file {source} is not a JSON object")
        missing = [key for key in contract.CONFIG_KEYS if key not in data]
        if missing:
            raise ConfigError(
                "config file is missing " + ", ".join(missing)
            )
        return {key: str(data[key]) for key in contract.CONFIG_KEYS}

    environ = os.environ if env is None else env
    config = {}
    missing = []
    for key in contract.CONFIG_KEYS:
        variable = contract.ENV_VARS[key]
        value = environ.get(variable)
        if not value:
            missing.append(variable)
        else:
            config[key] = str(value)
    if missing:
        raise ConfigError(
            "no --config given; missing " + ", ".join(missing)
        )
    return {key: config[key] for key in contract.CONFIG_KEYS}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music", description="Self-hosted music pipeline."
    )
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help="JSON config file (default: the MUSIC_* environment variables)",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dry-run", action="store_true",
        help="print what a real run would do, writing neither the index nor the tree",
    )
    commands = parser.add_subparsers(
        dest="command",
        metavar="{sync,scan,report,status,ingest,playlists,features,mix}",
    )
    commands.required = True
    sync_command = commands.add_parser(
        "sync", parents=[common], help="ingest, resolve, then scan"
    )
    sync_command.add_argument(
        "--adopt",
        action="store_true",
        help="record files an interrupted run already placed instead of failing them",
    )
    commands.add_parser(
        "scan", parents=[common], help="reconcile the index with the library tree"
    )
    commands.add_parser(
        "report", parents=[common], help="print one row per album and the total"
    )
    commands.add_parser(
        "status", parents=[common],
        help="count the tracks per status and list what failed",
    )
    commands.add_parser(
        "ingest", help="read the liked tracks into the state directory"
    )
    commands.add_parser(
        "features", help="fetch audio features and artist genres from Spotify"
    )
    mix_command = commands.add_parser(
        "mix", help="write one .m3u per rule set of the mix file"
    )
    mix_command.add_argument(
        "--from", dest="mix_from", metavar="PATH", default=None,
        help="mix file (default: MUSIC_MIX_FILE)",
    )
    playlist_command = commands.add_parser(
        "playlists", help="write one .m3u per playlist of the export"
    )
    playlist_command.add_argument(
        "--from", dest="playlists_from", metavar="PATH", default=None,
        help="playlist export (default: MUSIC_PLAYLISTS_EXPORT)",
    )
    return parser


def _run_sync(config: dict, dry_run: bool, adopt: bool = False) -> int:
    summary = library.sync(config, dry_run=dry_run, adopt=adopt)
    print(library.summary_line(summary))
    return 0


def _run_scan(config: dict) -> int:
    conn = store.open_index(config)
    try:
        result = store.scan(config, conn)
    finally:
        conn.close()
    print(f"indexed {result['indexed']}, missing {result['missing']}")
    return 0


def _run_features(config: dict) -> int:
    token_path = os.environ.get("MUSIC_SPOTIFY_TOKEN") or "/opt/falknest/music-state/spotify.json"
    try:
        counts = features.refresh(config, token_path)
    except (OSError, ValueError, features.spotify.SpotifyError) as exc:
        print(f"music: {exc}", file=sys.stderr)
        return 2
    print(
        f"tracks {counts['tracks']}, features {counts['features']}, "
        f"artists {counts['artists']}, genres {counts['genres']}"
    )
    return 0


def _run_mix(config: dict, source: str | None) -> int:
    path = source or os.environ.get("MUSIC_MIX_FILE") or ""
    if not path:
        print("music: no mix file; pass --from PATH or set MUSIC_MIX_FILE", file=sys.stderr)
        return 2
    try:
        rows = mix.build(config, path)
    except (OSError, ValueError) as exc:
        print(f"music: {exc}", file=sys.stderr)
        return 2
    for row in rows:
        print(f"{row['name']}: {row['tracks']} tracks")
    return 0


def _run_playlists(config: dict, source: str | None) -> int:
    try:
        rows = playlists.build(config, source)
    except ValueError as exc:
        print(f"music: {exc}", file=sys.stderr)
        return 2
    for row in rows:
        print(f"{row['name']}: {row['tracks']} tracks, {row['missing']} missing")
    return 0


def _run_status(config: dict) -> int:
    conn = store.open_index_readonly(config)
    try:
        counts = store.status_rows(conn) if conn is not None else []
        faults = store.fault_rows(conn) if conn is not None else []
    finally:
        if conn is not None:
            conn.close()
    for line in library.status_lines(counts, faults):
        print(line)
    return 0


def _run_ingest(config: dict) -> int:
    result = ingest.ingest(config)
    print(f"total {result['total']}, added {result['added']}")
    return 0


def _run_report(config: dict) -> int:
    conn = store.open_index_readonly(config)
    try:
        rows = store.report_rows(conn) if conn is not None else []
    finally:
        if conn is not None:
            conn.close()
    for line in library.report_lines(rows):
        print(line)
    return 0


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    """Parse ``argv``, dispatch the subcommand, return the exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(arguments)
    except SystemExit as exc:  # argparse already printed the message
        return int(exc.code) if isinstance(exc.code, int) else 2

    try:
        config = load_config(args.config, env)
    except ConfigError as exc:
        print(f"music: {exc}", file=sys.stderr)
        return 2

    if args.command == "sync":
        return _run_sync(config, args.dry_run, getattr(args, "adopt", False))
    if args.command == "scan":
        return _run_scan(config)
    if args.command == "report":
        return _run_report(config)
    if args.command == "status":
        return _run_status(config)
    if args.command == "playlists":
        return _run_playlists(config, getattr(args, "playlists_from", None))
    if args.command == "features":
        return _run_features(config)
    if args.command == "mix":
        return _run_mix(config, getattr(args, "mix_from", None))
    if args.command == "ingest":
        return _run_ingest(config)
    return 2
