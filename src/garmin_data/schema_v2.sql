-- Schema v2: activity time series.

-- One row per sample of Garmin's activity details chart data (``get_activity_details``).
-- Garmin downsamples long activities (at most ~2000 samples), so the interval
-- between samples is not constant. Values are as recorded by the device.
CREATE TABLE IF NOT EXISTS activity_sample (
    activity_id              BIGINT  NOT NULL,
    sample_index             INTEGER NOT NULL,  -- 0-based position in Garmin's list
    timestamp_utc            TIMESTAMP,
    timer_s                  DOUBLE,   -- sumDuration: timer time (excludes pauses)
    elapsed_s                DOUBLE,   -- sumElapsedDuration: wall-clock time since start
    moving_s                 DOUBLE,   -- sumMovingDuration
    distance_m               DOUBLE,   -- sumDistance
    heart_rate               DOUBLE,   -- bpm
    speed_mps                DOUBLE,
    grade_adjusted_speed_mps DOUBLE,
    cadence                  DOUBLE,   -- steps/min (both feet, like activity.avg_cadence)
    stride_length_m          DOUBLE,
    vertical_oscillation_cm  DOUBLE,
    vertical_ratio           DOUBLE,   -- percent
    ground_contact_ms        DOUBLE,
    power                    DOUBLE,   -- watts
    elevation_m              DOUBLE,
    respiration_rate         DOUBLE,   -- breaths/min
    latitude                 DOUBLE,
    longitude                DOUBLE,
    PRIMARY KEY (activity_id, sample_index)
);
