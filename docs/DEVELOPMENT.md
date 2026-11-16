# Development — M1-linux-library

How the tool works, every variable, the client steps, tests, update and removal.
The README holds the start; this file holds the rest.

## How it works

`sync` runs three steps in order:

1. `ingest` reads the liked tracks into `<MUSIC_STATE_DIR>/likes.json`, keyed on `spotify_id`, and keeps the order of the source. A flat list and the nested shape of a Spotify API dump are both accepted.
2. `resolve` calls the resolver for each pending track, three calls at a time, converts the output to AAC, tags the file, and places it at `<MUSIC_LIBRARY_ROOT>/<Artist>/<Album>/<NN - Title>.m4a`. One `cover.jpg` per album folder. The result order follows the likes, whatever order the calls finish in.
3. `scan` reconciles `<MUSIC_STATE_DIR>/library.db` with the folder.

One row per track lives in the table `tracks` of `library.db`. The status moves `pending` → `ok` or `failed`. A `failed` track is retried on the next run; after three failures it becomes `parked` and is never resolved again. A row is never deleted, also not when its file disappears.

The resolver contract: the command gets three positional arguments, `<artist> <title> <album>`, and runs without a shell. Exit 0 means line 1 of stdout names the produced audio file. A non-zero exit means stderr carries the reason, and the run continues with the next track.

`MUSIC_RESOLVER=fake` answers from the pipeline itself: it writes a four-second tone into a temp folder and resolves it like any other source. Use it to prove the whole chain without a network, then set the real resolver.

The player is Navidrome. It mounts `MUSIC_LIBRARY_ROOT` read-only at `/music` and serves the folder over the Subsonic API. The player never writes the folder.

## A ready resolver

`bin/resolve-ytdlp` implements the resolver contract with yt-dlp, so the pipeline runs without a hand-written
script:

```sh
MUSIC_RESOLVER="$PWD/bin/resolve-ytdlp" python3 bin/music sync
```

It calls `yt-dlp` from `PATH`, or `~/.local/bin/yt-dlp`, or the binary `YTDLP` names. The search is
`ytsearch1:<artist> - <title> audio`, and the result is cached under `~/.cache/music-pipeline/resolver`
(`MUSIC_RESOLVER_CACHE` moves it): the same artist and title downloads once, and the thumbnail lands next to
the audio, which is where `ensure_cover` looks for it. A cached file answers at once, so a repeated sync costs
no traffic.

Two limits are worth knowing. The search picks the first result, so a live or remix version can win over the
album take. And the `year` tag comes from the like date, not from the recording.

## Playlists

`music playlists --from <file>` reads a Spotify playlist export and writes one `.m3u` per playlist into
`<library_root>/Playlists`. A track reaches the file only when the index holds it with a placed file; the
rest are counted as `missing` and stay out, so a playlist never names a file the library does not have.

Two details decide whether Navidrome picks the files up:

1. An entry in the file is a path **relative to the music folder** (`Artist/Album/01 - Title.m3u`), not a
   host path: inside the player the library sits at `/music`. The command writes it that way.
2. Navidrome imports `.m3u` files during its scan, and only once an admin user exists. `PlaylistsPath`
   defaults to anywhere under the music folder, so the `Playlists` folder needs no configuration.

The export may be a list of playlist objects, an object with a `playlists` list, or an object that maps a
name to its tracks. A playlist is `{"name": …, "tracks": [<like entry>, …]}`; a nested Spotify row works
as well.

## Configuration

The pipeline reads the four `MUSIC_*` variables. The compose file reads the two `NAVIDROME_*` variables and `MUSIC_LIBRARY_ROOT`. A missing key is an error, not a default.

| Variable | Used by | Meaning |
| --- | --- | --- |
| `MUSIC_LIBRARY_ROOT` | `bin/music` | Library root the pipeline writes; mounted as the one Navidrome volume at `/music` (read-only). |
| `MUSIC_STATE_DIR` | `bin/music` | State directory holding `library.db` and `likes.json`. |
| `MUSIC_RESOLVER` | `bin/music` | Resolver command, shlex-split, called as `<resolver…> <artist> <title> <album>`. `fake` writes a four-second tone instead. |
| `MUSIC_SPOTIFY_EXPORT` | `bin/music` | JSON export of liked tracks, used when no `spotify` executable is on `PATH`. A nested Spotify dump is normalised. |
| `MUSIC_FFMPEG` | `bin/music` | The encoder to call, default `ffmpeg`. |
| `MUSIC_FFPROBE` | `bin/music` | The prober to call, default `ffprobe`. |
| `MUSIC_CONCURRENCY` | `bin/music` | Resolver calls that run at once: 1 to 3, default 3. A higher value is cut to 3. |
| `MUSIC_PLAYLISTS_EXPORT` | `bin/music` | The playlist export `music playlists` reads when `--from` is absent. |
| `NAVIDROME_BIND_ADDRESS` | compose | Host address the port is published on (default `127.0.0.1`, loopback only). Set `0.0.0.0` to publish on every interface. |
| `NAVIDROME_PORT` | compose | Host port for the Navidrome web UI and Subsonic API (default `4533`). |
| `NAVIDROME_ADMIN_PASSWORD` | compose | Initial password of the built-in `admin` user. Required — no default; compose fails while it is unset or empty. |

