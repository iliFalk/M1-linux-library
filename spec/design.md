# Design — cross-slice contract (lane `contract`)

What `music/_contract.py` declares, and the parts of the data model a signature
cannot express. `spec/requirements.md` is ground truth; this file only explains
and pins the shared shapes so the three slices agree. Every body in
`music/_contract.py` raises `NotImplementedError` — it is a declaration surface,
not an implementation.

## 1. Vocabulary and where it comes from

| Name | Value | Source |
| --- | --- | --- |
| `LIKES_KEYS` | `spotify_id, artist, title, album, added_at` | AC1/AC3 |
| `TRACK_COLUMNS` | `spotify_id, artist, title, album, path, status, attempts, error, added_at` | Surface |
| `CONFIG_KEYS` | `library_root, state_dir, resolver, spotify_export` | Surface |
| `ENV_VARS` | one `MUSIC_*` variable per config key | AC24 |
| `SYNC_STEPS` | `ingest, resolve, scan` | AC25 |
| `STATUS_*` | `pending, ok, failed, parked` | Surface/AC21–23 |
| `MAX_ATTEMPTS` | `3` | AC23 |

`ENV_VARS` is keyed by `CONFIG_KEYS`, so a slice that reads the environment walks
one mapping instead of hard-coding variable names. A test asserts that key set.

Decision: constants are tuples, not lists or sets. Reason — order is part of what
they promise (JSON key order, SQL column order, the order of a report row), and a
tuple cannot be mutated by a slice that imports it.

## 2. Data model

### Config object
A plain dict with exactly `CONFIG_KEYS`. `library_root` and `state_dir` are
strings that the slices turn into `Path` when needed; `resolver` is the command
string exactly as written (arguments included); `spotify_export` is the export
file path. `load_config` returns it fully populated — no slice reads
`os.environ` for a config value.

Decision (assumption): a key that is absent from both the config file and the
environment is an error, not a default. Reason — AC24 names exactly these four
variables and the Surface says the file holds *exactly* these four keys; a
silent default (e.g. `.` or `~/Music`) would let a misconfigured run write into
the wrong tree. Precedence: explicit `--config` file first, else environment.
`env=None` means "use `os.environ`"; passing a dict makes the function testable.

### `likes.json`
`<state_dir>/likes.json`, a JSON list of objects whose keys are exactly
`LIKES_KEYS` and whose values are all strings. `added_at` is ISO-8601 text; AC8
orders by `added_at` then `title`, so lexicographic order of that text must equal
chronological order (store the source value unchanged; do not reformat).
Identity is `spotify_id`: ingest dedupes on it, so a second run leaves the count
unchanged (AC4). `ingest()` returns the whole list under `"likes"` because
`sync()` hands that list straight to `pending_tracks()`; `"total"` is what the
source yielded after dedupe, `"added"` is the number newly appended by this run.

### Table `tracks`
`<state_dir>/library.db`, sqlite, one row per `spotify_id` (primary key). Roles
of `TRACK_COLUMNS`:

| column | type | role |
| --- | --- | --- |
| `spotify_id` | TEXT PK | identity, from `likes.json` |
| `artist`, `title`, `album` | TEXT | display and resolver arguments |
| `path` | TEXT NULL | final `.m4a` under `library_root`; NULL until placed |
| `status` | TEXT | one of `STATUS_*`; starts `pending` |
| `attempts` | INTEGER | resolver invocations that failed; `0` before the first |
| `error` | TEXT NULL | resolver stderr on failure, NULL on success |
| `added_at` | TEXT | ordering only, see above |

Lifecycle: `pending` → `ok` (file placed, path set) or `failed`
(error set, `attempts` incremented); `failed` with `attempts >= MAX_ATTEMPTS`
becomes `parked` and is never resolved again (AC22, AC23). An `ok` row is never
resolved again and its file is never overwritten (AC20). A row is never deleted,
including when its file disappears from disk (AC18); `scan` may clear `path` in
that case but the row stays.

`get_track` returns a dict keyed by `TRACK_COLUMNS` (all nine keys, NULLs as
`None`), or `None`. `upsert_track`/`record_result` accept those same keys, so a
row and a result dictionary round-trip. `record_result` takes one
`TRACK_RESULT_KEYS` dict and applies the lifecycle above — it is the only place
attempt counting happens, so AC21–23 have one implementation.

