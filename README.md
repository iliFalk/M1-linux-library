# M1-linux-library

Turns the music you liked on Spotify into a folder your phone can play.

```
$ python3 bin/music sync
added 2, missing 0, failed 0
$ python3 bin/music report
artist       album       tracks  seconds
Demo Artist  Demo Album  2       8
TOTAL                    2       8
```

- **What it is:** a Python command and a player, both on your own Linux machine.
- **What it does:** it reads your liked tracks and asks a resolver you name for each one, then converts the result to AAC, writes the artist, title, album and track tags, and saves one cover per album. Without it you copy files by hand and the player shows untitled tracks.
- **What it needs:** Python 3.14, ffmpeg, ffprobe and a resolver command such as `yt-dlp`. The player runs as one Compose service.
- **How to start:** copy `.env.example` to `.env` and set the four `MUSIC_*` values, then run `python3 bin/music sync`. It prints `added N, missing N, failed N`.

## Install

1. Download the repository as an archive and unpack it whole.
2. Install the tools: `sudo apt install ffmpeg` and a resolver. `bin/resolve-ytdlp` needs `yt-dlp`; the stand-alone binary from its release page needs no Python.
3. Copy `.env.example` to `.env` and fill in the four `MUSIC_*` values. The two paths are used as written, so give absolute paths or run every command from the unpacked folder.
4. Run `python3 bin/music sync` from the unpacked folder. The first run downloads, converts and tags the tracks it can fetch.
5. Set `NAVIDROME_ADMIN_PASSWORD` in `.env`, then run `docker compose up -d`. The player answers on `http://127.0.0.1:4533`.

For updates, clone the repository instead: `git clone <url>`, then `git pull` fetches later versions.

## Use

| Command | What it does |
|---|---|
| `python3 bin/music sync` | reads the likes, fetches, converts, tags and places the files, then updates the index |
| `python3 bin/music sync --dry-run` | prints what a real run would do, writes neither the folder nor the index |
| `python3 bin/music scan` | reconciles the index with the folder |
| `python3 bin/music report` | prints one aligned row per album with its track count and seconds, then a `TOTAL` row |
| `python3 bin/music status` | counts the tracks per status and lists every failed or parked track with its reason |
| `python3 bin/music playlists --from <file>` | writes one `.m3u` per Spotify playlist into `<library_root>/Playlists`, only for tracks the library holds |
| `python3 bin/music ingest` | reads the liked tracks into the state directory and prints `total N, added N` |
| `python3 bin/music --config <path> sync` | reads the four keys from a JSON file instead of the environment |

A failed track does not change the exit status. Run `python3 bin/music report` and read the index to find it.

## Settings

| Variable | Holds |
|---|---|
| `MUSIC_LIBRARY_ROOT` | the folder the tool writes, mounted read-only into the player at `/music` |
| `MUSIC_STATE_DIR` | `likes.json` and `library.db` |
| `MUSIC_RESOLVER` | the resolver command, called as `<resolver…> <artist> <title> <album>`; `fake` writes a four-second tone instead |
| `MUSIC_SPOTIFY_EXPORT` | a JSON export of liked tracks, used when no `spotify` command is on `PATH`; a flat list and a nested Spotify dump both work |
| `MUSIC_FFMPEG` | the encoder to call, default `ffmpeg` |
| `MUSIC_FFPROBE` | the prober to call, default `ffprobe` |
| `MUSIC_CONCURRENCY` | resolver calls that run at once, 1 to 3, default 3 |
| `MUSIC_PLAYLISTS_EXPORT` | the playlist export `music playlists` reads when `--from` is absent |
| `NAVIDROME_BIND_ADDRESS` | the host address the player port is published on, default `127.0.0.1` |
| `NAVIDROME_PORT` | the port of the player and its API, default `4533` |
| `NAVIDROME_ADMIN_USER` | the name of the admin user Navidrome creates on its first run, default `admin` |
| `NAVIDROME_ADMIN_PASSWORD` | the first password of the `admin` user; required, the player refuses to start without it |

`.env` holds the real values and stays out of git. Every other value has a default.

## Docs

- `docs/DEVELOPMENT.md` — how it works, the resolver contract, the full variable table, client setup, tests, update and removal
- `spec/` — requirements, design and the lane split
- `.env.example` — every variable with a comment
