# M1-linux-library

Turns the tracks you liked on Spotify into a tagged folder that Navidrome serves.

```
artist       album       tracks  seconds
Demo Artist  Demo Album       2      4.0
TOTAL                         2      4.0
```

- **What it is:** a Python command you run on your Linux server, on a timer.
- **What it does:** it reads your liked tracks, finds each one through a resolver you name, converts it to AAC, writes the artist, title, album and track tags, and saves one cover per album. Without it you copy files by hand and the player shows untitled tracks.
- **What it needs:** Python 3.14, ffmpeg, ffprobe, a resolver command such as yt-dlp, and docker compose for the player.
- **How to start:** copy `.env.example` to `.env` and set `MUSIC_ROOT`, then run `python3 -m music.cli sync`. It downloads the first tracks and prints `added N, missing N, failed N`.

## Install

1. Download the repository as an archive and unpack it whole.
2. Install the tools: `sudo apt install ffmpeg`, then a resolver of your choice, for example `pipx install yt-dlp`.
3. Run `cp .env.example .env` and fill in `MUSIC_ROOT` (an absolute path) and `MUSIC_NAVIDROME_PASSWORD`.
4. Run `docker compose up -d`. The player answers at `http://<server>:4533`.

For updates, clone the repository instead: `git clone <url>`, then `git pull` fetches later versions.

## Use

| Command | What it does |
|---|---|
| `python3 -m music.cli sync` | reads the likes, downloads, tags, covers and indexes; prints the summary |
| `python3 -m music.cli sync --dry-run` | prints what a sync would do and writes nothing |
| `python3 -m music.cli report` | prints one row per album |
| `python3 -m music.cli ingest` | reads the liked tracks into the state directory |
| `python3 -m music.cli scan` | re-reads the folder into the index |

## Settings

| Variable | Holds |
|---|---|
| `MUSIC_ROOT` | the folder the pipeline writes and the player serves |
| `MUSIC_STATE` | `likes.json` and `library.db` |
| `MUSIC_LIKES_EXPORT` | path to a Spotify JSON export, used when no `spotify` command exists |
| `MUSIC_RESOLVER` | the resolver command; `fake` runs the offline one |
| `MUSIC_MAX_ATTEMPTS` | failures before a track is parked, default 3 |
| `MUSIC_NAVIDROME_*` | port, user and password for the player container |

## Docs

- `docs/DEVELOPMENT.md` — requirements, the whole configuration table, the resolver contract, the index and the client steps for Amperfy and SonicLair
- `.env.example` — every variable with its default
- `tests/` — the offline suite: `python3 -m unittest discover -s tests`
