"""Synthetic payloads mirroring real python-garminconnect 0.3.16 response shapes.

Values are made up; only key names and nesting match Garmin responses.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta

WORKOUT_ID = 111


def workout(updated: str = "2026-09-01T10:00:00.0") -> dict:
    return {
        "workoutId": WORKOUT_ID,
        "updatedDate": updated,
        "workoutSegments": [
            {
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 1,
                        "stepType": {"stepTypeKey": "warmup"},
                    },
                    {
                        "type": "RepeatGroupDTO",
                        "stepOrder": 2,
                        "numberOfIterations": 2,
                        "workoutSteps": [
                            {
                                "type": "ExecutableStepDTO",
                                "stepOrder": 3,
                                "stepType": {"stepTypeKey": "interval"},
                            },
                            {
                                "type": "ExecutableStepDTO",
                                "stepOrder": 4,
                                "stepType": {"stepTypeKey": "recovery"},
                            },
                        ],
                    },
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 5,
                        "stepType": {"stepTypeKey": "cooldown"},
                    },
                ]
            }
        ],
    }


def activity_list_item(aid: int, day: str, workout_id: int | None = WORKOUT_ID) -> dict:
    return {
        "activityId": aid,
        "activityName": "Run",
        "startTimeLocal": f"{day} 07:00:00",
        "startTimeGMT": f"{day} 05:00:00",
        "activityType": {"typeKey": "running"},
        "distance": 8000.0,
        "duration": 2400.0,
        "averageSpeed": 3.333,
        "averageHR": 150.0,
        "deviceId": 42,
        "workoutId": workout_id,
    }


def activity_detail(aid: int, day: str) -> dict:
    return {
        "activityId": aid,
        "activityName": "Run",
        "activityTypeDTO": {"typeKey": "running"},
        "timeZoneUnitDTO": {"timeZone": "Europe/Warsaw"},
        "metadataDTO": {"deviceMetaDataDTO": {"deviceId": "42"}, "associatedWorkoutId": WORKOUT_ID},
        "summaryDTO": {
            "startTimeLocal": f"{day}T07:00:00.0",
            "startTimeGMT": f"{day}T05:00:00.0",
            "distance": 8000.0,
            "duration": 2400.0,
            "movingDuration": 2390.0,
            "elapsedDuration": 2410.0,
            "averageSpeed": 3.333,
            "maxSpeed": 5.0,
            "averageHR": 150.0,
            "maxHR": 175.0,
            "averageRunCadence": 170.0,
            "maxRunCadence": 190.0,
            "strideLength": 110.0,
            "elevationGain": 40.0,
            "elevationLoss": 38.0,
            "trainingEffect": 3.5,
            "anaerobicTrainingEffect": 2.0,
            "activityTrainingLoad": 120.0,
            "calories": 600.0,
            "directWorkoutRpe": 70,
            "directWorkoutFeel": 50,
        },
    }


def splits(aid: int, day: str) -> dict:
    laps = [
        ("WARMUP", 0, 1000.0),
        ("ACTIVE", 1, 400.0),
        ("RECOVERY", 2, 200.0),
        ("ACTIVE", 1, 400.0),
        ("RECOVERY", 2, 200.0),
        ("COOLDOWN", 4, 1000.0),
        ("ACTIVE", None, 50.0),
    ]
    return {
        "activityId": aid,
        "lapDTOs": [
            {
                "lapIndex": i + 1,
                "intensityType": it,
                "wktStepIndex": step,
                "distance": dist,
                "duration": dist / 3.0,
                "averageSpeed": 3.0,
                "averageHR": 140.0,
                "startTimeGMT": f"{day}T05:{i:02d}:00.0",
            }
            for i, (it, step, dist) in enumerate(laps)
        ],
    }


def typed_splits(aid: int) -> dict:
    return {
        "activityId": aid,
        "splits": [
            {"type": "INTERVAL_WARMUP", "messageIndex": 0, "lapIndexes": [1], "distance": 1000.0},
            {"type": "RWD_RUN", "messageIndex": 1, "lapIndexes": None, "distance": 3000.0},
        ],
    }


def details(aid: int, day: str, n: int = 5) -> dict:
    """Time series with ``n`` samples 10 s apart at 3.33 m/s."""
    keys = [
        "sumDuration",
        "directHeartRate",
        "directTimestamp",
        "sumDistance",
        "directSpeed",
        "directDoubleCadence",
        "directElevation",
        "directStrideLength",
    ]
    start_ms = int(datetime.fromisoformat(f"{day}T05:00:00+00:00").timestamp() * 1000)
    return {
        "activityId": aid,
        "metricDescriptors": [
            {"metricsIndex": i, "key": k, "unit": {"key": "x"}} for i, k in enumerate(keys)
        ],
        "activityDetailMetrics": [
            {
                "metrics": [
                    10.0 * i,
                    130.0 + i,
                    float(start_ms + 10_000 * i),
                    33.3 * i,
                    3.33,
                    170.0,
                    100.0 + i,
                    110.0,
                ]
            }
            for i in range(n)
        ],
        "geoPolylineDTO": None,
        "detailsAvailable": True,
    }


HR_ZONES = [
    {"zoneNumber": 1, "secsInZone": 100.0, "zoneLowBoundary": 110},
    {"zoneNumber": 2, "secsInZone": 900.0, "zoneLowBoundary": 130},
]
WEATHER = {
    "temp": 50,
    "apparentTemp": 48,
    "relativeHumidity": 70,
    "weatherTypeDTO": {"desc": "Cloudy"},
}
GEAR = [{"displayName": "Shoe A"}]
DEVICES = [{"deviceId": 42, "productDisplayName": "Watch X"}]


def user_summary(day: str) -> dict:
    return {
        "calendarDate": day,
        "restingHeartRate": 50,
        "totalSteps": 10000,
        "activeKilocalories": 500.0,
        "bmrKilocalories": 1800.0,
        "totalKilocalories": 2300.0,
        "averageStressLevel": -1,
        "maxStressLevel": 80,
        "bodyBatteryHighestValue": 90,
        "bodyBatteryLowestValue": 20,
        "bodyBatteryAtWakeTime": 88,
    }


def sleep(day: str) -> dict:
    return {
        "dailySleepDTO": {
            "calendarDate": day,
            "sleepTimeSeconds": 28000,
            "deepSleepSeconds": 5000,
            "lightSleepSeconds": 16000,
            "remSleepSeconds": 7000,
            "awakeSleepSeconds": 1000,
            "sleepStartTimestampGMT": 1790000000000,
            "sleepEndTimestampGMT": 1790028000000,
            "sleepScores": {"overall": {"value": 82}},
        },
        "avgOvernightHrv": 70.0,
        "hrvStatus": "BALANCED",
        "restingHeartRate": 49,
    }


def readiness(day: str) -> list:
    return [
        {
            "calendarDate": day,
            "timestamp": f"{day}T12:00:00.0",
            "score": 60,
            "inputContext": "UPDATE",
        },
        {
            "calendarDate": day,
            "timestamp": f"{day}T05:00:00.0",
            "score": 80,
            "level": "HIGH",
            "recoveryTime": 300,
            "inputContext": "AFTER_WAKEUP_RESET",
            "hrvFactorPercent": 90,
        },
    ]


class FakeGarmin:
    """Stands in for ``garminconnect.Garmin`` with deterministic data.

    ``activities`` maps date string -> list of activity ids.
    ``fail`` is a set of method names that raise.
    """

    def __init__(self, activities: dict[str, list[int]], fail: set | None = None) -> None:
        self.activities = activities
        # Entries: method name (always fails) or (method name, first argument).
        self.fail = fail or set()
        self.fail_exc: Exception = RuntimeError("boom")
        self.hrv = 72
        self.calls: list[str] = []
        self.call_args: list[tuple] = []

    def _call(self, name: str, value, arg=None):
        self.calls.append(name)
        self.call_args.append((name, None if arg is None else str(arg)))
        if name in self.fail or (name, None if arg is None else str(arg)) in self.fail:
            raise self.fail_exc
        return deepcopy(value)

    def _day_of(self, aid: int) -> str:
        return next(d for d, ids in self.activities.items() if aid in ids)

    def get_devices(self):
        return self._call("get_devices", DEVICES)

    def get_activities_by_date(self, start: str, end: str):
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        items = [
            activity_list_item(aid, d)
            for d, ids in self.activities.items()
            if s <= date.fromisoformat(d) <= e
            for aid in ids
        ]
        return self._call("get_activities_by_date", items, start)

    def get_activity(self, aid):
        return self._call("get_activity", activity_detail(int(aid), self._day_of(int(aid))), aid)

    def get_activity_splits(self, aid):
        return self._call("get_activity_splits", splits(int(aid), self._day_of(int(aid))), aid)

    def get_activity_typed_splits(self, aid):
        return self._call("get_activity_typed_splits", typed_splits(int(aid)), aid)

    def get_activity_hr_in_timezones(self, aid):
        return self._call("get_activity_hr_in_timezones", HR_ZONES, aid)

    def get_activity_weather(self, aid):
        return self._call("get_activity_weather", WEATHER, aid)

    def get_activity_gear(self, aid):
        return self._call("get_activity_gear", GEAR, aid)

    def get_activity_details(self, aid, maxchart=2000, maxpoly=4000):
        return self._call("get_activity_details", details(int(aid), self._day_of(int(aid))), aid)

    def get_workout_by_id(self, wid):
        return self._call("get_workout_by_id", workout(), wid)

    def get_user_summary(self, day):
        return self._call("get_user_summary", user_summary(day), day)

    def get_sleep_data(self, day):
        return self._call("get_sleep_data", sleep(day), day)

    def get_training_readiness(self, day):
        return self._call("get_training_readiness", readiness(day), day)

    def get_hrv_data_range(self, start, end):
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        days = [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]
        rows = [
            {"calendarDate": d, "lastNightAvg": self.hrv, "weeklyAvg": 71, "status": "BALANCED"}
            for d in days
        ]
        return self._call("get_hrv_data_range", {"hrvSummaries": rows}, start)

    def get_max_metrics_range(self, start, end):
        return self._call(
            "get_max_metrics_range",
            [{"generic": {"calendarDate": start, "vo2MaxPreciseValue": 52.4, "vo2MaxValue": 52.0}}],
            start,
        )

    def get_weigh_ins(self, start, end):
        return self._call(
            "get_weigh_ins",
            {"dailyWeightSummaries": [{"summaryDate": start, "latestWeight": {"weight": 70500.0}}]},
            start,
        )
