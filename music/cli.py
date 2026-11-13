"""Command line for the music library.

    python -m music.cli scan
    python -m music.cli sync [--dry-run]
    python -m music.cli report
    python -m music.cli ingest
    python -m music.cli sync --config /path/config.json

Exit code 0 means the run completed, even when individual tracks failed.
Exit code 1 means the run itself broke (bad config, unreadable database, or an
unexpected exception); the reason goes to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import library


ENV_KEYS = {
    "root": "MUSIC_ROOT",
    "state": "MUSIC_STATE",
    "likes_export": "MUSIC_LIKES_EXPORT",
    "resolver": "MUSIC_RESOLVER",
    "resolver_args": "MUSIC_RESOLVER_ARGS",
    "ffmpeg": "MUSIC_FFMPEG",
    "ffprobe": "MUSIC_FFPROBE",
    "max_attempts": "MUSIC_MAX_ATTEMPTS",
}

DEFAULTS: dict[str, Any] = {
    "root": str(Path.home() / "Music"),
    "state": str(Path.cwd() / "state"),
    "likes_export": None,
    "resolver": "yt-dlp",
    "resolver_args": [],
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "max_attempts": 3,
}

STRING_KEYS = ("root", "state", "likes_export", "resolver", "ffmpeg", "ffprobe")


class ConfigError(Exception):
    """The configuration cannot be turned into a usable config dict."""


def build_config(
    args: argparse.Namespace, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """defaults <- environment <- --config JSON <- argv flags."""
    env = os.environ if env is None else env
    config: dict[str, Any] = dict(DEFAULTS)
    config["resolver_args"] = list(DEFAULTS["resolver_args"])

    for key, variable in ENV_KEYS.items():
        value = env.get(variable)
        if value is not None and value != "":
            config[key] = value

    config_path = getattr(args, "config", None)
    if config_path:
        try:
            raw = Path(config_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"config {config_path} must contain a JSON object")
        for key, value in data.items():
            if key in config:
                config[key] = value

    for key in STRING_KEYS:
        value = getattr(args, key, None)
        if value is not None:
            config[key] = value
    if getattr(args, "max_attempts", None) is not None:
        config["max_attempts"] = args.max_attempts
    if getattr(args, "resolver_args", None) is not None:
        config["resolver_args"] = args.resolver_args

    config["root"] = str(Path(str(config["root"])).expanduser())
    config["state"] = str(Path(str(config["state"])).expanduser())
    if config.get("likes_export") is not None:
        config["likes_export"] = str(
            Path(str(config["likes_export"])).expanduser()
        )

    resolver_args = config.get("resolver_args") or []
    if isinstance(resolver_args, str):
        resolver_args = shlex.split(resolver_args)
    elif isinstance(resolver_args, (list, tuple)):
        resolver_args = list(resolver_args)
    else:
        raise ConfigError("resolver_args must be a list of strings or a string")
    config["resolver_args"] = [str(item) for item in resolver_args]

    try:
        config["max_attempts"] = int(config["max_attempts"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"max_attempts must be an integer, got {config['max_attempts']!r}"
        ) from exc
    if config["max_attempts"] < 1:
        raise ConfigError("max_attempts must be at least 1")

    return config


def _summary(result: Mapping[str, Any]) -> str:
    return (
        f"added {int(result['added'])}, "
        f"missing {int(result['missing'])}, "
        f"failed {int(result['failed'])}"
    )


def _seconds(value: Any) -> str:
    return f"{float(value):.1f}"


def report_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    header = ["artist", "album", "tracks", "seconds"]
    body = [
        [
            str(row["artist"]),
            str(row["album"]),
            str(int(row["tracks"])),
            _seconds(row["seconds"]),
        ]
        for row in rows
    ]
    body.append(
        [
            "TOTAL",
            "",
            str(sum(int(row["tracks"]) for row in rows)),
            _seconds(sum(float(row["seconds"]) for row in rows)),
        ]
    )
    widths = [
        max([len(header[i])] + [len(row[i]) for row in body]) for i in range(4)
    ]

    def render(cells: Sequence[str]) -> str:
        line = (
            f"{cells[0]:<{widths[0]}}  "
            f"{cells[1]:<{widths[1]}}  "
            f"{cells[2]:>{widths[2]}}  "
            f"{cells[3]:>{widths[3]}}"
        )
        return line.rstrip()

    return [render(header)] + [render(row) for row in body]


def _ingest_line(result: Mapping[str, Any]) -> str:
    return (
        f"liked {int(result.get('liked', 0))}, "
        f"added {int(result.get('added', 0))}, "
        f"total {int(result.get('total', 0))}"
    )


def _add_options(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS keeps options given before the subcommand from being clobbered
    # by the subparser's defaults, so argv genuinely beats the environment and
    # the config file.
    parser.add_argument("--config", default=argparse.SUPPRESS, metavar="PATH")
    parser.add_argument("--root", default=argparse.SUPPRESS)
    parser.add_argument("--state", default=argparse.SUPPRESS)
    parser.add_argument("--resolver", default=argparse.SUPPRESS)
    parser.add_argument(
        "--max-attempts", dest="max_attempts", type=int, default=argparse.SUPPRESS
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music.cli",
        description="Local music library: scan, sync and report.",
    )
    _add_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("scan", "reconcile the file tree with the index"),
        ("sync", "ingest likes and place anything missing"),
        ("report", "album totals from the index"),
        ("ingest", "collect and merge likes without resolving"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        _add_options(sub)
        if name in ("scan", "sync"):
            sub.add_argument(
                "--dry-run",
                action="store_true",
                help="show the summary without changing anything",
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help and usage errors already printed
        return int(exc.code) if isinstance(exc.code, int) else 1

    try:
        config = build_config(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "scan":
            result = library.scan(
                config,
                dry_run=args.dry_run,
                on_stray=lambda path: print(f"stray file: {path}", file=sys.stderr),
            )
            print(_summary(result))
            return 0

        if args.command == "sync":
            print(_summary(library.sync(config, dry_run=args.dry_run)))
            return 0

        if args.command == "report":
            for line in report_lines(library.report(config)):
                print(line)
            return 0

        if args.command == "ingest":
            from music.ingest import ingest

            print(_ingest_line(ingest(config)))
            return 0

        print(f"error: unknown command {args.command!r}", file=sys.stderr)
        return 1
    except (ConfigError, sqlite3.Error, OSError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # defensive: a broken run, not a broken track
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
