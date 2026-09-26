-- Schema v1. All *_at / *_utc timestamps are naive UTC.
--
-- Layers:
--   raw_*        immutable Garmin responses (source of truth)
--   activity*, daily_health
--                normalized; always rebuildable from raw_payload (`garmin-data rebuild`)
--   annotation   manual data; never written by sync or rebuild
--   v_*          derived views (computed at query time, clearly marked)

CREATE TABLE IF NOT EXISTS raw_payload (
    endpoint          VARCHAR   NOT NULL,  -- logical endpoint name, e.g. 'activity', 'sleep_data'
    source_key        VARCHAR   NOT NULL,  -- stable id: activity id, date, or 'YYYY-MM-DD..YYYY-MM-DD'
    sha256            VARCHAR   NOT NULL,  -- hash of canonical JSON; new content => new row
    date_from         DATE,                -- date coverage (for daily/range endpoints)
    date_to           DATE,
    first_fetched_at  TIMESTAMP NOT NULL,
    last_fetched_at   TIMESTAMP NOT NULL,
    payload           JSON,                -- exact response as returned by python-garminconnect
    PRIMARY KEY (endpoint, source_key, sha256)
);

CREATE TABLE IF NOT EXISTS activity (
    activity_id            BIGINT PRIMARY KEY,
    date                   DATE,
    start_time_local       TIMESTAMP,
    start_time_utc         TIMESTAMP,
    timezone               VARCHAR,
    activity_type          VARCHAR,
    activity_name          VARCHAR,
    distance_m             DOUBLE,
    duration_s             DOUBLE,
    moving_time_s          DOUBLE,
    elapsed_time_s         DOUBLE,
    avg_speed_mps          DOUBLE,
    max_speed_mps          DOUBLE,
    avg_pace_s_per_km      DOUBLE,
    best_pace_s_per_km     DOUBLE,
    avg_hr                 DOUBLE,
    max_hr                 DOUBLE,
    avg_cadence            DOUBLE,   -- running cadence, steps/min
    max_cadence            DOUBLE,
    avg_stride_length_m    DOUBLE,
    elevation_gain_m       DOUBLE,
    elevation_loss_m       DOUBLE,
    avg_power              DOUBLE,
    max_power              DOUBLE,
    normalized_power       DOUBLE,
    aerobic_te             DOUBLE,
    anaerobic_te           DOUBLE,
    training_load          DOUBLE,
    calories               DOUBLE,
    garmin_rpe             DOUBLE,   -- Garmin self-evaluation, scaled to 1-10 (raw value / 10)
    garmin_feel            INTEGER,  -- Garmin self-evaluation 0..100 (0 very weak, 50 normal, 100 very strong)
    workout_compliance     DOUBLE,   -- Garmin directWorkoutComplianceScore
    workout_id             BIGINT,
    temperature_c          DOUBLE,
    apparent_temperature_c DOUBLE,
    humidity_pct           DOUBLE,
    weather_desc           VARCHAR,
    device_id              BIGINT,
    device                 VARCHAR,
    gear                   VARCHAR,
    normalized_at          TIMESTAMP
);

CREATE TABLE IF NOT EXISTS activity_hr_zone (
    activity_id     BIGINT  NOT NULL,
    zone            INTEGER NOT NULL,
    seconds         DOUBLE,
    zone_low_hr     INTEGER,
    PRIMARY KEY (activity_id, zone)
);

-- Device laps (manual/auto laps and workout-step laps).
CREATE TABLE IF NOT EXISTS activity_lap (
    activity_id         BIGINT  NOT NULL,
    sequence            INTEGER NOT NULL,  -- Garmin lapIndex (1-based)
    step_type           VARCHAR NOT NULL,  -- warmup | work | recovery | cooldown | unknown
    step_type_raw       VARCHAR,           -- Garmin intensityType as returned
    workout_step_index  INTEGER,           -- Garmin wktStepIndex (FIT workout step index)
    workout_step_type   VARCHAR,           -- step type from workout definition (only if definition is trustworthy)
    repeat_group        INTEGER,           -- stepOrder of enclosing repeat group (only if definition is trustworthy)
    start_time_utc      TIMESTAMP,
    duration_s          DOUBLE,
    moving_time_s       DOUBLE,
    distance_m          DOUBLE,
    avg_speed_mps       DOUBLE,
    avg_pace_s_per_km   DOUBLE,
    avg_hr              DOUBLE,
    max_hr              DOUBLE,
    avg_cadence         DOUBLE,
    max_cadence         DOUBLE,
    avg_power           DOUBLE,
    max_power           DOUBLE,
    elevation_gain_m    DOUBLE,
    elevation_loss_m    DOUBLE,
    PRIMARY KEY (activity_id, sequence)
);

