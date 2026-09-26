import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from fakes import FakeGarmin

from garmin_data import web
from garmin_data.sync import sync


def test_format_helpers():
    assert web.fmt_duration(3725) == "1:02:05"
    assert web.fmt_duration(125) == "2:05"
    assert web.fmt_pace(300) == "5:00 /km"
    assert web.fmt_speed("running", 3.33, 300.0) == "5:00 /km"
    assert web.fmt_speed("kayaking_v2", 2.5, 400.0) == "9.0 km/h"
    assert web.fmt_num(None) == ""


def _sync_days(store, settings, days: dict[str, list[int]]) -> None:
    sync(
        FakeGarmin(days),
        store,
        settings,
        date(2026, 9, 1),
        date(2026, 9, 28),
        today=date(2026, 9, 30),
    )


def test_list_and_render(store, settings):
    _sync_days(store, settings, {"2026-09-02": [1], "2026-09-05": [2]})
    result = web.query_activities(store.con, web.ActivityQuery())
    assert [r["activity_id"] for r in result.rows] == [2, 1]  # newest first
    assert result.total == 2 and result.pages == 1
    q = web.ActivityQuery(date_from=date(2026, 9, 3))
    assert [r["activity_id"] for r in web.query_activities(store.con, q).rows] == [2]
    q = web.ActivityQuery(activity_type="cycling")
    assert web.query_activities(store.con, q).rows == []
    page = web.render_page(result, web.activity_types(store.con))
    assert "2 activities" in page and "8.00 km" in page and "5:00 /km" in page
    assert 'class="pager"' not in page  # a single page needs no pager


def test_pagination_reads_one_page_with_totals_over_all(store, settings):
    # Two activities per day at the same time (ties break on activity_id).
    days = {f"2026-09-{d:02d}": [2 * d - 1, 2 * d] for d in range(1, 29)}
    _sync_days(store, settings, days)
    total = 2 * len(days)
    result = web.query_activities(store.con, web.ActivityQuery(page=2, per_page=25))
    assert [r["activity_id"] for r in result.rows] == list(range(total - 25, total - 50, -1))
    assert result.total == total and result.pages == 3
    km = store.con.execute("SELECT sum(distance_m) FROM activity").fetchone()[0]
    assert result.total_distance_m == km  # totals cover every page
    # Out-of-range pages clamp; unknown page sizes fall back to the default.
    last = web.query_activities(store.con, web.ActivityQuery(page=99, per_page=25))
    assert last.query.page == 3 and [r["activity_id"] for r in last.rows] == [6, 5, 4, 3, 2, 1]
    assert web.query_activities(store.con, web.ActivityQuery(per_page=7)).query.per_page == 50
    page = web.render_page(result, [])
    assert 'class="pager"' in page and f"26–50 of {total}" in page
    assert 'href="?page=3&amp;per=25"' in page and 'href="?per=25"' in page  # page 1


def test_sorting(store, settings):
    _sync_days(store, settings, {"2026-09-02": [1], "2026-09-05": [2], "2026-09-07": [3]})
    store.con.execute("UPDATE activity SET distance_m = NULL WHERE activity_id = 3")
    store.con.execute("UPDATE activity SET distance_m = 5000 WHERE activity_id = 1")
    store.con.execute("UPDATE activity SET distance_m = 9000 WHERE activity_id = 2")

    def ids(**kw):
        result = web.query_activities(store.con, web.ActivityQuery(**kw))
        return [r["activity_id"] for r in result.rows]

    assert ids(sort="distance", desc=True) == [2, 1, 3]  # NULLs last either way
    assert ids(sort="distance", desc=False) == [1, 2, 3]
    assert ids(sort="date", desc=False) == [1, 2, 3]
    assert ids(sort="bogus; DROP TABLE activity") == [3, 2, 1]  # not whitelisted -> date

    result = web.query_activities(store.con, web.ActivityQuery(sort="distance", desc=True))
    page = web.render_page(result, [])
    assert "Distance ▼" in page
    assert 'href="?sort=distance&amp;dir=asc"' in page  # clicking again flips direction
    assert 'href="?sort=duration"' in page  # other columns use their default direction


def test_search(store, settings):
    _sync_days(store, settings, {"2026-09-02": [1], "2026-09-05": [2]})
    store.con.execute("UPDATE activity SET activity_name = 'Tempo 10_k' WHERE activity_id = 2")

    def ids(search):
        q = web.ActivityQuery(search=search)
        return [r["activity_id"] for r in web.query_activities(store.con, q).rows]

    assert ids("tempo") == [2]  # case-insensitive
    assert ids("RUNN") == [2, 1]  # matches the type too
    assert ids("0_k") == [2] and ids("0%k") == []  # LIKE wildcards are literal