`.env.example` lists the same variables with placeholder values and no real secret. `.env` is required and untracked. `docker compose` reads it from the repository root automatically.

`status` prints one count row per status and then one row per `failed` or `parked` track with its attempt count and the resolver's reason. Those rows are the tracks `sync` will retry, or never retry once parked.

`--config <path>` replaces the environment with a JSON object that carries the four keys `library_root`, `state_dir`, `resolver` and `spotify_export`. The file wins over the environment.

`NAVIDROME_ADMIN_PASSWORD` has no default on purpose: while it is unset or empty, every `docker compose` command fails instead of starting Navidrome with a known password. Navidrome creates the admin user on its first run; change the password in the UI afterwards.

The Navidrome image is pinned by digest (`deluan/navidrome:0.64.2@sha256:38dc2727…`), so a retagged upstream image cannot change what this checkout runs. `docker compose config` validates the compose file without pulling an image once `NAVIDROME_ADMIN_PASSWORD` is set; without it, compose exits non-zero and names the missing variable.

## The player's database

Navidrome keeps users, play counts, played history and the playlists a client creates in its own database at
`/data`. The compose file puts that folder in the named volume `navidrome-data`, so a recreated container keeps
them. `docker compose down --volumes` deletes the volume and with it every user and playlist: the next start
creates the admin user from `NAVIDROME_ADMIN_USER` and `NAVIDROME_ADMIN_PASSWORD` again.

## Connect a client

Server URL: `https://music.example.lan` — replace that host with the name your reverse proxy serves, and append `:NAVIDROME_PORT` only if you changed the port. Navidrome speaks plain HTTP inside the container and is published on `127.0.0.1` by default, so terminate TLS in front of it: point a reverse proxy (Caddy, nginx) on the Docker host at `127.0.0.1:<NAVIDROME_PORT>` and give it a certificate for your name. Clients then connect over HTTPS to the Subsonic API and log in with the `admin` account and `NAVIDROME_ADMIN_PASSWORD`.

Reaching Navidrome directly requires `NAVIDROME_BIND_ADDRESS=0.0.0.0`, which publishes it on every interface as plain HTTP with the admin password in clear text — use it only on a trusted network.

### Amperfy on iOS

1. Install **Amperfy** from the App Store.
2. Open Amperfy and choose **Add Server** (or the plus button on the server list).
3. Enter the server URL, e.g. `https://music.example.lan`.
4. Enter username `admin` and the password from `NAVIDROME_ADMIN_PASSWORD`.
5. Save; Amperfy syncs the library from Navidrome and starts playback.

### SonicLair on Android TV

1. Install **SonicLair** from the Play Store on the Android TV device.
2. Open SonicLair and select **Add server**.
3. Enter the server URL, e.g. `https://music.example.lan`.
4. Enter username `admin` and the password from `NAVIDROME_ADMIN_PASSWORD`.
5. Confirm, then pick the Navidrome library and browse or search for music.

## Troubleshooting

- `docker compose` fails with `NAVIDROME_ADMIN_PASSWORD must be set`: copy `.env.example` to `.env` and fill in `NAVIDROME_ADMIN_PASSWORD`. Every other compose variable has a default; this one deliberately does not.
- A client cannot reach Navidrome: the port is published on `127.0.0.1` only. Reach it through the TLS reverse proxy, or set `NAVIDROME_BIND_ADDRESS`.
- Navidrome shows an empty library: confirm `MUSIC_LIBRARY_ROOT` is the same path the pipeline wrote to and that the files sit under `<Artist>/<Album>/*.m4a`.
- The library is mounted read-only on purpose: the pipeline writes it, Navidrome only reads it.
- A track stays `failed`: the resolver wrote its reason into the `error` column of `library.db`. After three failures the track is `parked`.

## Tests

The suite needs no network. It encodes real files, so `ffmpeg` and `ffprobe` must be installed. It runs 93 tests and takes about six seconds:

```sh
python3 -m unittest discover -s tests
```

## Update

```sh
git -C <clone> pull
```

The state directory and the library stay as they are. A resumed run resolves only the tracks that are still pending.

## Removal

```sh
docker compose down
```

Then delete the checkout, the state directory and the library folder. Nothing outside those three paths is written.

## Layout

| Path | Holds |
|---|---|
| `bin/music` | the launcher; all parsing lives in `music/cli.py` |
| `music/ingest.py` | the Spotify adapter and the merge into `likes.json` |
| `music/resolve.py` | the resolver call, conversion, tagging, placement |
| `music/store.py` | `library.db`: the `tracks` table, `scan` and the report rows |
| `music/library.py` | `sync` orchestration, the summary line, the report table |
| `music/_contract.py` | the shared names; every body raises `NotImplementedError` |
| `spec/` | requirements, design, the lane split |
| `tests/` | the offline suite |
