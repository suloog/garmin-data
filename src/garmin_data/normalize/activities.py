"""Normalize activity payloads: summary, HR zones, laps and typed splits.

Field names verified against real python-garminconnect 0.3.16 responses:
- ``get_activity``: top level + ``summaryDTO``, ``activityTypeDTO``, ``timeZoneUnitDTO``,
  ``metadataDTO``
- ``get_activities_by_date`` list items (flat, differently named fields; used as fallback)
- ``get_activity_splits``: ``lapDTOs`` with ``intensityType`` and ``wktStepIndex``
- ``get_activity_typed_splits``: ``splits`` with ``type`` and ``lapIndexes``
- ``get_activity_hr_in_timezones``: list of ``zoneNumber`` / ``secsInZone`` / ``zoneLowBoundary``
- ``get_activity_weather``: ``temp`` / ``apparentTemp`` in Fahrenheit
- ``get_activity_details``: ``metricDescriptors`` (``key`` / ``metricsIndex``) and
  ``activityDetailMetrics[].metrics`` (positional values)
- ``get_workout_by_id``: ``workoutSegments[].workoutSteps[]`` (ExecutableStepDTO / RepeatGroupDTO)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ._util import (
    expect,
    expect_keys,
    f_to_c,
    get,
    integer,
    is_empty,
    num,
    pace_s_per_km,
    scaled,
    timestamp,
    to_date,
)

# Garmin lap intensityType -> step_type. ACTIVE is only "work" inside a
# structured workout (lap has a workout step index); free runs report
# INTERVAL/ACTIVE for plain auto-laps, which we leave as "unknown".
_INTENSITY = {
    "WARMUP": "warmup",
    "COOLDOWN": "cooldown",
    "RECOVERY": "recovery",
    "REST": "recovery",
}


_GEAR_KEYS = ("displayName", "customMakeModel", "gearModelName")


def _first(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)


def normalize_activity(
    detail: dict | None,
    list_item: dict | None = None,
    weather: dict | None = None,
    gear: list | None = None,
    devices: list | None = None,
) -> dict[str, Any] | None:
    """Build one ``activity`` row. Either ``detail`` or ``list_item`` must be present.

    Raises ``UnexpectedPayload`` if a non-empty payload has an unknown structure.
    """
    if not is_empty(detail):
        expect_keys(detail, "activity", ("activityId", "summaryDTO"))
    if not is_empty(weather):
        expect_keys(
            weather,
            "activity_weather",
            (),
            ("temp", "apparentTemp", "relativeHumidity", "weatherTypeDTO"),
        )
    if not is_empty(gear):
        expect(gear, list, "activity_gear")
        for g in gear:
            expect_keys(
                g, "activity_gear[]", (), ("displayName", "customMakeModel", "gearModelName")
            )
    if not is_empty(devices):
        expect(devices, list, "devices")
    d = detail or {}
    s = get(d, "summaryDTO", default={})
    li = list_item or {}
    activity_id = integer(_first(d.get("activityId"), li.get("activityId")))
    if activity_id is None:
        return None

    start_local = timestamp(_first(s.get("startTimeLocal"), li.get("startTimeLocal")))
    avg_speed = num(_first(s.get("averageSpeed"), li.get("averageSpeed")))
    max_speed = num(_first(s.get("maxSpeed"), li.get("maxSpeed")))
    device_id = integer(
        _first(get(d, "metadataDTO", "deviceMetaDataDTO", "deviceId"), li.get("deviceId"))
    )
    rpe_raw = num(_first(s.get("directWorkoutRpe"), li.get("directWorkoutRpe")))

    return {
        "activity_id": activity_id,
        "date": to_date(start_local),
        "start_time_local": start_local,
        "start_time_utc": timestamp(_first(s.get("startTimeGMT"), li.get("startTimeGMT"))),
        "timezone": get(d, "timeZoneUnitDTO", "timeZone"),
        "activity_type": _first(
            get(d, "activityTypeDTO", "typeKey"), get(li, "activityType", "typeKey")
        ),
        "activity_name": _first(d.get("activityName"), li.get("activityName")),
        "distance_m": num(_first(s.get("distance"), li.get("distance"))),
        "duration_s": num(_first(s.get("duration"), li.get("duration"))),
        "moving_time_s": num(_first(s.get("movingDuration"), li.get("movingDuration"))),
        "elapsed_time_s": num(_first(s.get("elapsedDuration"), li.get("elapsedDuration"))),
        "avg_speed_mps": avg_speed,
        "max_speed_mps": max_speed,
        "avg_pace_s_per_km": pace_s_per_km(avg_speed),
        "best_pace_s_per_km": pace_s_per_km(max_speed),
        "avg_hr": num(_first(s.get("averageHR"), li.get("averageHR"))),
        "max_hr": num(_first(s.get("maxHR"), li.get("maxHR"))),
        "avg_cadence": num(
            _first(s.get("averageRunCadence"), li.get("averageRunningCadenceInStepsPerMinute"))
        ),
        "max_cadence": num(
            _first(s.get("maxRunCadence"), li.get("maxRunningCadenceInStepsPerMinute"))
        ),
        # Garmin reports stride length in centimetres.
        "avg_stride_length_m": scaled(
            _first(s.get("strideLength"), li.get("avgStrideLength")), 0.01
        ),
        "elevation_gain_m": num(_first(s.get("elevationGain"), li.get("elevationGain"))),
        "elevation_loss_m": num(_first(s.get("elevationLoss"), li.get("elevationLoss"))),
        "avg_power": num(_first(s.get("averagePower"), li.get("avgPower"))),
        "max_power": num(_first(s.get("maxPower"), li.get("maxPower"))),
        "normalized_power": num(_first(s.get("normalizedPower"), li.get("normPower"))),
        "aerobic_te": num(_first(s.get("trainingEffect"), li.get("aerobicTrainingEffect"))),
        "anaerobic_te": num(
            _first(s.get("anaerobicTrainingEffect"), li.get("anaerobicTrainingEffect"))
        ),
        "training_load": num(_first(s.get("activityTrainingLoad"), li.get("activityTrainingLoad"))),
        "calories": num(_first(s.get("calories"), li.get("calories"))),
        "garmin_rpe": None if rpe_raw is None else rpe_raw / 10.0,
        "garmin_feel": integer(_first(s.get("directWorkoutFeel"), li.get("directWorkoutFeel"))),
        "workout_compliance": num(s.get("directWorkoutComplianceScore")),
        "workout_id": integer(
            _first(get(d, "metadataDTO", "associatedWorkoutId"), li.get("workoutId"))
        ),
        "temperature_c": f_to_c(get(weather, "temp")),
        "apparent_temperature_c": f_to_c(get(weather, "apparentTemp")),
        "humidity_pct": num(get(weather, "relativeHumidity")),
        "weather_desc": get(weather, "weatherTypeDTO", "desc"),
        "device_id": device_id,
        "device": _device_name(device_id, devices),
        "gear": _gear_names(gear),
    }


def _device_name(device_id: int | None, devices: list | None) -> str | None:
    if device_id is None or not isinstance(devices, list):
        return None
    for dev in devices:
        if integer(get(dev, "deviceId")) == device_id:
            return _first(get(dev, "productDisplayName"), get(dev, "displayName"))
    return None


def _gear_names(gear: list | None) -> str | None:
    if not isinstance(gear, list):
        return None
    names = [
        _first(get(g, "displayName"), get(g, "customMakeModel"), get(g, "gearModelName"))
        for g in gear
    ]
    names = [n for n in names if n]
    return ", ".join(names) or None


def normalize_hr_zones(activity_id: int, payload: list | None) -> list[dict[str, Any]]:
    if is_empty(payload):
        return []
    expect(payload, list, "activity_hr_zones")
    rows = []
    for z in payload:
        expect_keys(z, "activity_hr_zones[]", ("zoneNumber",), ("secsInZone",))
        zone = integer(get(z, "zoneNumber"))
        if zone is None:
            continue
        rows.append(
            {
                "activity_id": activity_id,
                "zone": zone,
                "seconds": num(get(z, "secsInZone")),
                "zone_low_hr": integer(get(z, "zoneLowBoundary")),
            }
        )
    return rows


# -- workout definition ---------------------------------------------------------


def workout_step_map(workout: dict | None) -> dict[int, dict[str, Any]]:
    """Map FIT workout step index -> {step_type, repeat_group}.

    FIT numbers steps depth-first with a repeat step placed *after* its child
    steps, e.g. ``[warmup, interval, recovery, REPEAT, cooldown]`` -> 0..4.
    Verified against lap ``wktStepIndex`` values for several real workouts.
    """
    if not is_empty(workout):
        expect_keys(workout, "workout", ("workoutSegments",))
    result: dict[int, dict[str, Any]] = {}
    counter = 0

    def walk(steps: list, group: int | None) -> None:
        nonlocal counter
        for step in steps or []:
            if get(step, "type") == "RepeatGroupDTO":
                walk(get(step, "workoutSteps", default=[]), integer(get(step, "stepOrder")))
                counter += 1  # the repeat step itself occupies an index
            else:
                result[counter] = {
                    "step_type": get(step, "stepType", "stepTypeKey"),
                    "repeat_group": group,
                }
                counter += 1

    for segment in get(workout, "workoutSegments", default=[]):
        walk(get(segment, "workoutSteps", default=[]), None)
    return result


def workout_is_trustworthy(workout: dict | None, activity_start_utc: datetime | None) -> bool:
    """A workout definition is mutable in Garmin Connect. Only trust it for
    step mapping if it was last updated before the activity started.
    (``updatedDate`` is assumed to be UTC.)"""
    if not is_empty(workout):
        expect_keys(workout, "workout", ("updatedDate", "workoutSegments"))
    updated = timestamp(get(workout, "updatedDate"))
    return bool(updated and activity_start_utc and updated <= activity_start_utc)


# -- laps & splits ---------------------------------------------------------------


def _segment_stats(x: dict) -> dict[str, Any]:
    speed = num(get(x, "averageSpeed"))
    return {
        "start_time_utc": timestamp(get(x, "startTimeGMT")),
        "duration_s": num(get(x, "duration")),
        "moving_time_s": num(get(x, "movingDuration")),
        "distance_m": num(get(x, "distance")),
        "avg_speed_mps": speed,
        "avg_pace_s_per_km": pace_s_per_km(speed),
        "avg_hr": num(get(x, "averageHR")),
        "max_hr": num(get(x, "maxHR")),
        "avg_cadence": num(get(x, "averageRunCadence")),
        "max_cadence": num(get(x, "maxRunCadence")),
        "avg_power": num(get(x, "averagePower")),
        "max_power": num(get(x, "maxPower")),
        "elevation_gain_m": num(get(x, "elevationGain")),
        "elevation_loss_m": num(get(x, "elevationLoss")),
    }


def classify_lap(intensity: str | None, step_index: int | None) -> str:
    if intensity in _INTENSITY:
        return _INTENSITY[intensity]
    if intensity == "ACTIVE" and step_index is not None:
        return "work"
    return "unknown"


def normalize_laps(
    activity_id: int,
    splits: dict | None,
    workout: dict | None = None,
    activity_start_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    if is_empty(splits):
        return []
    expect_keys(splits, "activity_splits", ("lapDTOs",))
    laps = splits["lapDTOs"] or []
    expect(laps, list, "activity_splits.lapDTOs")
    for lap in laps:
        expect_keys(lap, "activity_splits.lapDTOs[]", (), ("distance", "duration", "intensityType"))
    steps = workout_step_map(workout) if workout_is_trustworthy(workout, activity_start_utc) else {}
    rows = []
    for pos, lap in enumerate(laps, start=1):
        step_index = integer(get(lap, "wktStepIndex"))
        intensity = get(lap, "intensityType")
        step = steps.get(step_index, {}) if step_index is not None else {}
        rows.append(
            {
                "activity_id": activity_id,
                "sequence": integer(get(lap, "lapIndex")) or pos,
                "step_type": classify_lap(intensity, step_index),
                "step_type_raw": intensity,
                "workout_step_index": step_index,
                "workout_step_type": step.get("step_type"),
                "repeat_group": step.get("repeat_group"),
                **_segment_stats(lap),
            }
        )
    return rows


def normalize_typed_splits(activity_id: int, payload: dict | None) -> list[dict[str, Any]]:
    if is_empty(payload):
        return []
    expect_keys(payload, "activity_typed_splits", ("splits",))
    splits = payload["splits"] or []
    expect(splits, list, "activity_typed_splits.splits")
    rows = []
    for pos, split in enumerate(splits):
        expect_keys(split, "activity_typed_splits.splits[]", ("type",))
        lap_indexes = get(split, "lapIndexes")
        rows.append(
            {
                "activity_id": activity_id,
                "sequence": _first(integer(get(split, "messageIndex")), pos),
                "split_type": get(split, "type"),
                "lap_indexes": [int(i) for i in lap_indexes]
                if isinstance(lap_indexes, list)
                else None,
                **_segment_stats(split),
            }
        )
    return rows


# -- time series -------------------------------------------------------------------

# activity_sample column -> (Garmin metric key, scale factor)
_SAMPLE_METRICS = {
    "timer_s": ("sumDuration", 1.0),
    "elapsed_s": ("sumElapsedDuration", 1.0),
    "moving_s": ("sumMovingDuration", 1.0),
    "distance_m": ("sumDistance", 1.0),
    "heart_rate": ("directHeartRate", 1.0),
    "speed_mps": ("directSpeed", 1.0),
    "grade_adjusted_speed_mps": ("directGradeAdjustedSpeed", 1.0),
    "cadence": ("directDoubleCadence", 1.0),
    "stride_length_m": ("directStrideLength", 0.01),  # centimetres
    "vertical_oscillation_cm": ("directVerticalOscillation", 1.0),
    "vertical_ratio": ("directVerticalRatio", 1.0),
    "ground_contact_ms": ("directGroundContactTime", 1.0),
    "power": ("directPower", 1.0),
    "elevation_m": ("directElevation", 1.0),
    "respiration_rate": ("directRespirationRate", 1.0),
    "latitude": ("directLatitude", 1.0),
    "longitude": ("directLongitude", 1.0),
}


def normalize_samples(activity_id: int, payload: dict | None) -> list[dict[str, Any]]:
    """One ``activity_sample`` row per entry of ``activityDetailMetrics``.

    Metrics are positional; ``metricDescriptors`` maps each metric key to its
    index, and the set of metrics differs between activities and devices.
    """
    if is_empty(payload):
        return []
    expect_keys(payload, "activity_details", ("metricDescriptors", "activityDetailMetrics"))
    descriptors = payload["metricDescriptors"] or []
    samples = payload["activityDetailMetrics"] or []
    expect(descriptors, list, "activity_details.metricDescriptors")
    expect(samples, list, "activity_details.activityDetailMetrics")
    index: dict[str, int] = {}
    for d in descriptors:
        expect_keys(d, "activity_details.metricDescriptors[]", ("key", "metricsIndex"))
        i = integer(d["metricsIndex"])
        if i is not None:
            index[d["key"]] = i
    ts_index = index.get("directTimestamp")

    rows = []
    for pos, sample in enumerate(samples):
        expect_keys(sample, "activity_details.activityDetailMetrics[]", ("metrics",))
        values = sample["metrics"]
        expect(values, list, "activity_details.activityDetailMetrics[].metrics")

        def value(i: int | None, values: list = values) -> Any:
            return values[i] if i is not None and 0 <= i < len(values) else None

        row: dict[str, Any] = {
            "activity_id": activity_id,
            "sample_index": pos,
            "timestamp_utc": timestamp(integer(value(ts_index))),
        }
        for col, (key, factor) in _SAMPLE_METRICS.items():
            row[col] = scaled(value(index.get(key)), factor)
        rows.append(row)
    return rows
