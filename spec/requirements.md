# Requirements — self-hosted music pipeline (bench14)

Ground truth for this run. One line per testable MUST of the task, `AC<n>  WHEN … THE SYSTEM SHALL …`.
Nothing below the "Surface" block is negotiable; the surface is what makes a criterion observable.

## Surface fixed by this file

CLI: `bin/music [--config <path>] <sync|scan|report|status|ingest|playlists> [--dry-run]`; `sync` also takes `--adopt`., executable, `python3` shebang.
`--dry-run` applies to `sync`, `scan` and `report`.
Config file: JSON object with exactly the keys `library_root`, `state_dir`, `resolver`, `spotify_export`.
`resolver` is a command string (shlex-split, may carry arguments). For one track it is called as
`<resolver…> <artist> <title> <album>` — three positional arguments, in that order.
Resolver success: exit 0, the produced audio file's path on line 1 of stdout.
Resolver failure: non-zero exit, the reason on stderr.
Spotify JSON export: JSON list of objects with the keys `spotify_id`, `artist`, `title`, `album`, `added_at`.
A nested Spotify row (`track.id`, `track.name`, `track.artists[].name`, `track.album.name`, `added_at`) is
normalised to those five keys.
Summary line: `added <N>, missing <N>, failed <N>`.
Report table: first line is the header `artist album tracks seconds`; one further line per album, then one
`TOTAL` line with the summed tracks and seconds.
Index: sqlite3 file `<state_dir>/library.db`, table `tracks` with the columns spotify_id, artist, title,
album, path, status, attempts, error, added_at. `status` is one of `pending`, `ok`, `failed`, `parked`.

## Criteria

AC1  WHEN no `spotify` executable is on PATH THE SYSTEM SHALL read the liked tracks from the JSON export file named by `spotify_export`, a JSON list of objects with the keys spotify_id, artist, title, album, added_at. An object that carries those keys inside `track`, `episode` or `item`, or under the names `id`, `name`, `added` or `addedAt`, SHALL be normalised to the same five keys.
AC2  WHEN a `spotify` executable is on PATH THE SYSTEM SHALL read the liked tracks from the standard output of `spotify liked --json` instead of the export file.
AC3  WHEN ingest runs THE SYSTEM SHALL write `<state_dir>/likes.json` as a JSON list of objects whose keys are exactly spotify_id, artist, title, album, added_at.
AC4  WHEN ingest runs twice on the same source THE SYSTEM SHALL leave likes.json with the same entry count and no duplicate spotify_id.
AC5  WHEN a track is resolved THE SYSTEM SHALL call the resolver command with artist, title and album as three positional arguments in that order.
AC6  WHEN the resolver exits 0 THE SYSTEM SHALL take the produced file path from the first line of its standard output.
AC7  WHEN a track is converted THE SYSTEM SHALL write an AAC-encoded .m4a file carrying the metadata tags artist, title, album, track and year in the iTunes atoms (`ilst`), which a player reads; the `mdta` box is not used.
AC8  WHEN a track is placed THE SYSTEM SHALL put it at `<library_root>/<Artist>/<Album>/<NN - Title>.m4a`, with NN the two-digit position of the track in its album, ordered by added_at and then by title.
AC9  WHEN two tracks of one album are placed THE SYSTEM SHALL leave exactly one cover.jpg, a JPEG image, in that album folder.
AC10 WHEN a track is converted THE SYSTEM SHALL record the duration that ffprobe reports for the written .m4a file.
AC11 WHEN the resolver exits non-zero for one track THE SYSTEM SHALL return a failed result carrying its standard error as the reason and continue with the remaining tracks.
AC12 WHEN `music sync` completes THE SYSTEM SHALL print one line `added <N>, missing <N>, failed <N>`, added the tracks placed by this run, missing the liked tracks absent from the library after the run, failed the tracks whose resolution failed in this run.
AC13 WHEN `music sync --dry-run` runs THE SYSTEM SHALL print the summary line a real sync on the same state would print.
AC14 WHEN `music sync --dry-run` runs THE SYSTEM SHALL leave the library tree and state/library.db byte-identical.
AC15 WHEN one track fails during `music sync` and the run itself completes THE SYSTEM SHALL exit 0.
AC16 WHEN `music report` runs THE SYSTEM SHALL print a first line whose whitespace-separated words are exactly `artist album tracks seconds`, in that order.
AC17 WHEN `music report` runs THE SYSTEM SHALL print one row per album with its artist, album, number of tracks and total seconds read from state/library.db, seconds a whole number, and then one `TOTAL` row holding the sum of both numbers.
AC18 WHEN a library file is deleted by hand and `music scan` runs THE SYSTEM SHALL keep that track's row in state/library.db.
AC19 WHEN `music scan` runs twice on an unchanged tree THE SYSTEM SHALL leave state/library.db unchanged and hold no duplicate spotify_id.
AC20 WHEN the index already holds a track's spotify_id THE SYSTEM SHALL not call the resolver for that track and not overwrite its file.
AC21 WHEN the resolver fails for a track THE SYSTEM SHALL store that track with status failed, its error text and attempts 1.
AC22 WHEN a failed track has fewer than three attempts THE SYSTEM SHALL call the resolver for it again on the next sync.
AC23 WHEN a track reaches three attempts THE SYSTEM SHALL store it with status parked and shall not call the resolver for it again.
AC24 WHEN no `--config` argument is given THE SYSTEM SHALL take library_root, state_dir, resolver and spotify_export from the environment variables MUSIC_LIBRARY_ROOT, MUSIC_STATE_DIR, MUSIC_RESOLVER and MUSIC_SPOTIFY_EXPORT.
AC25 WHEN `music sync` runs THE SYSTEM SHALL call the steps ingest, then resolve, then scan, in that order.
AC26 WHEN `docker compose config` runs in the repository with the variables .env.example names supplied THE SYSTEM SHALL exit 0 without pulling an image.
AC27 WHEN the repository is inspected THE SYSTEM SHALL list .env in .gitignore, track .env.example and hold no credential value.
AC28 WHEN the README and the file it links under Docs are read THE SYSTEM SHALL name every environment variable the pipeline reads and every variable the compose file uses.
AC29 WHEN the README and the file it links under Docs are read THE SYSTEM SHALL hold a section titled "Connect a client" naming the server URL and the steps for Amperfy on iOS and SonicLair on Android TV.
AC30 WHEN docker-compose.yml is read THE SYSTEM SHALL mount the pipeline's library_root as one volume and take the port and the admin credentials from the .env variables named in .env.example.

