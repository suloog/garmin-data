"""Regression tests for data-reliability issues found in review."""

from datetime import date

import pytest
from fakes import FakeGarmin

from garmin_data import build, cli
from garmin_data import sources as src
from garmin_data.normalize import activities as na
from garmin_data.sync import incremental_window, sync

TODAY = date(2026, 9, 30)


def hrv(store, day: str):
    return store.con.execute(
        "SELECT hrv_last_night_avg FROM daily_health WHERE date = ?", [day]
    ).fetchone()[0]


# 1. Older data must not override newer data --------------------------------


def test_resync_of_older_range_keeps_newest_value(store, settings):
    g = FakeGarmin({})
    g.hrv = 50
    sync(g, store, settings, date(2026, 9, 1), date(2026, 9, 28), today=TODAY)
    g.hrv = 70  # Garmin revised the value
    sync(g, store, settings, date(2026, 9, 5), date(2026, 9, 10), today=TODAY)
    assert hrv(store, "2026-09-05") == 70
    # Re-syncing the wider, older range must not bring back the old value
    sync(g, store, settings, date(2026, 9, 1), date(2026, 9, 28), today=TODAY)
    assert hrv(store, "2026-09-05") == 70
    build.rebuild(store)
    assert hrv(store, "2026-09-05") == 70


def test_each_day_is_covered_by_one_range_payload(store, settings):
    g = FakeGarmin({})
    sync(g, store, settings, date(2026, 9, 1), date(2026, 9, 28), today=TODAY)
    sync(g, store, settings, date(2026, 9, 5), date(2026, 9, 10), today=TODAY)
    sync(g, store, settings, date(2026, 8, 20), date(2026, 9, 3), today=TODAY)
    keys = sorted(store.raw_keys(src.HRV_RANGE))
    assert keys == ["2026-08", "2026-09"]


# 2. Failed historical fetches must be retried ------------------------------


def test_incremental_retries_failed_old_day(store, settings):
    g = FakeGarmin({}, fail={("get_sleep_data", "2026-09-02")})
    sync(g, store, settings, date(2026, 9, 1), date(2026, 9, 10), today=date(2026, 9, 10))
    assert not store.has_raw(src.SLEEP, "2026-09-02")
    g.fail.clear()
    start, end = incremental_window(store, settings, today=date(2026, 9, 12))
    assert start > date(2026, 9, 2)  # the window alone would miss it
    sync(g, store, settings, start, end, fill_gaps=True, today=date(2026, 9, 12))
    assert store.has_raw(src.SLEEP, "2026-09-02")
    score = store.con.execute(
        "SELECT sleep_score FROM daily_health WHERE date = '2026-09-02'"
    ).fetchone()
    assert score == (82,)


def test_incremental_retries_failed_activity_detail_and_range(store, settings):
    g = FakeGarmin(
        {"2026-08-10": [1]},
        fail={("get_activity_weather", "1"), ("get_hrv_data_range", "2026-08-01")},
    )
    sync(g, store, settings, date(2026, 8, 1), date(2026, 8, 31), today=TODAY)
    g.fail.clear()
    start, end = incremental_window(store, settings, today=TODAY)
    sync(g, store, settings, start, end, fill_gaps=True, today=TODAY)
    assert store.has_raw(src.ACTIVITY_WEATHER, "1")
    assert store.has_raw(src.HRV_RANGE, "2026-08")
    assert store.con.execute(
        "SELECT temperature_c FROM activity WHERE activity_id = 1"
    ).fetchone() == (10.0,)
    assert hrv(store, "2026-08-15") == 72


