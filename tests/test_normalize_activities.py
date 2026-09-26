from datetime import datetime

from fakes import activity_detail, activity_list_item, splits, workout

from garmin_data.normalize import activities as na


def test_activity_from_detail():
    row = na.normalize_activity(
        activity_detail(1, "2026-09-10"),
        weather={"temp": 50, "weatherTypeDTO": {"desc": "Cloudy"}},
        gear=[{"displayName": "Shoe A"}, {"displayName": "Shoe B"}],
        devices=[{"deviceId": 42, "productDisplayName": "Watch X"}],
    )
    assert row["activity_id"] == 1
    assert str(row["date"]) == "2026-09-10"
    assert row["timezone"] == "Europe/Warsaw"
    assert row["garmin_rpe"] == 7.0
    assert row["avg_stride_length_m"] == 1.1
    assert round(row["avg_pace_s_per_km"]) == 300
    assert row["temperature_c"] == 10.0
    assert row["device"] == "Watch X"
    assert row["gear"] == "Shoe A, Shoe B"
    assert row["workout_id"] == 111


def test_activity_tolerates_missing_fields():
    row = na.normalize_activity({"activityId": 5, "summaryDTO": {}})
    assert row["activity_id"] == 5
    assert row["distance_m"] is None and row["avg_pace_s_per_km"] is None and row["date"] is None


def test_activity_falls_back_to_list_item():
    row = na.normalize_activity(None, activity_list_item(9, "2026-09-11"))
    assert row["activity_id"] == 9
    assert row["distance_m"] == 8000.0
    assert row["start_time_local"] == datetime(2026, 9, 11, 7, 0)


def test_reshaped_activity_payloads_raise():
    import pytest

    from garmin_data.normalize._util import UnexpectedPayload

    bad = {"unexpected": "payload shape"}
    with pytest.raises(UnexpectedPayload):
        na.normalize_activity(bad)
    with pytest.raises(UnexpectedPayload):
        na.normalize_laps(1, bad)
    with pytest.raises(UnexpectedPayload):
        na.normalize_laps(1, {"lapDTOs": [bad]})
    with pytest.raises(UnexpectedPayload):
        na.normalize_typed_splits(1, bad)
    with pytest.raises(UnexpectedPayload):
        na.normalize_hr_zones(1, bad)
    with pytest.raises(UnexpectedPayload):
        na.normalize_activity(activity_detail(1, "2026-09-10"), weather=bad)


def test_no_id_returns_none():
    assert na.normalize_activity(None, None) is None


def test_workout_step_map_uses_fit_ordering():
    m = na.workout_step_map(workout())
    # warmup, interval, recovery, [repeat=3], cooldown
    assert m[0] == {"step_type": "warmup", "repeat_group": None}
    assert m[1] == {"step_type": "interval", "repeat_group": 2}
    assert m[2] == {"step_type": "recovery", "repeat_group": 2}
    assert 3 not in m
    assert m[4] == {"step_type": "cooldown", "repeat_group": None}


def test_classify_lap():
    assert na.classify_lap("WARMUP", 0) == "warmup"
    assert na.classify_lap("ACTIVE", 1) == "work"
    assert na.classify_lap("ACTIVE", None) == "unknown"  # lap outside a structured workout
    assert na.classify_lap("INTERVAL", None) == "unknown"  # plain auto-lap
    assert na.classify_lap("RECOVERY", 2) == "recovery"
    assert na.classify_lap("REST", 2) == "recovery"
    assert na.classify_lap(None, None) == "unknown"


def test_laps_use_workout_only_when_trustworthy():
    start = datetime(2026, 9, 10, 5, 0)
    rows = na.normalize_laps(1, splits(1, "2026-09-10"), workout("2026-09-01T00:00:00.0"), start)
    assert [r["step_type"] for r in rows] == [
        "warmup",
        "work",
        "recovery",
        "work",
        "recovery",
        "cooldown",
        "unknown",
    ]
    assert rows[1]["repeat_group"] == 2 and rows[1]["workout_step_type"] == "interval"

    edited_later = workout("2026-09-20T00:00:00.0")
    rows = na.normalize_laps(1, splits(1, "2026-09-10"), edited_later, start)
    assert all(r["repeat_group"] is None and r["workout_step_type"] is None for r in rows)
    assert rows[1]["step_type"] == "work"  # device intensity is still used
    assert rows[1]["workout_step_index"] == 1


def test_laps_empty_payload():
    assert na.normalize_laps(1, None) == []
    assert na.normalize_laps(1, {"lapDTOs": None}) == []