`pending_tracks(conn, likes)` returns the like entries that still need a resolver
call: absent from `tracks`, or `failed` with `attempts < MAX_ATTEMPTS`. Parked and
`ok` entries are excluded. The returned list is ordered as `likes` was, which is
also the order resolver calls happen in.

## 3. Result shapes

Every dict-returning name has a `*_KEYS` tuple that fixes its keys *and their
order*. A slice that builds a dict in the wrong order or with an extra/missing
key must fail in its own worktree against these tuples.

- `INGEST_RESULT_KEYS` — `total`, `added`, `likes` (the full likes list).
- `TRACK_RESULT_KEYS` — `spotify_id`, `ok`, `path`, `error`, `source`.
  `ok=True`: `path` is the placed `.m4a`, `error=None`. `ok=False`: `path=None`,
  `error` is the resolver's stderr text. `source` is what the resolver named on
  stdout line 1 (AC6), i.e. the file that was converted into `path`; on failure
  it is `None`. Assumption: `source` always means that resolver-provided file,
  never a `likes.json` origin.
- `SCAN_RESULT_KEYS` — `indexed` (rows found on disk / reconciled by this scan),
  `missing` (rows whose file is gone; the rows are kept — AC18).
- `SYNC_SUMMARY_KEYS` — `added`, `missing`, `failed`; exactly the three numbers
  printed on the AC12 summary line.
- `REPORT_ROW_KEYS` — `artist`, `album`, `tracks` (count), `seconds` (whole
  number, AC17).

Decision (assumption): `seconds` is a `float` summed by sqlite/report code and
rounded to a whole number only when printed; the dict carries the number the
report prints as an `int`. Reason — AC17 says seconds is a whole number and the
report is the only consumer.

## 4. Resolver and media boundary

`resolver_argv(config)` returns `shlex.split(config["resolver"])` — the base
argv with its own arguments. `source_file` appends `artist`, `title`, `album`
as three separate argv items in that order and never uses a shell (AC5). When
the base argv is exactly `fake`, no subprocess runs: `fake_source` encodes one
four-second tone in a private temp folder and that file is the source (AC40),
so the placement guard and the tagger see a normal resolver result. Exit 0
plus the first stdout line is success (AC6); non-zero is a failure whose reason
is the stderr text (AC11), and one failure must not stop the loop in
`resolve_pending` — it returns one `TRACK_RESULT_KEYS` dict per input track, in
input order.

`convert_to_m4a`, `probe_duration` and `write_tags` are the only names that talk
to external tools. They call `convert.tool(FFMPEG, FFMPEG_VAR)`, which reads
`MUSIC_FFMPEG` / `MUSIC_FFPROBE` at call time, so a machine can name its own
binaries without touching the config object (AC39). They take `Path`s, not commands, so the contract does not
declare ffmpeg/ffprobe as dependencies (this lane declares none). `write_tags`
takes the five AAC tags of AC7 (`artist`, `title`, `album`, `track` — one-based
position — and `year`) as a `dict[str, str]`; a tag key outside AC7 is a
slice-level choice, not a contract change. `probe_duration` measures the written
`.m4a` (AC10).

`ensure_cover(album_dir, cover_src=None)` returns the path of the one
`cover.jpg` (AC9). It is idempotent: an existing valid JPEG is returned as is, so
two tracks of one album leave one cover. `cover_src` is optional because AC9
demands a JPEG even when the source has no artwork; what is embedded when
`cover_src` is `None` is a slice decision.

`FakeResolver` is the resolver double for tests: constructed from a config and
an optional `sources` mapping, its `resolve(artist, title, album)` returns
`TRACK_RESULT_KEYS`. Decision (assumption): a key missing from `sources` yields
`ok=False` with an error string, so the retry/parking path (AC21–23) is testable
without ffmpeg or network. It never touches `library_root`.

## 5. Orchestration and CLI

`sync` runs `SYNC_STEPS` in the declared order (AC25), passing the
`steps` dict as an override so a test can stub a step; the value for a key is the
callable used for that step. It returns `SYNC_SUMMARY_KEYS`. With
`dry_run=True` it computes the summary a real run would print but performs no
write at all — neither the library tree nor `state/library.db` (AC13, AC14), so
dry-run must still resolve and scan in memory/temp space. A failed track does
not change the exit status (AC15); `sync` returns numbers and `main` returns 0.