def test_time_series_backfilled_for_activities_synced_before_it_existed(store, settings):
    # Simulates activities stored before the activity_details endpoint was added.
    g = FakeGarmin({"2026-08-10": [1]}, fail={"get_activity_details"})
    sync(g, store, settings, date(2026, 8, 1), date(2026, 8, 31), today=TODAY)
    assert store.con.execute("SELECT count(*) FROM activity_sample").fetchone()[0] == 0
    g.fail.clear()
    g.calls.clear()
    start, end = incremental_window(store, settings, today=TODAY)
    sync(g, store, settings, start, end, fill_gaps=True, today=TODAY)
    assert g.calls.count("get_activity_details") == 1
    assert store.con.execute("SELECT count(*) FROM activity_sample").fetchone()[0] == 5


def test_aborted_run_leaves_retryable_gaps(store, settings):
    from garminconnect import GarminConnectAuthenticationError

    g = FakeGarmin({}, fail={("get_user_summary", "2026-09-05")})
    g.fail_exc = GarminConnectAuthenticationError("expired")
    result = sync(g, store, settings, date(2026, 9, 1), date(2026, 9, 10), today=date(2026, 9, 10))
    assert result.aborted
    g.fail.clear()
    sync(
        g,
        store,
        settings,
        date(2026, 9, 10),
        date(2026, 9, 10),
        fill_gaps=True,
        today=date(2026, 9, 10),
    )
    for d in src.days_between(date(2026, 9, 1), date(2026, 9, 10)):
        assert store.has_raw(src.USER_SUMMARY, d.isoformat()), d


def test_permanently_failing_item_is_given_up(store, settings):
    g = FakeGarmin({}, fail={("get_sleep_data", "2026-09-02")})
    day = date(2026, 9, 10)
    for i in range(settings.max_fetch_attempts + 2):
        sync(g, store, settings, day, day, fill_gaps=True, today=day) if i else sync(
            g, store, settings, date(2026, 9, 1), day, today=day
        )
    attempts = [a for a in g.call_args if a == ("get_sleep_data", "2026-09-02")]
    assert len(attempts) == settings.max_fetch_attempts
    from garmin_data.gaps import find_gaps

    assert find_gaps(store, settings.max_fetch_attempts, day).given_up == 1


# 3. A failed rebuild must not destroy normalized data ------------------------


def test_failed_rebuild_keeps_existing_rows(store, settings, monkeypatch):
    sync(
        FakeGarmin({"2026-09-02": [1]}),
        store,
        settings,
        date(2026, 9, 1),
        date(2026, 9, 3),
        today=TODAY,
    )

    def broken(*args, **kwargs):
        raise KeyError("unexpected payload shape")

    monkeypatch.setattr(na, "normalize_laps", broken)
    with pytest.raises(build.RebuildError):
        build.rebuild(store)
    counts = store.con.execute(
        "SELECT (SELECT count(*) FROM activity), (SELECT count(*) FROM activity_lap), "
        "(SELECT count(*) FROM daily_health)"
    ).fetchone()
    assert counts == (1, 7, 30)  # daily rows cover the whole synced month


def test_normalize_error_in_sync_is_isolated_and_reported(store, settings, monkeypatch):
    real = na.normalize_laps

    def flaky(aid, *args, **kwargs):
        if aid == 1:
            raise KeyError("unexpected payload shape")
        return real(aid, *args, **kwargs)

    monkeypatch.setattr(na, "normalize_laps", flaky)
    result = sync(
        FakeGarmin({"2026-09-02": [1, 2]}),
        store,
        settings,
        date(2026, 9, 1),
        date(2026, 9, 3),
        today=TODAY,
    )
    assert result.normalize_errors == ["activity 1: KeyError: 'unexpected payload shape'"]
    assert store.con.execute("SELECT list(activity_id) FROM activity").fetchone() == ([2],)
    assert store.con.execute(
        "SELECT count(*) FROM sync_log WHERE status = 'normalize_error'"
    ).fetchone() == (1,)


# 4. CLI exit code reflects partial failure ----------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[storage]\ndata_dir = "{tmp_path}"\n[sync]\nrequest_delay_s = 0\n')
    monkeypatch.delenv("GARMIN_DATA_DIR", raising=False)
    return cfg