def test_parse_query():
    q = web.parse_query(
        {"type": ["running"], "sort": ["name"], "page": ["x"], "per": ["100"], "q": ["  a  "]},
        ["running"],
    )
    assert q == web.ActivityQuery(
        activity_type="running", search="a", sort="name", desc=False, page=1, per_page=100
    )
    q = web.parse_query({"type": ["nope"], "sort": ["evil"], "dir": ["asc"]}, ["running"])
    assert q.activity_type is None and q.sort == "date" and q.desc is False


def test_render_escapes_html():
    row = {k: None for k in web._COLUMNS.replace(" ", "").split(",")}
    row["activity_name"] = "<script>alert(1)</script>"
    result = web.ActivityPage([row], 1, 0, 0, web.ActivityQuery(search="<b>"))
    page = web.render_page(result, [])
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "<b>" not in page


def test_server_serves_page_read_only(store, settings):
    sync(
        FakeGarmin({"2026-09-02": [1]}),
        store,
        settings,
        date(2026, 9, 1),
        date(2026, 9, 3),
        today=date(2026, 9, 30),
    )
    store.close()  # a writer holds a lock; the web view opens read-only per request
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(settings.db_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        query = "type=running&from=bad&sort=load&page=9&per=abc"
        body = urllib.request.urlopen(f"{base}/?{query}").read().decode()
        assert "1 activity ·" in body
        try:
            urllib.request.urlopen(f"{base}/nope")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
        server.server_close()


def _synced(store, settings):
    sync(
        FakeGarmin({"2026-09-02": [1]}),
        store,
        settings,
        date(2026, 9, 1),
        date(2026, 9, 3),
        today=date(2026, 9, 30),
    )


def test_render_activity_detail(store, settings):
    _synced(store, settings)
    store.con.execute(
        "INSERT INTO annotation (activity_id, rpe, notes, created_at, updated_at) "
        "VALUES (1, 7, '<b>windy</b>', now(), now())"
    )
    activity = web.get_activity(store.con, 1)
    children = web.activity_children(store.con, 1)
    assert len(children["samples"]) == 5 and len(children["laps"]) == 7
    page = web.render_activity(activity, children)
    assert 'data-chart="pace"' in page and 'data-chart="hr"' in page
    assert "Time in HR zones" in page and "Garmin segments" in page
    assert "&lt;b&gt;windy" in page and "<b>windy" not in page
    assert web.get_activity(store.con, 999) is None


def test_render_activity_without_samples(store, settings):
    _synced(store, settings)
    store.con.execute("DELETE FROM activity_sample")
    page = web.render_activity(web.get_activity(store.con, 1), web.activity_children(store.con, 1))
    assert "No time series stored" in page and "data-chart" not in page


def test_derived_metrics():
    xs = [0.0, 10.0, 20.0, 30.0]
    assert web.smooth(xs, [1.0, 2.0, None, 4.0], 20) == [1.5, 1.5, None, 4.0]
    steady = [
        {"timer_s": float(t), "heart_rate": 140.0 if t < 1500 else 150.0, "speed_mps": 3.0}
        for t in range(0, 3000, 10)
    ]
    assert round(web.decoupling(steady), 1) == 6.7  # same speed, higher HR later
    assert web.decoupling(steady[:50]) is None  # too short


def test_lap_bands_locate_work_laps_by_start_time():
    from datetime import datetime, timedelta

    t0 = datetime(2026, 9, 2, 5, 0, 0)
    samples = [{"timestamp_utc": t0 + timedelta(seconds=s), "timer_s": s} for s in range(0, 600, 5)]
    laps = [
        {"step_type": "warmup", "start_time_utc": t0, "duration_s": 120.0},
        {"step_type": "work", "start_time_utc": t0 + timedelta(seconds=120), "duration_s": 60.0},
        {"step_type": "recovery", "start_time_utc": t0 + timedelta(seconds=180), "duration_s": 60},
    ]
    assert web.lap_bands(laps, samples) == [(120, 180.0)]


def test_server_serves_activity_detail(store, settings):
    _synced(store, settings)
    store.close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(settings.db_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        body = urllib.request.urlopen(f"{base}/activity/1").read().decode()
        assert "Laps" in body and "← All activities" in body
        assert 'href="/activity/1"' in urllib.request.urlopen(base).read().decode()
        for path in ("/activity/999", "/activity/x"):
            try:
                urllib.request.urlopen(base + path)
                raise AssertionError("expected 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        server.shutdown()
        server.server_close()