`main(argv=None, env=None)` is the `bin/music` entry point: it parses
`[--config <path>] <sync|scan|report|ingest> [--dry-run]`, dispatches, prints
the summary line, the report table or the ingest line, and returns the exit
code. `ingest` runs the same step `sync` starts with and resolves nothing
(AC41). `env` is passed to
`load_config`. Decision (assumption): `bin/music` stays a two-line launcher that
calls `main()`; all parsing lives in `main`, so the CLI is testable without a
subprocess.

## 6. What the three slices must not change

- Do not edit `music/_contract.py` while implementing. Add implementations in
  your own modules (`music/ingest.py`, `music/resolver.py`, `music/index.py`,
  `music/cli.py`, whichever the tasks assign) and import these names from here.
- Do not rename a constant, reorder its elements, change `MAX_ATTEMPTS` or the
  `STATUS_*` strings, or change a signature/annotation. `tests/test_contract.py`
  asserts each of them.
- Do not return dicts with extra keys or another key order; assert your own
  outputs against the `*_KEYS` tuples.
- Do not widen `TRACK_RESULT_KEYS` to carry extra per-track state (e.g. tags,
  duration); store that in the database instead.

## 7. Left open deliberately

These are slice decisions, not contract gaps the installers may invent on their
own: filesystem sanitising of `artist`/`album`/`title` (assume path separators
are replaced, unicode kept), `NN` beyond 99 (assume at least two digits), the
tie-break when `added_at` and `title` are equal (assume source order), how
"is a JPEG" is verified (assume magic bytes), how missing files are reconciled
without deleting rows (AC18), report row order (assume artist then album), and
the exact `bin/music` arg-parsing messages. Docker/compose, README and
`.env.example` (AC26–30) are outside this lane.

## 8. Parallel resolve and the status table (AC42, AC43)

`resolve_pending` keeps a thread pool of `workers()` threads: `MUSIC_CONCURRENCY` read at call time, cut to
the `MAX_WORKERS` ceiling of three. Placement stays single-threaded by construction — `convert.reserve`
claims each destination with `O_EXCL | O_NOFOLLOW`, so two threads cannot take the same name, and the index
is written after the pool closes, one `record_result` per result, in the input order. The result list is
filled by slot index, not by completion order, so AC11's ordering promise holds.

`status_rows` counts per status and always returns the four contract statuses, zero included, so the table
shape does not move between runs. `fault_rows` returns `failed` before `parked`: the first group is retried
by the next sync, the second never is.

## 9. Playlists (AC44, AC45)

`music playlists` is a separate command, not a sync step: a playlist is a view over the library, so it must
not cost a resolver call. The export is read with the same row normaliser as the likes, so one reader serves
both. `playlists.render` maps `spotify_id` to the placed path through the index; a track without an `ok` row
or without a file is counted and dropped.

The entry is written relative to `library_root` because that folder is mounted at `/music` inside the player.
Navidrome resolves an `.m3u` entry against its music folder, so a host path would name nothing. The file
lands in `Playlists/`, which `PlaylistsPath` (default: anywhere under the music folder) already covers.

## 10. Tags reach a player (AC7)

`write_tags` writes the iTunes atoms, not the `mdta` box: `-movflags use_metadata_tags` produced a file that
ffprobe reads and taglib does not, so Navidrome indexed the whole library as `[Unknown Artist]`. The year goes
through ffmpeg's `date` key, because the muxer only writes `©day` for that name.

## 11. A run that dies (AC46, AC47)

`resolve_pending` takes an optional `on_result` callback and calls it with each result as it arrives, so
`library.sync` writes it through `store.record_result` at once instead of after the whole list. A reboot or a
killed session therefore costs at most the calls that were in flight, not the finished tracks.

The files a dead run placed stay in the tree while their rows are still `pending`. On the next run the computed
destination is occupied, which the guard refuses by design (AC37) — the track would fail three times and park.
`sync --adopt` is the operator's answer: it records a pending track whose destination holds a regular file as
`ok` with the duration ffprobe reads, and resolves only the rest. It is opt-in, so the ordinary run keeps the
refusal that stops one track from claiming another one's file.
