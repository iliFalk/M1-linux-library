# music

Reads the tracks you liked on Spotify.
Resolves each one through an external resolver and converts it to AAC `.m4a`.
Tags it and writes one `cover.jpg` per album into a plain artist/album tree.
Navidrome serves that tree to a phone, a TV and a browser.

The pipeline writes exactly this layout:

```
<MUSIC_ROOT>/<Artist>/<Album>/<NN - Title>.m4a
<MUSIC_ROOT>/<Artist>/<Album>/cover.jpg
```

## Requirements

- Python 3.14 — **standard library only**, no `pip install`.
- `ffmpeg` and `ffprobe` on `PATH` (or pointed at with `MUSIC_FFMPEG` / `MUSIC_FFPROBE`).
- `docker compose` for Navidrome.
- A resolver of your choice, for example `yt-dlp` — none is bundled (see "Resolver contract").

## Setup

1. `cp .env.example .env`
2. Fill in `.env`: at least `MUSIC_ROOT` (absolute path) and `MUSIC_NAVIDROME_PASSWORD`.
3. Provide your liked tracks: the Spotify adapter uses the `spotify` CLI when it is present,
   otherwise it reads the Spotify JSON export at `MUSIC_LIKES_EXPORT`.
4. `.env` is git-ignored — it is never committed, and `.env.example` holds no real secret.

## Configuration

Every setting is an environment variable (or a key of the same name in lowercase in a
`--config PATH` JSON object). `python -m music.cli --config ./config.json <command>` reads
`{"music_root": "...", "music_state": "...", ...}` instead of the environment.

| Variable | Meaning | Default |
| --- | --- | --- |
| `MUSIC_ROOT` | Library root, the tree the pipeline writes | `<home>/Music` |
| `MUSIC_STATE` | State dir: `likes.json`, `library.db` | `<cwd>/state` |
| `MUSIC_LIKES_EXPORT` | Path to the Spotify JSON export | unset |
| `MUSIC_RESOLVER` | Resolver command; `"fake"` for the offline one | `yt-dlp` |
| `MUSIC_RESOLVER_ARGS` | Extra arguments for the resolver, shell-split | unset |
| `MUSIC_FFMPEG` | `ffmpeg` binary | `ffmpeg` |
| `MUSIC_FFPROBE` | `ffprobe` binary | `ffprobe` |
| `MUSIC_MAX_ATTEMPTS` | Failures before a track is parked | `3` |
| `MUSIC_NAVIDROME_PORT` | Host port Navidrome is published on (compose only) | `4533` |
| `MUSIC_NAVIDROME_USER` | Navidrome admin user (compose only) | `admin` |
| `MUSIC_NAVIDROME_PASSWORD` | Navidrome admin password (compose only) | `change-me` |

The three `MUSIC_NAVIDROME_*` variables are read by `docker-compose.yml`; compose passes the
credentials into the container as `ND_USERNAME` / `ND_PASSWORD` and sets `ND_MUSICFOLDER=/music`.

## Usage

```
python -m music.cli ingest          # read liked tracks into MUSIC_STATE/likes.json
python -m music.cli resolve         # resolve the pending likes to local files
python -m music.cli sync            # resolve, convert, tag, cover, index
python -m music.cli sync --dry-run  # show what sync would do, write nothing
python -m music.cli scan            # re-read MUSIC_ROOT into the sqlite index
python -m music.cli report          # print the library summary
```

`sync` ends with a summary line:

```
added 2, missing 0, failed 0
```

`report` prints one row per album:

```
artist               album              tracks  seconds
Artist Name          Album Name             10     2413
```

A track that fails is retried on the next run and parked after `MUSIC_MAX_ATTEMPTS` (3) failed
attempts. A parked track stays in `library.db` and in the summary's `failed` count; it no longer
stops the run. `sync` exits non-zero only when the run itself broke — a missing or failed track
is data, not an error exit.

## Resolver contract

Resolving is a seam, not a downloader. The command named in `MUSIC_RESOLVER` (default `yt-dlp`)
is called with the track's **artist**, **title** and **album**, and must either

- print the path of the resolved audio file on stdout and exit `0`, or
- exit non-zero with a message on stderr — a clean failure.

A clean failure is recorded in `library.db` and retried on the next run. `MUSIC_RESOLVER="fake"`
selects the in-repo `FakeResolver` used by the test suite, so everything runs offline. This
repository ships the seam and the fake only; bring your own resolver for real downloads.

## Navidrome

`docker-compose.yml` runs one service, `deluan/navidrome:latest`, with one volume,
`${MUSIC_ROOT:-../Music}:/music:ro`. The library is mounted read-only on purpose: the pipeline is
the only writer. Compose reads the optional `.env` (`env_file: [{path: .env, required: false}]`),
so `docker compose config` validates on a fresh clone that has no `.env`.

The run never starts the container. Start it yourself:

```
docker compose up -d
```

Navidrome keeps its own database (users, play counts, playlists, scans) inside the container
layer, not in the mounted volume — that is the price of the single-volume requirement. When the
container is recreated, that state is lost and Navidrome rescans `/music` from scratch: the files
and their tags survive, the listening history and any change made in the web UI do not. Do not
`docker compose down -v` expecting it to come back.

## Connect a client

Server URL: `http://<your-server-ip>:<MUSIC_NAVIDROME_PORT>` (default port `4533`).
Username and password: `MUSIC_NAVIDROME_USER` and `MUSIC_NAVIDROME_PASSWORD` from `.env`.

### Amperfy (iOS)

1. Open the App Store.
2. Search for `Amperfy`.
3. Install Amperfy.
4. Open Amperfy.
5. Choose "Add server".
6. Enter the server URL.
7. Enter the username from `.env`.
8. Enter the password from `.env`.
9. Connect.
10. Browse by artist or album.

### SonicLair (Android TV)

1. Install SonicLair from the Play Store, or sideload its APK.
2. Open SonicLair.
3. Go to Settings.
4. Choose "Server".
5. Choose "Add server".
6. Enter the same server URL.
7. Enter the same username and password.
8. Connect.
9. Browse the library with the remote's direction keys.

Screen and menu names differ between client versions; the steps above name the visible screens
("Settings", "Server", "Add server") and the action on each, so read the wording on your version.