## Audit fixes (security-audit run music-pipeline-20261115, findings confirmed at 44167cc)

AC31 WHEN `docker compose` runs without NAVIDROME_ADMIN_PASSWORD set THE SYSTEM SHALL fail instead of starting Navidrome with a known default admin password.
AC32 WHEN the compose file publishes the Navidrome port THE SYSTEM SHALL bind the published host address to 127.0.0.1 unless the operator sets an explicit variable for a wider bind.
AC33 WHEN the Navidrome image is named THE SYSTEM SHALL pin it by digest, and the README SHALL show the https URL in "Connect a client".
AC34 WHEN a file is written under library_root THE SYSTEM SHALL refuse a symlink at the destination and leave no file outside library_root.
AC35 WHEN a directory component under library_root is a symlink THE SYSTEM SHALL refuse to follow it, record the track as failed with the reason, and place nothing outside library_root.
AC36 WHEN `cover.jpg` already exists at the album path THE SYSTEM SHALL require a regular file, never write through a symlink, and leave the album with a regular cover.jpg of its own.
AC37 WHEN a file already sits at the computed destination THE SYSTEM SHALL call the resolver for that track and SHALL NOT report the pre-existing file as the fetched track with status ok.
AC38 WHEN two different tracks sanitize to the same file name THE SYSTEM SHALL keep their paths distinct and neither may claim the other's file.

## Ported from the first build (M1-linux-library, merged 2026-11-16)

AC39 WHEN `MUSIC_FFMPEG` or `MUSIC_FFPROBE` is set THE SYSTEM SHALL call that executable instead of the name on PATH.
AC40 WHEN the resolver command is `fake` THE SYSTEM SHALL write a four-second tone in a private temp folder, take it as the resolver's file, and place the track through the normal conversion and guard. The temp file SHALL NOT be written under `library_root`.
AC41 WHEN `music ingest` runs THE SYSTEM SHALL read the liked tracks into `likes.json`, resolve nothing, and print one line `total <N>, added <N>`.

## Second development round (2026-11-16)

AC42 WHEN `music sync` resolves tracks THE SYSTEM SHALL run at most three resolver calls at once, the number set by `MUSIC_CONCURRENCY` between one and three, and SHALL return one result per track in the order of the likes whatever order the calls finish in.
AC44 WHEN `music playlists --from <file>` runs THE SYSTEM SHALL write one `.m3u` file per playlist of the export into `<library_root>/Playlists`, each holding only the tracks the index marks `ok` with a placed file, and SHALL print one line `name: <N> tracks, <M> missing` per playlist.
AC46 WHEN `music sync` resolves tracks THE SYSTEM SHALL store each result in the index as soon as the call returns, so a run that dies mid-way keeps every finished track as `ok` and the next run resolves only the rest.
AC48 WHEN `bin/spotify-fetch` runs THE SYSTEM SHALL read the liked tracks from `GET /me/tracks` and the playlists from `GET /me/playlists` plus each playlist's tracks, and SHALL write `likes.json` and `playlists.json` in the shapes `music sync` and `music playlists` read, one entry per Spotify id, playlist order kept.
AC49 WHEN `bin/spotify-fetch` runs THE SYSTEM SHALL exchange the stored refresh token for an access token, write the refreshed token back with mode 600, and SHALL fail with the missing key named when the file holds no `refresh_token`.

AC47 WHEN `music sync --adopt` runs and the computed destination of a pending track already holds a regular file THE SYSTEM SHALL record that track `ok` with that path and the duration ffprobe reads, and SHALL NOT call the resolver for it.

AC45 WHEN a playlist entry is written THE SYSTEM SHALL write the track path relative to the playlist file, so the player resolves it against the folder that holds the playlist (`../Artist/Album/file.m4a`), and SHALL name a missing source in the error text.

AC43 WHEN `music status` runs THE SYSTEM SHALL print one row per status with its track count (pending, ok, failed, parked) and then one row per failed or parked track with artist, title, album, status, attempts and the resolver's reason. Without an index it SHALL print `no index yet`.
