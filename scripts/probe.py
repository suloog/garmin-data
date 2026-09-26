"""Fetch sample payloads from every endpoint the project uses and dump them locally.

Purpose: verify real payload structures before (and when updating) the
normalization code. Output goes to ``probe_output/`` (gitignored) and contains
personal data — do not commit it.

Usage: uv run python scripts/probe.py [YYYY-MM-DD] [activity_id]
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

from garmin_data.auth import get_client
from garmin_data.config import load_settings

OUT = Path("probe_output")


def dump(name: str, fn, *args) -> object:
    try:
        data = fn(*args)
        status = "ok"
    except Exception as e:  # noqa: BLE001 - probe records every failure
        data = {"__error__": f"{type(e).__name__}: {e}"}
        status = "ERROR"
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False))
    kind = type(data).__name__
    size = len(data) if isinstance(data, (list, dict)) else "-"
    print(f"{status:5} {name:40} {kind:6} len={size}")
    return data


def main() -> None:
    OUT.mkdir(exist_ok=True)
    day = sys.argv[1] if len(sys.argv) > 1 else (date.today() - timedelta(days=1)).isoformat()
    d = date.fromisoformat(day)
    start = (d - timedelta(days=2)).isoformat()
    g = get_client(load_settings())

    acts = dump(
        "activities_by_date", g.get_activities_by_date, (d - timedelta(days=60)).isoformat(), day
    )
    if len(sys.argv) > 2:
        activity_ids = [sys.argv[2]]
    else:
        runs = [
            a for a in acts if (a.get("activityType") or {}).get("typeKey", "").endswith("running")
        ]
        structured = [a for a in runs if a.get("workoutId")]
        chosen = (structured[:1] or []) + runs[:1]
        activity_ids = list(dict.fromkeys(str(a["activityId"]) for a in chosen))

    for aid in activity_ids:
        dump(f"activity_{aid}", g.get_activity, aid)
        dump(f"activity_{aid}_splits", g.get_activity_splits, aid)
        dump(f"activity_{aid}_typed_splits", g.get_activity_typed_splits, aid)
        dump(f"activity_{aid}_split_summaries", g.get_activity_split_summaries, aid)
        dump(f"activity_{aid}_hr_zones", g.get_activity_hr_in_timezones, aid)
        dump(f"activity_{aid}_power_zones", g.get_activity_power_in_timezones, aid)
        dump(f"activity_{aid}_weather", g.get_activity_weather, aid)
        dump(f"activity_{aid}_gear", g.get_activity_gear, aid)
        act = next((a for a in acts if str(a["activityId"]) == aid), {})
        if act.get("workoutId"):
            dump(f"workout_{act['workoutId']}", g.get_workout_by_id, act["workoutId"])

    dump("user_summary", g.get_user_summary, day)
    dump("sleep_data", g.get_sleep_data, day)
    dump("training_readiness", g.get_training_readiness, day)
    dump("training_status", g.get_training_status, day)
    dump("hrv_data", g.get_hrv_data, day)
    dump("sleep_daily_range", g.get_sleep_daily, start, day)
    dump("hrv_range", g.get_hrv_data_range, start, day)
    dump("rhr_range", g.get_rhr_daily, start, day)
    dump("calories_range", g.get_calories_daily, start, day)
    dump("body_battery_range", g.get_body_battery, start, day)
    dump("weigh_ins_range", g.get_weigh_ins, (d - timedelta(days=30)).isoformat(), day)
    dump("max_metrics_range", g.get_max_metrics_range, start, day)
    dump("devices", g.get_devices)


if __name__ == "__main__":
    main()
