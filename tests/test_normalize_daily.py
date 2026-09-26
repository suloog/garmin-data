from datetime import date

import pytest
from fakes import readiness, sleep, user_summary

from garmin_data.normalize import daily as nd
from garmin_data.normalize._util import UnexpectedPayload

D = "2026-09-10"


def test_user_summary_negative_stress_is_null():
    row = nd.from_user_summary(user_summary(D))[date(2026, 9, 10)]
    assert row["stress_avg"] is None
    assert row["stress_max"] == 80
    assert row["steps"] == 10000


def test_morning_readiness_preferred():
    row = nd.from_training_readiness(readiness(D))[date(2026, 9, 10)]
    assert row["training_readiness"] == 80
    assert row["recovery_time_min"] == 300
    assert row["tr_hrv_pct"] == 90


def test_merge_fallback_and_precedence():
    d = date(2026, 9, 10)
    prim_sleep, fb_sleep = nd.from_sleep(sleep(D))
    hrv = {d: {"hrv_last_night_avg": 72.0, "hrv_status": "BALANCED"}}
    rows = nd.merge_daily(
        [d, date(2026, 9, 11)], [hrv, nd.from_user_summary(user_summary(D)), prim_sleep], [fb_sleep]
    )
    row = rows[0]
    assert row["hrv_last_night_avg"] == 72.0  # range endpoint wins over sleep fallback
    assert row["resting_hr"] == 50  # user summary wins over sleep fallback
    assert row["sleep_score"] == 82
    # Days without data still get a row
    assert rows[1] == {"date": date(2026, 9, 11)}


def test_extractors_accept_no_data_responses():
    assert nd.from_user_summary(None) == {}
    assert nd.from_sleep({}) == ({}, {})
    assert nd.from_sleep({"dailySleepDTO": None}) == ({}, {})
    assert nd.from_training_readiness([]) == {}
    assert nd.from_hrv_range({"hrvSummaries": None}) == {}
    assert nd.from_hrv_range({"hrvSummaries": []}) == {}
    assert nd.from_max_metrics([{"generic": None, "cycling": {}}]) == {}
    assert (
        nd.from_weigh_ins({"dailyWeightSummaries": [{"summaryDate": D, "latestWeight": None}]})
        == {}
    )
    # Known keys with null values are "no data", not a shape change
    assert (
        nd.from_user_summary({"calendarDate": D, "totalSteps": None})[date(2026, 9, 10)]["steps"]
        is None
    )


@pytest.mark.parametrize(
    "fn, payload",
    [
        (nd.from_user_summary, {"unexpected": "payload shape"}),
        (nd.from_user_summary, {"calendarDate": D, "renamedSteps": 1}),
        (nd.from_sleep, {"sleepSummary": {}}),
        (nd.from_sleep, {"dailySleepDTO": {"calendarDate": D, "totalSleep": 1}}),
        (nd.from_training_readiness, {"score": 1}),
        (nd.from_training_readiness, [{"calendarDate": D, "readiness": 1}]),
        (nd.from_hrv_range, {"summaries": []}),
        (nd.from_hrv_range, {"hrvSummaries": [{"calendarDate": D}]}),
        (nd.from_max_metrics, {"unexpected": True}),
        (nd.from_weigh_ins, {"weights": []}),
        (nd.from_weigh_ins, {"dailyWeightSummaries": [{"summaryDate": D}]}),
    ],
)
def test_extractors_reject_unknown_shapes(fn, payload):
    with pytest.raises(UnexpectedPayload):
        fn(payload)