-- Garmin "typed splits": one row per executed workout step (INTERVAL_*)
-- or detected run/walk segment (RWD_*). Aggregates multiple laps of one step.
CREATE TABLE IF NOT EXISTS activity_split (
    activity_id         BIGINT  NOT NULL,
    sequence            INTEGER NOT NULL,  -- Garmin messageIndex
    split_type          VARCHAR,           -- e.g. INTERVAL_ACTIVE, INTERVAL_RECOVERY, INTERVAL_WARMUP, RWD_RUN
    lap_indexes         INTEGER[],         -- activity_lap.sequence values covered, if any
    start_time_utc      TIMESTAMP,
    duration_s          DOUBLE,
    moving_time_s       DOUBLE,
    distance_m          DOUBLE,
    avg_speed_mps       DOUBLE,
    avg_pace_s_per_km   DOUBLE,
    avg_hr              DOUBLE,
    max_hr              DOUBLE,
    avg_cadence         DOUBLE,
    max_cadence         DOUBLE,
    avg_power           DOUBLE,
    max_power           DOUBLE,
    elevation_gain_m    DOUBLE,
    elevation_loss_m    DOUBLE,
    PRIMARY KEY (activity_id, sequence)
);

CREATE TABLE IF NOT EXISTS daily_health (
    date                    DATE PRIMARY KEY,
    resting_hr              INTEGER,
    hrv_last_night_avg      DOUBLE,
    hrv_weekly_avg          DOUBLE,
    hrv_status              VARCHAR,
    sleep_start_utc         TIMESTAMP,
    sleep_end_utc           TIMESTAMP,
    sleep_duration_s        INTEGER,
    sleep_score             INTEGER,
    deep_sleep_s            INTEGER,
    light_sleep_s           INTEGER,
    rem_sleep_s             INTEGER,
    awake_s                 INTEGER,
    body_battery_high       INTEGER,
    body_battery_low        INTEGER,
    body_battery_wake       INTEGER,
    training_readiness      INTEGER,
    training_readiness_level VARCHAR,
    recovery_time_min       INTEGER,   -- from the morning Training Readiness entry
    tr_sleep_score_pct      INTEGER,   -- Training Readiness factor percentages
    tr_recovery_time_pct    INTEGER,
    tr_acwr_pct             INTEGER,
    tr_hrv_pct              INTEGER,
    tr_stress_history_pct   INTEGER,
    tr_sleep_history_pct    INTEGER,
    acute_load              DOUBLE,
    stress_avg              INTEGER,
    stress_max              INTEGER,
    steps                   INTEGER,
    active_kcal             DOUBLE,
    resting_kcal            DOUBLE,
    total_kcal              DOUBLE,
    weight_kg               DOUBLE,
    vo2max                  DOUBLE,    -- Garmin generic (running) VO2max, precise value
    normalized_at           TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS annotation_id_seq;

-- Manual notes. Independent from Garmin data; sync/rebuild never touch this table.
CREATE TABLE IF NOT EXISTS annotation (
    id                BIGINT PRIMARY KEY DEFAULT nextval('annotation_id_seq'),
    activity_id       BIGINT,
    date              DATE,
    rpe               INTEGER,   -- 1-10
    fatigue           INTEGER,   -- free scale, suggested 1-10
    legs              VARCHAR,
    motivation        INTEGER,   -- free scale, suggested 1-10
    soreness_or_pain  VARCHAR,
    notes             VARCHAR,
    created_at        TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL,
    CHECK (activity_id IS NOT NULL OR date IS NOT NULL)
);

-- One row per sync run: the date range that was requested. Used to find
-- days/items inside requested ranges that are still missing (gap retry).
CREATE TABLE IF NOT EXISTS sync_run (
    run_id      VARCHAR PRIMARY KEY,
    started_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    date_from   DATE NOT NULL,
    date_to     DATE NOT NULL,
    status      VARCHAR NOT NULL   -- running | ok | partial | aborted
);

CREATE TABLE IF NOT EXISTS sync_log (
    run_id      VARCHAR     NOT NULL,
    logged_at   TIMESTAMP NOT NULL,
    endpoint    VARCHAR     NOT NULL,
    source_key  VARCHAR     NOT NULL,
    status      VARCHAR     NOT NULL,  -- ok | unchanged | empty | error | aborted | normalize_error
    error       VARCHAR
);

-- Derived views -----------------------------------------------------------

CREATE OR REPLACE VIEW v_activity_annotated AS
SELECT a.*, n.rpe AS note_rpe, n.fatigue AS note_fatigue, n.legs AS note_legs,
       n.motivation AS note_motivation, n.soreness_or_pain AS note_soreness_or_pain,
       n.notes AS note_notes
FROM activity a
LEFT JOIN (
    SELECT * FROM annotation WHERE activity_id IS NOT NULL
    QUALIFY row_number() OVER (PARTITION BY activity_id ORDER BY updated_at DESC) = 1
) n USING (activity_id);

CREATE OR REPLACE VIEW v_weekly_training AS
SELECT date_trunc('week', date)::DATE AS week_start,
       activity_type,
       count(*)                              AS activities,
       round(sum(distance_m) / 1000, 2)      AS distance_km,
       round(sum(duration_s) / 3600, 2)      AS duration_h,
       round(sum(training_load), 1)          AS training_load,
       round(avg(avg_hr), 1)                 AS avg_hr
FROM activity
GROUP BY ALL;
