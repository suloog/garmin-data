from datetime import date

import pytest
from fakes import FakeGarmin
from garminconnect import GarminConnectAuthenticationError

from garmin_data import annotations as ann
from garmin_data import build
from garmin_data.sync import incremental_window, sync

TODAY = date(2026, 9, 30)
START, END = date(2026, 9, 1), date(2026, 9, 10)


def counts(store):
    tables = (
        "raw_payload",
        "activity",
        "activity_lap",
        "activity_split",
        "activity_hr_zone",
        "daily_health",
    )
    return {t: store.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def test_sync_populates_all_layers(store, settings):
    g = FakeGarmin({"2026-09-02": [1], "2026-09-05": [2, 3]})
    result = sync(g, store, settings, START, END, today=TODAY)
    assert result.aborted is None and result.stats.errors == 0
    c = counts(store)
    assert c["activity"] == 3
    assert c["activity_lap"] == 21
    assert c["daily_health"] == 30  # whole month: range payloads are monthly
    row = store.con.execute(
        "SELECT weight_kg, vo2max, training_readiness FROM daily_health WHERE date='2026-09-01'"
    ).fetchone()
    assert row == (70.5, 52.4, 80)


def test_repeated_sync_creates_no_duplicates(store, settings):
    g = FakeGarmin({"2026-09-02": [1]})
    sync(g, store, settings, START, END, today=TODAY)
    before = counts(store)
    g.calls.clear()
    sync(g, store, settings, START, END, today=TODAY)
    assert counts(store) == before
    # Old, already-stored details are not re-fetched
    assert "get_activity" not in g.calls and "get_sleep_data" not in g.calls
    sync(g, store, settings, START, END, refresh=True, today=TODAY)
    assert counts(store) == before
    assert "get_activity" in g.calls


def test_annotations_survive_sync_and_rebuild(store, settings):
    g = FakeGarmin({"2026-09-02": [1]})
    sync(g, store, settings, START, END, today=TODAY)
    nid = ann.add_annotation(store, activity_id=1, rpe=8, legs="heavy", notes="hard")
    ann.add_annotation(store, day=date(2026, 9, 3), fatigue=6)
    sync(g, store, settings, START, END, refresh=True, today=TODAY)
    build.rebuild(store)
    notes = ann.list_annotations(store)
    assert len(notes) == 2
    first = next(n for n in notes if n["id"] == nid)
    assert (first["rpe"], first["legs"], first["notes"], str(first["date"])) == (
        8,
        "heavy",
        "hard",
        "2026-09-02",
    )
    joined = store.con.execute(
        "SELECT note_rpe FROM v_activity_annotated WHERE activity_id=1"
    ).fetchone()
    assert joined == (8,)


def test_rebuild_is_identical(store, settings):
    sync(FakeGarmin({"2026-09-02": [1]}), store, settings, START, END, today=TODAY)
    q = "SELECT * EXCLUDE (normalized_at) FROM {} ORDER BY ALL"
    before = {t: store.con.execute(q.format(t)).fetchall() for t in ("activity", "daily_health")}
    build.rebuild(store)
    after = {t: store.con.execute(q.format(t)).fetchall() for t in ("activity", "daily_health")}
    assert before == after


def test_failing_endpoint_does_not_stop_sync(store, settings):
    g = FakeGarmin({"2026-09-02": [1]}, fail={"get_activity_weather", "get_sleep_data"})
    result = sync(g, store, settings, START, END, today=TODAY)
    assert result.aborted is None
    assert result.stats.errors == 11  # 1 weather + 10 sleep days
    assert counts(store)["activity"] == 1
    assert (
        store.con.execute("SELECT count(*) FROM sync_log WHERE status='error'").fetchone()[0] == 11
    )
    # Failed endpoints are retried by the next sync
    g.fail.clear()
    sync(g, store, settings, START, END, today=TODAY)
    assert store.con.execute("SELECT count(sleep_score) FROM daily_health").fetchone()[0] == 10


def test_auth_error_aborts(store, settings):
    g = FakeGarmin({"2026-09-02": [1]}, fail={"get_activities_by_date"})
    g.fail_exc = GarminConnectAuthenticationError("expired")
    result = sync(g, store, settings, START, END, today=TODAY)
    assert result.aborted and "Authentication" in result.aborted


def test_incremental_window(store, settings):
    assert incremental_window(store, settings, today=TODAY) == (date(2026, 9, 1), TODAY)
    sync(FakeGarmin({}), store, settings, START, END, today=TODAY)
    assert incremental_window(store, settings, today=TODAY) == (date(2026, 9, 7), TODAY)


def test_invalid_range(store, settings):
    with pytest.raises(ValueError):
        sync(FakeGarmin({}), store, settings, END, START, today=TODAY)


def test_rebuild_ignores_activities_outside_requested_ranges(store, settings):
    # The monthly list also returns the 2026-09-20 activity, which was not requested.
    g = FakeGarmin({"2026-09-02": [1], "2026-09-20": [2]})
    sync(g, store, settings, START, END, today=TODAY)
    assert store.con.execute("SELECT list(activity_id) FROM activity").fetchone() == ([1],)
    build.rebuild(store)
    assert store.con.execute("SELECT list(activity_id) FROM activity").fetchone() == ([1],)