def run_cli(monkeypatch, cfg, g, *args):
    monkeypatch.setattr(cli, "get_client", lambda settings, interactive=False: g)
    return cli.main(["--config", str(cfg), "sync", *args])


def test_cli_exit_codes(cli_env, monkeypatch):
    rng = ["--from", "2026-09-01", "--to", "2026-09-03"]
    assert run_cli(monkeypatch, cli_env, FakeGarmin({}), *rng) == 0
    partial = FakeGarmin({}, fail={("get_sleep_data", "2026-09-02")})
    assert run_cli(monkeypatch, cli_env, partial, *rng, "--refresh") == cli.EXIT_PARTIAL
    from garminconnect import GarminConnectAuthenticationError

    aborted = FakeGarmin({}, fail={"get_devices"})
    aborted.fail_exc = GarminConnectAuthenticationError("expired")
    assert run_cli(monkeypatch, cli_env, aborted, *rng) == cli.EXIT_ABORTED


def daily_row(store, day: str, *cols: str):
    return store.con.execute(
        f"SELECT {', '.join(cols)} FROM daily_health WHERE date = ?", [day]
    ).fetchone()


def test_daily_normalize_error_keeps_previous_values(store, settings, monkeypatch):
    from garmin_data.normalize import daily as nd

    g = FakeGarmin({})
    day = date(2026, 9, 2)
    sync(g, store, settings, day, day, today=TODAY)
    assert daily_row(store, "2026-09-02", "steps", "resting_hr", "sleep_score") == (10000, 50, 82)

    def broken(payload):
        raise KeyError("unexpected payload shape")

    monkeypatch.setattr(nd, "from_user_summary", broken)
    g.hrv = 60  # other sources still update normally
    result = sync(g, store, settings, day, day, refresh=True, today=TODAY)
    assert result.normalize_errors == [
        "user_summary 2026-09-02: KeyError: 'unexpected payload shape'"
    ]
    # Fields owned by the failed payload keep their previous values ...
    assert daily_row(store, "2026-09-02", "steps", "resting_hr", "total_kcal") == (
        10000,
        50,
        2300.0,
    )
    # ... while fields from healthy payloads are refreshed.
    assert daily_row(store, "2026-09-02", "hrv_last_night_avg", "sleep_score") == (60, 82)


def test_daily_normalize_error_on_range_payload_keeps_month(store, settings, monkeypatch):
    from garmin_data.normalize import daily as nd

    g = FakeGarmin({})
    sync(g, store, settings, date(2026, 9, 1), date(2026, 9, 3), today=TODAY)
    before = store.con.execute(
        "SELECT list(hrv_last_night_avg ORDER BY date) FROM daily_health"
    ).fetchone()

    def broken(payload):
        raise ValueError("bad")

    monkeypatch.setattr(nd, "from_hrv_range", broken)
    sync(g, store, settings, date(2026, 9, 1), date(2026, 9, 3), refresh=True, today=TODAY)
    after = store.con.execute(
        "SELECT list(hrv_last_night_avg ORDER BY date) FROM daily_health"
    ).fetchone()
    assert after == before


def test_declared_daily_columns_match_extractors():
    from fakes import readiness, sleep, user_summary

    from garmin_data.normalize import daily as nd

    d = "2026-09-02"
    samples = {
        "user_summary": [nd.from_user_summary(user_summary(d))],
        "sleep_data": list(nd.from_sleep(sleep(d))),
        "training_readiness": [nd.from_training_readiness(readiness(d))],
        "hrv_range": [
            nd.from_hrv_range({"hrvSummaries": [{"calendarDate": d, "lastNightAvg": 1}]})
        ],
        "max_metrics_range": [
            nd.from_max_metrics([{"generic": {"calendarDate": d, "vo2MaxValue": 1}}])
        ],
        "weigh_ins_range": [
            nd.from_weigh_ins(
                {"dailyWeightSummaries": [{"summaryDate": d, "latestWeight": {"weight": 1}}]}
            )
        ],
    }
    for endpoint, outputs in samples.items():
        produced = {col for out in outputs for fields in out.values() for col in fields}
        assert produced == set(nd.SOURCE_COLUMNS[endpoint]), endpoint


