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


def test_list_and_render(store, settings):
    sync(
        FakeGarmin({"2026-09-02": [1], "2026-09-05": [2]}),
        store,
        settings,
        date(2026, 9, 1),
        date(2026, 9, 10),
        today=date(2026, 9, 30),
    )
    rows = web.list_activities(store.con)
    assert [r["activity_id"] for r in rows] == [2, 1]  # newest first
    assert web.list_activities(store.con, date_from=date(2026, 9, 3))[0]["activity_id"] == 2
    assert web.list_activities(store.con, activity_type="cycling") == []
    page = web.render_page(rows, web.activity_types(store.con), None, None, None)
    assert "2 activities" in page and "8.00 km" in page and "5:00 /km" in page


def test_render_escapes_html():
    row = {k: None for k in web._COLUMNS.replace(" ", "").split(",")}
    row["activity_name"] = "<script>alert(1)</script>"
    page = web.render_page([row], [], None, None, None)
    assert "<script>alert" not in page and "&lt;script&gt;" in page


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
        body = urllib.request.urlopen(f"{base}/?type=running&from=bad").read().decode()
        assert "1 activity ·" in body
        try:
            urllib.request.urlopen(f"{base}/nope")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
        server.server_close()
