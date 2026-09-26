from datetime import date


def test_put_raw_is_idempotent_and_versioned(store):
    assert store.put_raw("x", "k", {"a": 1, "b": 2}) is True
    assert store.put_raw("x", "k", {"b": 2, "a": 1}) is False  # same content, key order irrelevant
    assert store.con.execute("SELECT count(*) FROM raw_payload").fetchone()[0] == 1
    assert store.put_raw("x", "k", {"a": 3}) is True
    assert store.con.execute("SELECT count(*) FROM raw_payload").fetchone()[0] == 2
    assert store.latest_raw("x", "k") == {"a": 3}
    assert store.raw_versions("x", "k") == [{"a": 1, "b": 2}, {"a": 3}]


def test_latest_raw_by_endpoint_window(store):
    store.put_raw("r", "a", [1], date(2026, 1, 1), date(2026, 1, 28))
    store.put_raw("r", "b", [2], date(2026, 1, 29), date(2026, 2, 25))
    got = store.latest_raw_by_endpoint("r", date(2026, 2, 1), date(2026, 2, 2))
    assert got == [("b", [2])]


def test_migrations_rerun_safely(settings):
    from garmin_data.store import Store

    Store(settings.db_path).close()
    with Store(settings.db_path) as s:
        assert s.con.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 1