def test_failed_fallback_source_does_not_freeze_higher_precedence_columns(store, monkeypatch):
    from fakes import sleep, user_summary

    from garmin_data.normalize import daily as nd

    d = date(2026, 9, 2)
    summary = user_summary("2026-09-02")
    store.put_raw(src.USER_SUMMARY, "2026-09-02", summary, d, d)
    store.put_raw(src.SLEEP, "2026-09-02", sleep("2026-09-02"), d, d)
    build.build_daily(store, d, d)
    assert daily_row(store, "2026-09-02", "resting_hr", "sleep_score") == (50, 82)

    summary["restingHeartRate"] = 55  # Garmin revised the daily summary
    store.put_raw(src.USER_SUMMARY, "2026-09-02", summary, d, d)

    def broken(payload):
        raise KeyError("sleep shape changed")

    monkeypatch.setattr(nd, "from_sleep", broken)
    report = build.build_daily(store, d, d)
    assert len(report.errors) == 1
    # resting_hr comes from the healthy user summary (it outranks sleep's fallback);
    # sleep columns keep their previous values.
    assert daily_row(store, "2026-09-02", "resting_hr", "sleep_score") == (55, 82)


SHAPE_CHANGED = {"unexpected": "payload shape"}


def test_reshaped_daily_payload_keeps_values_and_is_reported(store):
    d = date(2026, 9, 2)
    from fakes import user_summary

    store.put_raw(src.USER_SUMMARY, "2026-09-02", user_summary("2026-09-02"), d, d)
    build.build_daily(store, d, d)
    assert daily_row(store, "2026-09-02", "steps") == (10000,)

    store.put_raw(src.USER_SUMMARY, "2026-09-02", SHAPE_CHANGED, d, d)
    report = build.build_daily(store, d, d)
    assert daily_row(store, "2026-09-02", "steps") == (10000,)
    assert len(report.errors) == 1 and "UnexpectedPayload" in report.errors[0]


def test_reshaped_activity_payloads_keep_laps_and_are_reported(store, settings):
    sync(
        FakeGarmin({"2026-09-02": [1]}),
        store,
        settings,
        date(2026, 9, 1),
        date(2026, 9, 3),
        today=TODAY,
    )
    before = store.con.execute(
        "SELECT (SELECT count(*) FROM activity_lap), (SELECT count(*) FROM activity_split), "
        "(SELECT count(*) FROM activity_hr_zone), (SELECT temperature_c FROM activity)"
    ).fetchone()
    for endpoint in (
        src.ACTIVITY_SPLITS,
        src.ACTIVITY_TYPED_SPLITS,
        src.ACTIVITY_HR_ZONES,
        src.ACTIVITY_WEATHER,
    ):
        store.put_raw(endpoint, "1", SHAPE_CHANGED)
        report = build.build_activities(store, [1])
        assert len(report.errors) == 1, endpoint
        after = store.con.execute(
            "SELECT (SELECT count(*) FROM activity_lap), (SELECT count(*) FROM activity_split), "
            "(SELECT count(*) FROM activity_hr_zone), (SELECT temperature_c FROM activity)"
        ).fetchone()
        assert after == before, endpoint


def test_reshaped_payload_fails_rebuild_atomically(store, settings):
    sync(FakeGarmin({}), store, settings, date(2026, 9, 2), date(2026, 9, 2), today=TODAY)
    d = date(2026, 9, 2)
    store.put_raw(src.USER_SUMMARY, "2026-09-02", SHAPE_CHANGED, d, d)
    with pytest.raises(build.RebuildError, match="UnexpectedPayload"):
        build.rebuild(store)
    assert daily_row(store, "2026-09-02", "steps") == (10000,)
