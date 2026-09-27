"""Minimal local web view: a list of activities and a detail page per activity.

Standard library only (``http.server``). Read-only: every request opens a
short-lived read-only DuckDB connection, so the view never modifies data.
Intended for localhost; it has no authentication.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import duckdb

from . import web_charts as charts

log = logging.getLogger(__name__)

# Activity types where pace (min/km) is the natural unit; others show km/h.
_PACE_TYPES = ("running", "walking", "hiking")

_COLUMNS = (
    "activity_id, date, start_time_local, activity_type, activity_name, distance_m, "
    "duration_s, avg_speed_mps, avg_pace_s_per_km, avg_hr, max_hr, avg_cadence, "
    "elevation_gain_m, aerobic_te, anaerobic_te, training_load, garmin_rpe, garmin_feel"
)


# -- data -------------------------------------------------------------------------

# Sortable columns: URL key -> (SQL expression, default direction is descending).
# Only these expressions ever reach the ORDER BY clause.
SORTS: dict[str, tuple[str, bool]] = {
    "date": ("start_time_local", True),
    "type": ("activity_type", False),
    "name": ("lower(activity_name)", False),
    "distance": ("distance_m", True),
    "duration": ("duration_s", True),
    "speed": ("avg_speed_mps", True),
    "avg_hr": ("avg_hr", True),
    "max_hr": ("max_hr", True),
    "cadence": ("avg_cadence", True),
    "elevation": ("elevation_gain_m", True),
    "te": ("aerobic_te", True),
    "load": ("training_load", True),
    "rpe": ("garmin_rpe", True),
    "feel": ("garmin_feel", True),
}
DEFAULT_SORT = "date"
PAGE_SIZES = (25, 50, 100, 200)
DEFAULT_PAGE_SIZE = 50


@dataclass(frozen=True)
class ActivityQuery:
    """Filter, search, sort and page of the activity list (all applied in SQL)."""

    activity_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    search: str | None = None
    sort: str = DEFAULT_SORT
    desc: bool = True
    page: int = 1
    per_page: int = DEFAULT_PAGE_SIZE


@dataclass(frozen=True)
class ActivityPage:
    rows: list[dict[str, Any]]
    total: int  # activities matching the filters, across all pages
    total_distance_m: float
    total_duration_s: float
    query: ActivityQuery  # with ``page`` clamped to the existing range

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.query.per_page))


def _where(q: ActivityQuery) -> tuple[str, list[Any]]:
    clauses, params = [], []
    if q.activity_type:
        clauses.append("activity_type = ?")
        params.append(q.activity_type)
    if q.date_from:
        clauses.append("date >= ?")
        params.append(q.date_from)
    if q.date_to:
        clauses.append("date <= ?")
        params.append(q.date_to)
    if q.search:
        pattern = "%" + q.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(activity_name ILIKE ? ESCAPE '\\' OR activity_type ILIKE ? ESCAPE '\\')")
        params += [pattern, pattern]
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def query_activities(con: duckdb.DuckDBPyConnection, q: ActivityQuery) -> ActivityPage:
    """One page of activities matching ``q``; only that page is read from the database."""
    where, params = _where(q)
    total, dist, dur = con.execute(
        "SELECT count(*), coalesce(sum(distance_m), 0), coalesce(sum(duration_s), 0) "
        f"FROM activity{where}",
        params,
    ).fetchone()
    per_page = q.per_page if q.per_page in PAGE_SIZES else DEFAULT_PAGE_SIZE
    pages = max(1, math.ceil(total / per_page))
    q = replace(q, per_page=per_page, page=min(max(q.page, 1), pages))
    expr, _ = SORTS.get(q.sort, SORTS[DEFAULT_SORT])
    direction = "DESC" if q.desc else "ASC"
    cur = con.execute(
        f"SELECT {_COLUMNS} FROM activity{where} "
        f"ORDER BY {expr} {direction} NULLS LAST, activity_id {direction} "
        "LIMIT ? OFFSET ?",
        [*params, per_page, (q.page - 1) * per_page],
    )
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    return ActivityPage(rows, total, dist, dur, q)


def activity_types(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        "SELECT activity_type FROM activity WHERE activity_type IS NOT NULL "
        "GROUP BY 1 ORDER BY count(*) DESC, 1"
    ).fetchall()
    return [r[0] for r in rows]


def _rows(cur: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def get_activity(con: duckdb.DuckDBPyConnection, activity_id: int) -> dict[str, Any] | None:
    rows = _rows(con.execute("SELECT * FROM activity WHERE activity_id = ?", [activity_id]))
    return rows[0] if rows else None


def activity_children(con: duckdb.DuckDBPyConnection, activity_id: int) -> dict[str, Any]:
    """Laps, splits, HR zones, samples and manual notes of one activity."""

    def q(sql: str) -> list[dict[str, Any]]:
        return _rows(con.execute(sql, [activity_id]))

    # A database created before schema v2 has no samples table until the next sync.
    has_samples = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'activity_sample'"
    ).fetchone()[0]

    return {
        "laps": q("SELECT * FROM activity_lap WHERE activity_id = ? ORDER BY sequence"),
        "splits": q("SELECT * FROM activity_split WHERE activity_id = ? ORDER BY sequence"),
        "zones": q("SELECT * FROM activity_hr_zone WHERE activity_id = ? ORDER BY zone"),
        "samples": []
        if not has_samples
        else q(
            "SELECT sample_index, timestamp_utc, timer_s, distance_m, heart_rate, speed_mps, "
            "grade_adjusted_speed_mps, cadence, elevation_m, power "
            "FROM activity_sample WHERE activity_id = ? ORDER BY sample_index"
        ),
        "notes": q(
            "SELECT * FROM annotation WHERE activity_id = ? ORDER BY updated_at DESC, id DESC"
        ),
    }


# -- derived metrics (computed at request time, not stored) -------------------------

# Below this speed a sample counts as standing/walking for pace charts (m/s; 16:40 /km).
_MIN_PACE_SPEED = 1.0


def smooth(xs: list[float], ys: list[float | None], window_s: float) -> list[float | None]:
    """Centered moving average over a time window; ``None`` values are skipped."""
    out: list[float | None] = []
    lo = hi = 0
    total, count = 0.0, 0
    half = window_s / 2
    for i, x in enumerate(xs):
        while hi < len(xs) and xs[hi] <= x + half:
            if ys[hi] is not None:
                total += ys[hi]
                count += 1
            hi += 1
        while xs[lo] < x - half:
            if ys[lo] is not None:
                total -= ys[lo]
                count -= 1
            lo += 1
        out.append(total / count if count and ys[i] is not None else None)
    return out


def _intervals(
    samples: list[dict[str, Any]], keys: tuple[str, ...], min_speed: float = 0.0
) -> list[tuple[float, float, tuple[float, ...]]]:
    """(start, end, values) per interval between consecutive samples, in timer time.

    Garmin's samples are not evenly spaced, so derived means are integrated
    over time: each interval carries the average of its two end samples
    (trapezoid rule). Intervals where either end lacks a value or is below
    ``min_speed`` (standing, walking) are left out.
    """

    def ok(s: dict[str, Any]) -> bool:
        return (
            s["timer_s"] is not None
            and all(s[k] is not None for k in keys)
            and (s["speed_mps"] or 0) >= min_speed
        )

    out = []
    for x, y in zip(samples, samples[1:], strict=False):
        if ok(x) and ok(y) and y["timer_s"] > x["timer_s"]:
            values = tuple((x[k] + y[k]) / 2 for k in keys)
            out.append((x["timer_s"], y["timer_s"], values))
    return out


def decoupling(samples: list[dict[str, Any]]) -> float | None:
    """Pa:HR aerobic decoupling in percent: how much the speed/HR ratio drops
    from the first to the second half (by moving timer time). Only meaningful
    for steady efforts; None if there is too little data (< 20 min moving)."""
    intervals = [
        iv
        for iv in _intervals(samples, ("speed_mps", "heart_rate"), _MIN_PACE_SPEED)
        if iv[2][1] > 0
    ]
    total = sum(t1 - t0 for t0, t1, _ in intervals)
    if total < 1200:
        return None
    # Split at half of the moving time; the interval crossing it is divided.
    halves = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]  # duration, speed*s, hr*s
    elapsed = 0.0
    for t0, t1, (speed, hr) in intervals:
        dt = t1 - t0
        first = min(dt, max(0.0, total / 2 - elapsed))
        for half, part in ((halves[0], first), (halves[1], dt - first)):
            half[0] += part
            half[1] += speed * part
            half[2] += hr * part
        elapsed += dt
    ef1, ef2 = (h[1] / h[2] for h in halves)
    return (ef1 - ef2) / ef1 * 100


def mean_of(samples: list[dict[str, Any]], key: str, min_speed: float = 0.0) -> float | None:
    """Time-weighted mean of ``key`` over intervals with speed >= ``min_speed``."""
    intervals = _intervals(samples, (key,), min_speed)
    total = sum(t1 - t0 for t0, t1, _ in intervals)
    return sum((t1 - t0) * v for t0, t1, (v,) in intervals) / total if total else None


def lap_bands(
    laps: list[dict[str, Any]], samples: list[dict[str, Any]]
) -> list[tuple[float, float]]:
    """Timer-time ranges of work laps, located via their UTC start time."""
    times = [s["timestamp_utc"] for s in samples]
    if not times or None in times:
        return []
    bands = []
    for lap in laps:
        if lap["step_type"] != "work" or lap["start_time_utc"] is None:
            continue
        i = min(bisect_left(times, lap["start_time_utc"]), len(samples) - 1)
        start = samples[i]["timer_s"]
        if start is not None and lap["duration_s"]:
            bands.append((start, start + lap["duration_s"]))
    return bands


# -- formatting ---------------------------------------------------------------------


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_pace(s_per_km: float | None) -> str:
    if s_per_km is None:
        return ""
    m, s = divmod(int(round(s_per_km)), 60)
    return f"{m}:{s:02d} /km"


def is_pace_type(activity_type: str | None) -> bool:
    return bool(activity_type) and any(t in activity_type for t in _PACE_TYPES)


def fmt_speed(activity_type: str | None, speed_mps: float | None, pace: float | None) -> str:
    if is_pace_type(activity_type):
        return fmt_pace(pace)
    return "" if speed_mps is None else f"{speed_mps * 3.6:.1f} km/h"


def fmt_num(value: float | None, digits: int = 0, suffix: str = "") -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}{suffix}"


_FEEL = {0: "very weak", 25: "weak", 50: "normal", 75: "strong", 100: "very strong"}


# -- rendering ----------------------------------------------------------------------

_CSS = """
:root { --bg:#fbfbfa; --fg:#1d1d1f; --muted:#6b6b70; --line:#e4e4e2; --head:#f2f2f0;
        --accent:#2f6fde; --row:#f7f7f5;
        --c-pace:#2a78d6; --c-hr:#e34948; --c-cad:#1baf7a; --c-elev:#8a8983;
        --band:rgba(42,120,214,.09); --bar-muted:#c9c8c3;
        --z1:#86b6ef; --z2:#5598e7; --z3:#2a78d6; --z4:#1c5cab; --z5:#104281; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161618; --fg:#ececee; --muted:#9a9aa2; --line:#2c2c30; --head:#1f1f23;
          --accent:#7aa7ff; --row:#1b1b1e;
          --c-pace:#3987e5; --c-hr:#e66767; --c-cad:#199e70; --c-elev:#8f8e87;
          --band:rgba(57,135,229,.16); --bar-muted:#4a4a47;
          --z1:#184f95; --z2:#256abf; --z3:#3987e5; --z4:#6da7ec; --z5:#9ec5f4; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1280px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 20px; margin: 0 0 4px; }
.summary { color: var(--muted); margin: 0 0 16px; }
form { display:flex; flex-wrap:wrap; gap:8px 12px; align-items:end; margin-bottom:16px; }
label { display:flex; flex-direction:column; font-size:12px; color:var(--muted); gap:2px; }
select, input, button { font:inherit; padding:5px 8px; border:1px solid var(--line);
       border-radius:6px; background:var(--bg); color:var(--fg); }
button { cursor:pointer; }
a { color: var(--accent); }
.wrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
table { border-collapse: collapse; width: 100%; white-space: nowrap; }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--line); text-align: left; }
th { background: var(--head); font-weight:600; font-size:12px; color:var(--muted);
     position: sticky; top: 0; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:nth-child(even) { background: var(--row); }
td.name { max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
.empty { padding: 24px; color: var(--muted); }
th a { color: inherit; text-decoration: none; }
th a:hover, th.sorted a { color: var(--fg); }
.pager { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-top:12px;
         color: var(--muted); }
.pager a, .pager span.cur { padding: 3px 9px; border:1px solid var(--line); border-radius:6px;
         text-decoration:none; }
.pager span.cur { color: var(--fg); background: var(--head); font-weight:600; }
.pager .info { margin-left:auto; }
h2 { font-size: 15px; margin: 28px 0 10px; }
.back { display:inline-block; margin-bottom: 8px; }
.tiles { display:grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
.tile { border:1px solid var(--line); border-radius:8px; padding:8px 12px; }
.tile .k { font-size:12px; color:var(--muted); }
.tile .v { font-size:18px; font-weight:600; font-variant-numeric: tabular-nums; }
.tile .s { font-size:12px; color:var(--muted); font-variant-numeric: tabular-nums; }
.readout { position: sticky; top: 0; z-index: 1; background: var(--bg); min-height: 28px;
           padding: 4px 0; display:flex; flex-wrap:wrap; gap: 4px 18px; color: var(--muted);
           font-variant-numeric: tabular-nums; border-bottom: 1px solid var(--line); }
.readout b { color: var(--fg); font-weight: 600; margin-right: 4px; }
figure.chart { margin: 10px 0 0; }
figcaption { font-size: 12px; color: var(--muted); margin-bottom: 2px; }
svg { display:block; width:100%; height:auto; }
svg .grid { stroke: var(--line); stroke-width: 1; }
svg .axis { stroke: var(--muted); stroke-width: 1; opacity: .5; }
svg .tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
svg .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round;
            vector-effect: non-scaling-stroke; }
svg .area { opacity: .18; stroke: none; }
svg .band { fill: var(--band); }
svg .xh { stroke: var(--fg); stroke-width: 1; opacity: .6; vector-effect: non-scaling-stroke; }
svg .bar { fill: var(--bar-muted); }
svg .bar.hi { fill: var(--c-pace); }
svg .bar:hover, svg .bar:focus { opacity: .75; outline: none; }
.legend { display:flex; gap: 14px; font-size: 12px; color: var(--muted); margin: 4px 0 8px; }
.legend i { display:inline-block; width: 10px; height: 10px; border-radius: 2px;
            margin-right: 5px; vertical-align: -1px; }
.cols { display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 0 32px; }
.zones { display:grid; grid-template-columns: auto 1fr auto; gap: 6px 10px; align-items:center;
         font-variant-numeric: tabular-nums; }
.zones .track { height: 14px; }
.zones .fill { height: 14px; border-radius: 0 4px 4px 0; min-width: 2px; }
.zones .lbl { font-size: 12px; color: var(--muted); }
dl.kv { display:grid; grid-template-columns: auto 1fr; gap: 6px 16px; margin: 0; }
dl.kv dt { color: var(--muted); }
dl.kv dd { margin: 0; font-variant-numeric: tabular-nums; }
.note { color: var(--muted); font-size: 12px; margin-top: 8px; }
tr.work td { font-weight: 600; }
"""

# (label, CSS class, sort key or None)
_HEADERS = (
    ("Date", "", "date"),
    ("Time", "", None),
    ("Type", "", "type"),
    ("Name", "", "name"),
    ("Distance", "n", "distance"),
    ("Duration", "n", "duration"),
    ("Pace / speed", "n", "speed"),
    ("Avg HR", "n", "avg_hr"),
    ("Max HR", "n", "max_hr"),
    ("Cadence", "n", "cadence"),
    ("Elev +", "n", "elevation"),
    ("TE aer/ana", "n", "te"),
    ("Load", "n", "load"),
    ("RPE", "n", "rpe"),
    ("Feel", "", "feel"),
)


def _cells(a: dict[str, Any]) -> list[tuple[str, str]]:
    te = ""
    if a["aerobic_te"] is not None or a["anaerobic_te"] is not None:
        te = f"{fmt_num(a['aerobic_te'], 1)} / {fmt_num(a['anaerobic_te'], 1)}"
    start = a["start_time_local"]
    return [
        (str(a["date"] or ""), ""),
        (start.strftime("%H:%M") if start else "", ""),
        (a["activity_type"] or "", ""),
        (a["activity_name"] or "(unnamed)", "name"),
        (fmt_num(None if a["distance_m"] is None else a["distance_m"] / 1000, 2, " km"), "n"),
        (fmt_duration(a["duration_s"]), "n"),
        (fmt_speed(a["activity_type"], a["avg_speed_mps"], a["avg_pace_s_per_km"]), "n"),
        (fmt_num(a["avg_hr"]), "n"),
        (fmt_num(a["max_hr"]), "n"),
        (fmt_num(a["avg_cadence"]), "n"),
        (fmt_num(a["elevation_gain_m"], 0, " m"), "n"),
        (te, "n"),
        (fmt_num(a["training_load"]), "n"),
        (fmt_num(a["garmin_rpe"]), "n"),
        (_FEEL.get(a["garmin_feel"], "") if a["garmin_feel"] is not None else "", ""),
    ]


def query_string(q: ActivityQuery, **changes: Any) -> str:
    """URL query for ``q`` with ``changes`` applied; defaults are omitted."""
    q = replace(q, **changes)
    params = {
        "type": q.activity_type or "",
        "from": str(q.date_from or ""),
        "to": str(q.date_to or ""),
        "q": q.search or "",
        "sort": q.sort if q.sort != DEFAULT_SORT else "",
        "dir": ("desc" if q.desc else "asc") if q.desc != SORTS[q.sort][1] else "",
        "page": str(q.page) if q.page > 1 else "",
        "per": str(q.per_page) if q.per_page != DEFAULT_PAGE_SIZE else "",
    }
    return "?" + urlencode({k: v for k, v in params.items() if v})


def _header(q: ActivityQuery, label: str, css: str, key: str | None) -> str:
    esc = html.escape
    if key is None:
        return f'<th class="{css}">{esc(label)}</th>'
    if key == q.sort:
        desc, arrow, css = not q.desc, (" ▼" if q.desc else " ▲"), f"{css} sorted"
    else:
        desc, arrow = SORTS[key][1], ""
    href = esc(query_string(q, sort=key, desc=desc, page=1))
    return f'<th class="{css.strip()}"><a href="{href}">{esc(label)}{arrow}</a></th>'


def _pager(result: ActivityPage) -> str:
    q, pages = result.query, result.pages
    if pages <= 1:
        return ""
    shown = sorted({1, pages, *range(max(1, q.page - 2), min(pages, q.page + 2) + 1)})
    parts, prev = [], 0
    if q.page > 1:
        parts.append(f'<a href="{html.escape(query_string(q, page=q.page - 1))}">‹ Prev</a>')
    for n in shown:
        if n - prev > 1:
            parts.append("<span>…</span>")
        if n == q.page:
            parts.append(f'<span class="cur">{n}</span>')
        else:
            parts.append(f'<a href="{html.escape(query_string(q, page=n))}">{n}</a>')
        prev = n
    if q.page < pages:
        parts.append(f'<a href="{html.escape(query_string(q, page=q.page + 1))}">Next ›</a>')
    first = (q.page - 1) * q.per_page + 1
    last = first + len(result.rows) - 1
    parts.append(f'<span class="info">{first}–{last} of {result.total}</span>')
    return f'<nav class="pager">{"".join(parts)}</nav>'


def render_page(result: ActivityPage, types: list[str]) -> str:
    esc = html.escape
    q = result.query
    count = f"{result.total} activit{'y' if result.total == 1 else 'ies'}"
    options = ['<option value="">all</option>'] + [
        f'<option value="{esc(t)}"{" selected" if t == q.activity_type else ""}>{esc(t)}</option>'
        for t in types
    ]
    per_options = "".join(
        f'<option value="{n}"{" selected" if n == q.per_page else ""}>{n}</option>'
        for n in PAGE_SIZES
    )
    head = "".join(_header(q, *h) for h in _HEADERS)

    def cell(a: dict[str, Any], value: str, cls: str) -> str:
        if cls == "name" and a["activity_id"] is not None:
            value = f'<a href="/activity/{int(a["activity_id"])}">{esc(value)}</a>'
        else:
            value = esc(value)
        return f'<td class="{cls}">{value}</td>'

    body = "".join(
        "<tr>" + "".join(cell(a, v, c) for v, c in _cells(a)) + "</tr>" for a in result.rows
    )
    table = (
        f'<div class="wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>{_pager(result)}"
        if result.rows
        else '<p class="empty">No activities match. Run <code>garmin-data sync</code> first?</p>'
    )
    # Sorting survives a new filter submit; the page resets to 1.
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{esc(v)}">'
        for k, v in (
            ("sort", q.sort if q.sort != DEFAULT_SORT else ""),
            ("dir", "desc" if q.desc else "asc"),
        )
        if v
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Training log</title><style>{_CSS}</style></head>
<body><main>
<h1>Training log</h1>
<p class="summary">{count} · {result.total_distance_m / 1000:.1f} km · \
{fmt_duration(result.total_duration_s)}</p>
<form method="get">
  <label>Search<input type="search" name="q" value="{esc(q.search or "")}"
    placeholder="name or type"></label>
  <label>Type<select name="type">{"".join(options)}</select></label>
  <label>From<input type="date" name="from" value="{esc(str(q.date_from or ""))}"></label>
  <label>To<input type="date" name="to" value="{esc(str(q.date_to or ""))}"></label>
  <label>Per page<select name="per">{per_options}</select></label>
  {hidden}<button type="submit">Filter</button> <a href="/">Reset</a>
</form>
{table}
</main></body></html>"""


_STEP_LABEL = {"unknown": "", "work": "work"}


def _tile(key: str, value: str, sub: str = "") -> str:
    if not value:
        return ""
    esc = html.escape
    sub_html = f'<div class="s">{esc(sub)}</div>' if sub else ""
    return (
        f'<div class="tile"><div class="k">{esc(key)}</div>'
        f'<div class="v">{esc(value)}</div>{sub_html}</div>'
    )


def _join(*parts: str) -> str:
    return " · ".join(p for p in parts if p)


def _km(meters: float | None, digits: int = 2) -> str:
    return fmt_num(None if meters is None else meters / 1000, digits, " km")


def _tiles(a: dict[str, Any]) -> str:
    pace = is_pace_type(a["activity_type"])
    speed = fmt_speed(a["activity_type"], a["avg_speed_mps"], a["avg_pace_s_per_km"])
    best = fmt_speed(a["activity_type"], a["max_speed_mps"], a["best_pace_s_per_km"])
    te = ""
    if a["aerobic_te"] is not None or a["anaerobic_te"] is not None:
        te = f"{fmt_num(a['aerobic_te'], 1)} / {fmt_num(a['anaerobic_te'], 1)}"
    elev = ""
    if a["elevation_gain_m"] is not None or a["elevation_loss_m"] is not None:
        elev = f"+{fmt_num(a['elevation_gain_m'])} / −{fmt_num(a['elevation_loss_m'])} m"
    weather = fmt_num(a["temperature_c"], 0, " °C")
    feel = _FEEL.get(a["garmin_feel"], "") if a["garmin_feel"] is not None else ""
    return "".join(
        [
            _tile("Distance", _km(a["distance_m"])),
            _tile(
                "Time",
                fmt_duration(a["duration_s"]),
                _join(
                    f"moving {fmt_duration(a['moving_time_s'])}" if a["moving_time_s"] else "",
                    f"elapsed {fmt_duration(a['elapsed_time_s'])}" if a["elapsed_time_s"] else "",
                ),
            ),
            _tile("Avg pace" if pace else "Avg speed", speed, f"best {best}" if best else ""),
            _tile(
                "Avg HR",
                fmt_num(a["avg_hr"], 0, " bpm"),
                f"max {fmt_num(a['max_hr'])}" if a["max_hr"] else "",
            ),
            _tile(
                "Cadence",
                fmt_num(a["avg_cadence"], 0, " spm"),
                _join(
                    f"max {fmt_num(a['max_cadence'])}" if a["max_cadence"] else "",
                    f"stride {fmt_num(a['avg_stride_length_m'], 2, ' m')}"
                    if a["avg_stride_length_m"]
                    else "",
                ),
            ),
            _tile("Elevation", elev),
            _tile(
                "Power",
                fmt_num(a["avg_power"], 0, " W"),
                _join(
                    f"NP {fmt_num(a['normalized_power'])}" if a["normalized_power"] else "",
                    f"max {fmt_num(a['max_power'])}" if a["max_power"] else "",
                ),
            ),
            _tile(
                "Training effect",
                te,
                f"load {fmt_num(a['training_load'])}" if a["training_load"] else "",
            ),
            _tile("Calories", fmt_num(a["calories"], 0, " kcal")),
            _tile(
                "Weather",
                weather,
                _join(
                    fmt_num(a["humidity_pct"], 0, "% RH"),
                    a["weather_desc"] or "",
                    f"feels {fmt_num(a['apparent_temperature_c'], 0, ' °C')}"
                    if a["apparent_temperature_c"] is not None
                    else "",
                ),
            ),
            _tile("Garmin RPE", fmt_num(a["garmin_rpe"]), feel),
        ]
    )


def _charts(a: dict[str, Any], samples: list[dict[str, Any]], laps: list[dict[str, Any]]) -> str:
    samples = [s for s in samples if s["timer_s"] is not None]
    if len(samples) < 2:
        return (
            '<p class="empty">No time series stored for this activity. '
            "Run <code>garmin-data sync</code> to download it.</p>"
        )
    pace_mode = is_pace_type(a["activity_type"])
    xs = [s["timer_s"] for s in samples]
    bands = lap_bands(laps, samples)
    speed = smooth(xs, [s["speed_mps"] for s in samples], 20)
    hr = [s["heart_rate"] for s in samples]
    cad = smooth(xs, [s["cadence"] if (s["cadence"] or 0) > 0 else None for s in samples], 10)
    elev = [s["elevation_m"] for s in samples]
    series: dict[str, list[Any]] = {
        "t": xs,
        "d": [None if s["distance_m"] is None else round(s["distance_m"]) for s in samples],
    }
    out: list[str] = []

    if pace_mode:
        pace = [1000 / v if v and v >= _MIN_PACE_SPEED else None for v in speed]
        valid = [p for p in pace if p is not None]
        if valid:
            # Keep the fast end fully visible; slow outliers (walking, recovery
            # jogs) are clamped to the bottom so they don't flatten the rest.
            lo = charts.percentile(valid, 0.01)
            hi = min(charts.percentile(valid, 0.98), charts.percentile(valid, 0.5) * 1.35)
            hi = max(hi, lo + 30)
            pad = max((hi - lo) * 0.06, 5)
            span = hi - lo + 2 * pad
            out.append(
                charts.line_chart(
                    name="pace",
                    title="Pace (min/km, 20 s smoothing; faster up, slow parts clamped)",
                    xs=xs,
                    ys=pace,
                    color="--c-pace",
                    y_fmt=lambda v: fmt_pace(v).removesuffix(" /km"),
                    y_domain=(lo - pad, hi + pad),
                    invert=True,
                    bands=bands,
                    y_step=30 if span <= 150 else 60 if span <= 360 else 120,
                    height=170,
                )
            )
            series["pace"] = [None if p is None else round(p, 1) for p in pace]
    else:
        kmh = [None if v is None else v * 3.6 for v in speed]
        valid = [v for v in kmh if v is not None]
        if valid:
            out.append(
                charts.line_chart(
                    name="speed",
                    title="Speed (km/h, 20 s smoothing)",
                    xs=xs,
                    ys=kmh,
                    color="--c-pace",
                    y_fmt=lambda v: f"{v:.0f}",
                    y_domain=(0, max(valid) * 1.05),
                    bands=bands,
                    height=170,
                )
            )
            series["speed"] = [None if v is None else round(v, 1) for v in kmh]

    valid_hr = [v for v in hr if v]
    if valid_hr:
        lo, hi = charts.percentile(valid_hr, 0.01) - 5, max(valid_hr) + 5
        out.append(
            charts.line_chart(
                name="hr",
                title="Heart rate (bpm)",
                xs=xs,
                ys=[v if v else None for v in hr],
                color="--c-hr",
                y_fmt=lambda v: f"{v:.0f}",
                y_domain=(lo, hi),
                y_step=10 if hi - lo <= 60 else 20,
                bands=bands,
                height=170,
            )
        )
        series["hr"] = [v if v else None for v in hr]

    valid_cad = [v for v in cad if v is not None]
    if pace_mode and valid_cad:
        out.append(
            charts.line_chart(
                name="cad",
                title="Cadence (spm, 10 s smoothing)",
                xs=xs,
                ys=cad,
                color="--c-cad",
                y_fmt=lambda v: f"{v:.0f}",
                y_domain=(
                    charts.percentile(valid_cad, 0.03) - 5,
                    charts.percentile(valid_cad, 0.995) + 5,
                ),
                bands=bands,
                height=120,
            )
        )
        series["cad"] = [None if v is None else round(v) for v in cad]

    valid_elev = [v for v in elev if v is not None]
    if valid_elev:
        lo, hi = min(valid_elev), max(valid_elev)
        pad = max(0.0, 10 - (hi - lo)) / 2 + 2
        out.append(
            charts.line_chart(
                name="elev",
                title="Elevation (m)",
                xs=xs,
                ys=elev,
                color="--c-elev",
                y_fmt=lambda v: f"{v:.0f}",
                y_domain=(lo - pad, hi + pad),
                area=True,
                bands=bands,
                height=110,
            )
        )
        series["elev"] = [None if v is None else round(v, 1) for v in elev]

    band_note = (
        '<p class="note">Shaded: work laps of the structured workout. X axis: timer time.</p>'
        if bands
        else '<p class="note">X axis: timer time (pauses excluded).</p>'
    )
    data = json.dumps(series, separators=(",", ":")).replace("</", "<\\/")
    return (
        '<div class="readout" id="readout" aria-live="polite">Hover a chart for values</div>'
        + "".join(out)
        + band_note
        + f'<script type="application/json" id="series">{data}</script>'
        + f"<script>{_HOVER_JS}</script>"
    )


_HOVER_JS = """
(() => {
  const data = JSON.parse(document.getElementById('series').textContent);
  const out = document.getElementById('readout');
  const svgs = [...document.querySelectorAll('svg[data-chart]')];
  const clock = s => { s = Math.round(s); const h = Math.floor(s / 3600),
    m = Math.floor(s % 3600 / 60), x = String(s % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`; };
  const fields = [
    ['t', 'time', clock], ['d', 'km', v => (v / 1000).toFixed(2)],
    ['pace', '/km', clock], ['speed', 'km/h', v => v.toFixed(1)],
    ['hr', 'bpm', v => v.toFixed(0)], ['cad', 'spm', v => v.toFixed(0)],
    ['elev', 'm', v => v.toFixed(0)],
  ].filter(f => data[f[0]]);
  const nearest = t => { let lo = 0, hi = data.t.length - 1;
    while (lo < hi) { const m = (lo + hi) >> 1; if (data.t[m] < t) lo = m + 1; else hi = m; }
    if (lo > 0 && t - data.t[lo - 1] < data.t[lo] - t) lo--; return lo; };
  const show = i => {
    const t = data.t[i];
    for (const s of svgs) {
      const pl = +s.dataset.padl, pr = +s.dataset.padr, xm = +s.dataset.xmax;
      const x = pl + t / xm * (1000 - pl - pr), l = s.querySelector('.xh');
      l.setAttribute('x1', x); l.setAttribute('x2', x); l.setAttribute('visibility', 'visible');
    }
    out.replaceChildren(...fields.map(([k, label, f]) => {
      const span = document.createElement('span'), b = document.createElement('b');
      const v = data[k][i]; b.textContent = v == null ? '–' : f(v);
      span.append(b, document.createTextNode(label)); return span; }));
  };
  for (const s of svgs) {
    s.addEventListener('pointermove', e => {
      const r = s.getBoundingClientRect(), pl = +s.dataset.padl, pr = +s.dataset.padr;
      const vx = (e.clientX - r.left) / r.width * 1000;
      show(nearest(Math.max(0, (vx - pl) / (1000 - pl - pr) * +s.dataset.xmax)));
    });
    s.addEventListener('pointerleave', () => svgs.forEach(
      x => x.querySelector('.xh').setAttribute('visibility', 'hidden')));
  }
})();
"""


def _zones(zones: list[dict[str, Any]]) -> str:
    total = sum(z["seconds"] or 0 for z in zones)
    if not zones or not total:
        return '<p class="empty">No HR zone data.</p>'
    peak = max(z["seconds"] or 0 for z in zones)
    rows = []
    for z in zones:
        sec = z["seconds"] or 0
        low = f"≥ {z['zone_low_hr']} bpm" if z["zone_low_hr"] is not None else ""
        color = f"var(--z{min(max(z['zone'], 1), 5)})"
        rows.append(
            f'<div>Z{z["zone"]} <span class="lbl">{html.escape(low)}</span></div>'
            f'<div class="track" title="{fmt_duration(sec)}">'
            f'<div class="fill" style="width:{sec / peak * 100:.1f}%;background:{color}">'
            "</div></div>"
            f'<div>{fmt_duration(sec)} <span class="lbl">{sec / total * 100:.0f}%</span></div>'
        )
    return f'<div class="zones">{"".join(rows)}</div>'


def _derived(samples: list[dict[str, Any]], pace_mode: bool) -> str:
    if not samples:
        return '<p class="empty">Needs the time series.</p>'
    items = []
    if pace_mode:
        gap = mean_of(samples, "grade_adjusted_speed_mps", _MIN_PACE_SPEED)
        if gap:
            items.append(("Grade-adjusted pace", fmt_pace(1000 / gap)))
        moving_speed = mean_of(samples, "speed_mps", _MIN_PACE_SPEED)
        if moving_speed:
            items.append(("Pace while moving", fmt_pace(1000 / moving_speed)))
    dec = decoupling(samples)
    if dec is not None:
        items.append(("Pa:HR decoupling", f"{dec:+.1f}%"))
    if not items:
        return '<p class="empty">Not enough data.</p>'
    kv = "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>" for k, v in items)
    return (
        f'<dl class="kv">{kv}</dl><p class="note">Computed here from the time series '
        "(time-weighted, not Garmin values). Decoupling compares speed/HR between the first "
        "and second half; it is only meaningful for steady efforts.</p>"
    )


_LAP_HEADERS = (
    ("#", "n"),
    ("Type", ""),
    ("Distance", "n"),
    ("Time", "n"),
    ("Pace / speed", "n"),
    ("Avg HR", "n"),
    ("Max HR", "n"),
    ("Cadence", "n"),
    ("Power", "n"),
    ("Elev +/−", "n"),
)


def _segment_cells(activity_type: str | None, x: dict[str, Any]) -> list[str]:
    elev = ""
    if x["elevation_gain_m"] is not None or x["elevation_loss_m"] is not None:
        elev = f"+{fmt_num(x['elevation_gain_m'])} / −{fmt_num(x['elevation_loss_m'])}"
    return [
        _km(x["distance_m"]),
        fmt_duration(x["duration_s"]),
        fmt_speed(activity_type, x["avg_speed_mps"], x["avg_pace_s_per_km"]),
        fmt_num(x["avg_hr"]),
        fmt_num(x["max_hr"]),
        fmt_num(x["avg_cadence"]),
        fmt_num(x["avg_power"]),
        elev,
    ]


def _table(headers: tuple[tuple[str, str], ...], rows: list[tuple[str, list[str]]]) -> str:
    esc = html.escape
    head = "".join(f'<th class="{c}">{esc(h)}</th>' for h, c in headers)
    body = "".join(
        f'<tr class="{row_cls}">'
        + "".join(
            f'<td class="{c}">{esc(v)}</td>' for v, (_, c) in zip(cells, headers, strict=True)
        )
        + "</tr>"
        for row_cls, cells in rows
    )
    return (
        f'<div class="wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _laps(a: dict[str, Any], laps: list[dict[str, Any]]) -> str:
    if not laps:
        return '<p class="empty">No laps.</p>'
    atype = a["activity_type"]
    has_work = any(lap["step_type"] == "work" for lap in laps)
    pace_mode = is_pace_type(atype)

    def label(lap: dict[str, Any]) -> str:
        return _join(
            f"Lap {lap['sequence']}",
            _STEP_LABEL.get(lap["step_type"], lap["step_type"]),
            _km(lap["distance_m"]),
            fmt_duration(lap["duration_s"]),
            fmt_speed(atype, lap["avg_speed_mps"], lap["avg_pace_s_per_km"]),
            fmt_num(lap["avg_hr"], 0, " bpm"),
        )

    bars = charts.lap_bars(
        laps,
        x_key="distance_m" if any(lap["distance_m"] for lap in laps) else "duration_s",
        value_key="avg_speed_mps",
        label=label,
        highlight=lambda lap: lap["step_type"] == "work" or not has_work,
    )
    legend = (
        '<div class="legend"><span><i style="background:var(--c-pace)"></i>work</span>'
        '<span><i style="background:var(--bar-muted)"></i>warmup / recovery / cooldown / other'
        "</span></div>"
        if has_work
        else ""
    )
    caption = (
        '<p class="note">Bar width: lap distance; height: '
        f"{'pace (faster is taller)' if pace_mode else 'speed'}. Hover a bar for details.</p>"
    )
    rows = [
        (
            "work" if lap["step_type"] == "work" else "",
            [
                str(lap["sequence"]),
                _STEP_LABEL.get(lap["step_type"], lap["step_type"]),
                *_segment_cells(atype, lap),
            ],
        )
        for lap in laps
    ]
    return bars + legend + caption + _table(_LAP_HEADERS, rows)


def _splits(a: dict[str, Any], splits: list[dict[str, Any]]) -> str:
    if not splits:
        return ""
    headers = (("Type", ""), ("Laps", "")) + _LAP_HEADERS[2:]
    rows = []
    for x in splits:
        idx = x["lap_indexes"] or []
        laps = f"{idx[0]}–{idx[-1]}" if len(idx) > 1 else (str(idx[0]) if idx else "")
        rows.append(("", [x["split_type"] or "", laps, *_segment_cells(a["activity_type"], x)]))
    return (
        "<h2>Garmin segments</h2>"
        '<p class="note">Garmin typed splits: executed workout steps (INTERVAL_*) or detected '
        "run/walk segments (RWD_*).</p>" + _table(headers, rows)
    )


def _notes(notes: list[dict[str, Any]]) -> str:
    if not notes:
        return ""
    labels = (
        ("rpe", "RPE"),
        ("fatigue", "Fatigue"),
        ("motivation", "Motivation"),
        ("legs", "Legs"),
        ("soreness_or_pain", "Soreness / pain"),
        ("notes", "Notes"),
    )
    blocks = []
    for n in notes:
        kv = "".join(
            f"<dt>{html.escape(label)}</dt><dd>{html.escape(str(n[key]))}</dd>"
            for key, label in labels
            if n[key] is not None
        )
        blocks.append(f'<dl class="kv">{kv}</dl>')
    return "<h2>My notes</h2>" + '<hr class="sep">'.join(blocks)


def render_activity(a: dict[str, Any], children: dict[str, Any]) -> str:
    esc = html.escape
    start = a["start_time_local"]
    meta = _join(
        str(a["date"] or ""),
        start.strftime("%H:%M") if start else "",
        a["activity_type"] or "",
        a["device"] or "",
        a["gear"] or "",
    )
    title = a["activity_name"] or "Activity"
    pace_mode = is_pace_type(a["activity_type"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Training log</title><style>{_CSS}</style></head>
<body><main>
<a class="back" href="/">← All activities</a>
<h1>{esc(title)}</h1>
<p class="summary">{esc(meta)}</p>
<div class="tiles">{_tiles(a)}</div>
<h2>{"Pace" if pace_mode else "Speed"} &amp; heart rate</h2>
{_charts(a, children["samples"], children["laps"])}
<div class="cols">
<section><h2>Time in HR zones</h2>{_zones(children["zones"])}</section>
<section><h2>Derived</h2>{_derived(children["samples"], pace_mode)}</section>
</div>
<h2>Laps</h2>
{_laps(a, children["laps"])}
{_splits(a, children["splits"])}
{_notes(children["notes"])}
</main></body></html>"""


def render_error(message: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Training log</title>'
        f"<style>{_CSS}</style></head><body><main><h1>Training log</h1>"
        f'<p class="empty">{html.escape(message)}</p></main></body></html>'
    )


# -- server ---------------------------------------------------------------------------


def _parse_date(values: list[str] | None) -> date | None:
    try:
        return date.fromisoformat(values[0]) if values and values[0] else None
    except ValueError:
        return None


def _parse_int(values: list[str] | None, default: int) -> int:
    try:
        return int(values[0]) if values and values[0] else default
    except ValueError:
        return default


def parse_query(params: dict[str, list[str]], types: list[str]) -> ActivityQuery:
    """ActivityQuery from URL parameters; invalid values fall back to defaults."""
    first = lambda key: (params.get(key) or [""])[0]  # noqa: E731
    selected = first("type") or None
    sort = first("sort") if first("sort") in SORTS else DEFAULT_SORT
    direction = first("dir")
    return ActivityQuery(
        activity_type=selected if selected in types else None,
        date_from=_parse_date(params.get("from")),
        date_to=_parse_date(params.get("to")),
        search=first("q").strip()[:200] or None,
        sort=sort,
        desc=SORTS[sort][1] if direction not in ("asc", "desc") else direction == "desc",
        page=_parse_int(params.get("page"), 1),
        per_page=_parse_int(params.get("per"), DEFAULT_PAGE_SIZE),
    )


_ACTIVITY_PATH = re.compile(r"^/activity/(\d{1,18})$")


def make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            url = urlparse(self.path)
            detail = _ACTIVITY_PATH.match(url.path)
            if url.path != "/" and not detail:
                self._send(404, render_error("Not found"))
                return
            q = parse_qs(url.query)
            try:
                con = duckdb.connect(str(db_path), read_only=True)
            except duckdb.Error as e:
                log.warning("Cannot open database: %s", e)
                self._send(
                    503,
                    render_error(
                        "Database is unavailable (a sync may be running). Retry in a moment."
                    ),
                )
                return
            try:
                if detail:
                    aid = int(detail.group(1))
                    activity = get_activity(con, aid)
                    children = activity_children(con, aid) if activity else {}
                else:
                    types = activity_types(con)
                    result = query_activities(con, parse_query(q, types))
            finally:
                con.close()
            if not detail:
                self._send(200, render_page(result, types))
            elif activity is None:
                self._send(404, render_error("Activity not found"))
            else:
                self._send(200, render_activity(activity, children))

        def _send(self, status: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("web: " + fmt, *args)

    return Handler


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(db_path))
    print(f"Serving training log on http://{host}:{port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    finally:
        server.server_close()
