# garmin-data

Local, long-term collection of Garmin Connect training and recovery data for
analysis across weeks, months and training cycles.

It downloads data through [python-garminconnect](https://github.com/cyberjunky/python-garminconnect),
keeps **every raw JSON response** as immutable source data, and builds a small set
of normalized tables in a single [DuckDB](https://duckdb.org) file that you can query from
Python or SQL.

**What it is not:** a Garmin Connect clone, a dashboard, a per-workout analysis
tool, or a training model. There are no fitness/fatigue models, readiness scores,
predictions or recommendations. It is a reliable historical dataset to build
those on.

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
git clone <this repo> && cd garmin-data
uv sync
uv run garmin-data --help
```

## Authentication

Credentials are **never stored** by this project. python-garminconnect caches an
OAuth session in `garmin_tokens.json` (file mode 0600, directory 0700) and refreshes it
automatically.

### First login

```bash
uv run garmin-data login
```

You are prompted for your email, password and, if enabled, an MFA code. For a
non-interactive first login, set `GARMIN_EMAIL` / `GARMIN_PASSWORD` for that single
command only (don't put them in files in this repository).

The session is written to `~/.garminconnect/garmin_tokens.json` by default. Change
this with `GARMINTOKENS=/some/dir` or `auth.tokenstore` in the config.

### Reusing the cached session

Every other command (`sync`, …) loads the cached session and never asks for a
password. If the session expires or is rejected, the command stops with a message
asking you to run `garmin-data login` again. The token store is compatible with
other python-garminconnect tools, so an existing `~/.garminconnect` works as-is.

## Configuration

Optional. Copy `config.example.toml` to `./config.toml` or
`~/.config/garmin-data/config.toml`. Environment variables take precedence:

| Variable | Meaning | Default |
|---|---|---|
| `GARMIN_DATA_DIR` | data directory (DB, exports, log) | `~/.local/share/garmin-data` |
| `GARMINTOKENS` | session cache directory | `~/.garminconnect` |
| `GARMIN_DATA_CONFIG` | explicit config file path | – |

## Usage

```bash
# Sync a date range (activities + daily health).
uv run garmin-data sync --from 2026-08-01 --to 2026-09-25

# Incremental: from the last synced day (minus a 3-day overlap) to today, plus a
# retry of anything still missing from earlier syncs. Empty database: last 30 days.
uv run garmin-data sync

# Re-download data already stored in the range.
uv run garmin-data sync --from 2026-09-01 --to 2026-09-25 --refresh

# Also retry earlier gaps during an explicit range sync.
uv run garmin-data sync --from 2026-09-01 --to 2026-09-25 --fill-gaps

uv run garmin-data status                   # what is stored, runs, gaps, recent errors
uv run garmin-data rebuild                  # rebuild normalized tables from raw (offline, atomic)
uv run garmin-data export --format parquet  # or csv; to <data_dir>/exports
```

Syncing is idempotent: a range can be synced any number of times without
creating duplicates. Already-stored data is skipped, except for the last few days
(`incremental_overlap_days`), which are always re-fetched because Garmin revises
recent sleep, HRV and readiness values, and activity names or RPE are often edited
after the run.

One failing endpoint never stops a sync. Errors are logged to `sync_log`, the log
file (`<data_dir>/garmin-data.log`) and the console. Only authentication failures
and repeated rate limiting (HTTP 429, after backing off) abort a run.

**Gaps are retried.** Every run records its requested range in `sync_run`. A
day, month or activity detail in any requested range that has no raw payload,
because its request failed or the run was aborted before reaching it, is a gap.
`garmin-data sync` without arguments retries all gaps, however old. An item that
failed `max_fetch_attempts` times (default 3) is marked "given up" and shown in
`status`. This covers e.g. days before your account had data. Auth and rate-limit
failures don't count as attempts. Run an explicit `sync --from/--to` over the
range to try such items again.

**Normalization errors** are isolated and logged in `sync_log` with status
`normalize_error`. A changed response shape counts as an error, not as "no
data". The normalizers distinguish Garmin's empty responses (`null`, `[]`, `{}`, or
known fields with null values) from a non-empty response that lacks the expected
keys, and raise `UnexpectedPayload` for the latter. The sync still finishes, with
exit code 3:

- activity: nothing is written for it; its previous rows (summary, laps, splits,
  zones) stay as they were.
- daily payload: on the days it covers, the columns that source provides keep
  their previous values. Columns from other, healthy payloads are still updated,
  including a column the failed source shares with a healthy source of higher
  precedence (e.g. `resting_hr` from the daily summary vs. the sleep fallback).
`rebuild` is all-or-nothing: on any error it rolls back and leaves the existing
tables unchanged.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success, everything requested was fetched and normalized |
| 1 | aborted / fatal (auth failure, rate limit, failed rebuild, no session) |
| 2 | invalid arguments |
| 3 | finished, but some requests or normalizations failed (data incomplete; rerun `sync` later) |

### Manual annotations

```bash
uv run garmin-data note add --activity 1234567890 --fatigue 6 --legs heavy --notes "windy"
uv run garmin-data note add --date 2026-09-24 --soreness "left achilles" --motivation 4
uv run garmin-data note list --from 2026-09-01
uv run garmin-data note edit 3 --rpe 8
uv run garmin-data note delete 3
```

Fields: `rpe` (1–10), `fatigue`, `motivation` (integers, suggested 1–10), `legs`,
`soreness_or_pain`, `notes` (text). Notes live in the `annotation` table, which sync
and rebuild never write to. Garmin's own post-workout RPE and "feel" are imported
separately into `activity.garmin_rpe` / `activity.garmin_feel`.

## Storage layout

```
<data_dir>/
  garmin.duckdb       all data
  garmin-data.log     sync log
  exports/            output of `garmin-data export`
```

Data layers inside `garmin.duckdb`:

```
raw_payload            Garmin JSON exactly as returned (immutable source of truth)
    │   normalize (pure functions; `rebuild` re-runs them offline)
    ▼
activity, activity_lap, activity_split, activity_hr_zone, daily_health
    │
    ▼
v_*  views             derived metrics, computed at query time
annotation             manual notes, independent of Garmin data
```

| Table | Key | Content |
|---|---|---|
| `raw_payload` | endpoint, source_key, sha256 | Raw responses. Same content is stored once (only `last_fetched_at` is bumped); changed content is appended as a new version. Payloads are never modified. `source_key` is an activity id, a date, or a calendar month (`YYYY-MM`) for range endpoints. |
| `activity` | activity_id | One row per activity: time, type, distance, durations, pace, HR, cadence, stride, elevation, power, training effect/load, calories, Garmin RPE/feel, weather, device, gear. |
| `activity_hr_zone` | activity_id, zone | Time in HR zones. |
| `activity_lap` | activity_id, sequence | Device laps with `step_type`, the raw Garmin `intensityType`, workout step index and repeat group. |
| `activity_split` | activity_id, sequence | Garmin "typed splits": one row per executed workout step (`INTERVAL_ACTIVE`, `INTERVAL_RECOVERY`, …, with the laps it covers) and detected run/walk segments (`RWD_RUN`, `RWD_WALK`). |
| `daily_health` | date | One row per day (including rest days): resting HR, HRV, sleep, Body Battery, Training Readiness and its factors, recovery time, stress, steps, calories, weight, VO2max. |
| `annotation` | id | Manual notes. |
| `sync_run` | run_id | Each sync run: requested date range and final status (`ok`/`partial`/`aborted`). |
| `sync_log` | – | Per-request outcome of each sync run. |

Units: metres, seconds, m/s, pace in s/km, kg, °C, kcal. All `*_utc` / `*_at`
timestamps are UTC; `start_time_local` is local time for the activity's `timezone`.

### Querying

```python
from pathlib import Path
import duckdb

con = duckdb.connect(str(Path("~/.local/share/garmin-data/garmin.duckdb").expanduser()), read_only=True)
con.sql("""
    SELECT a.date, l.sequence, round(l.distance_m) AS m, l.avg_pace_s_per_km, l.avg_hr
    FROM activity_lap l JOIN activity a USING (activity_id)
    WHERE l.step_type = 'work' AND l.distance_m BETWEEN 380 AND 620
    ORDER BY a.date
""").show()
```

Raw JSON can be queried directly, e.g.
`SELECT payload->>'$.summaryDTO.averageHR' FROM raw_payload WHERE endpoint='activity'`.

## Garmin data sources and limitations

Endpoints used (python-garminconnect method → `raw_payload.endpoint`):

| Data | Method | Requests |
|---|---|---|
| activity list | `get_activities_by_date` | per calendar month |
| activity detail | `get_activity`, `get_activity_splits`, `get_activity_typed_splits`, `get_activity_hr_in_timezones`, `get_activity_weather`, `get_activity_gear` | per activity |
| workout definition | `get_workout_by_id` | per structured workout |
| devices | `get_devices` | per sync |
| daily | `get_user_summary`, `get_sleep_data`, `get_training_readiness` | per day |
| daily ranges | `get_hrv_data_range`, `get_max_metrics_range`, `get_weigh_ins` | per calendar month |

Known limitations:

- **Step classification.** `activity_lap.step_type` comes only from the lap's
  `intensityType` as recorded by the watch: `WARMUP`→warmup, `COOLDOWN`→cooldown,
  `RECOVERY`/`REST`→recovery, `ACTIVE`→work **only inside a structured workout**.
  Laps of unstructured runs (auto-laps report `INTERVAL`) and laps after a workout
  ended are `unknown`. Nothing is inferred from pace or HR. The raw value is kept in
  `step_type_raw`. A warmup defined as a plain "interval" step in Garmin will show
  as `work`.
- **Workout definitions are mutable.** Garmin only serves the *current* version of
  a workout. If it was edited after the run, its steps no longer match the laps.
  `repeat_group` and `workout_step_type` are therefore filled only from a stored
  workout version whose `updatedDate` predates the activity (assumed to be UTC).
  Every fetched version is kept, so syncing soon after a run gives the best
  coverage. For older activities of edited workouts these columns stay NULL, while
  `workout_step_index` (from the watch) is always kept.
- **Repeat iterations** are not numbered. Use `activity_split` (one row per executed
  step) or consecutive `workout_step_index` values.
- **Request volume.** About 3 requests per day plus about 7 per activity. A year of
  history is roughly 1,500–3,000 requests, so run long backfills in pieces
  (e.g. month by month). The default pause between requests is 0.5 s.
- **Monthly range requests.** Range endpoints are always requested for a whole
  calendar month (clipped to today), whatever range you sync. So every day is
  covered by exactly one payload per endpoint, and syncing a sub-range can never
  leave an older, overlapping payload that competes with a newer one. Some
  Garmin range endpoints reject long ranges ("requested date range is too big"),
  and a month is within their limits. Body Battery range data is not fetched;
  daily high/low/wake values come from the daily summary. Because range payloads
  are monthly, `daily_health` gets a row for every day of each synced month.
- **Library reshaping.** `get_sleep_daily`, `get_rhr_daily` and
  `get_calories_daily` return reshaped data rather than the Garmin response, so they
  are not used. The same values come from per-day endpoints that return raw JSON.
- **Units in payloads.** Weather temperature is in °F (converted to °C). Weight is in
  grams (converted to kg). Stride length is in cm (converted to m). Garmin RPE is
  10–100 (stored as 1–10). Garmin "feel" is 0–100 (0 very weak, 25 weak, 50
  normal, 75 strong, 100 very strong). Negative stress values mean "not enough
  data" and are stored as NULL.
- **Cadence** is running cadence only. Other sports' cadence fields are left in raw.
- **VO2max** appears only on days Garmin (re)computed it. `weight_kg` appears only on
  days with a weigh-in.
- **Recovery time** is taken from the morning Training Readiness entry, in minutes.
- **Sleep timestamps.** Some accounts (reported for connect.garmin.cn) have wrong
  local sleep timestamps, so only the `*GMT` values are used.
- **Not included in V1:** FIT files, per-second time series, and training-plan data.
  The raw layer and `activity_id` keys are designed so that FIT downloads
  (`download_activity`) can be added as a separate table/directory later.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format .

# Dump sample payloads from every endpoint (for mapping new fields).
# Writes to ./probe_output/, which is gitignored and contains personal data.
uv run python scripts/probe.py 2026-09-24 [activity_id]
```

Adding a field: check the raw payload (`probe.py` or `raw_payload`), extend the
normalizer and the schema (add a migration in `store.py`), then run
`garmin-data rebuild`. No re-download is needed.

Code layout: `src/garmin_data/` is the library (`sources` fetch, `normalize/` pure
mapping, `build` raw→tables, `store` DuckDB, `sync` orchestration) and `cli.py` is a
thin argparse layer on top.

## License

MIT placeholder, see `LICENSE`. The final license has not been chosen yet.
