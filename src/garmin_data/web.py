"""Minimal local web view: a list of activities with basic metrics.

Standard library only (``http.server``). Read-only: every request opens a
short-lived read-only DuckDB connection, so the view never modifies data.
Intended for localhost; it has no authentication.
"""

from __future__ import annotations

import html
import logging
import math
from dataclasses import dataclass, replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import duckdb

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


def fmt_speed(activity_type: str | None, speed_mps: float | None, pace: float | None) -> str:
    if activity_type and any(t in activity_type for t in _PACE_TYPES):
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
        --accent:#2f6fde; --row:#f7f7f5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161618; --fg:#ececee; --muted:#9a9aa2; --line:#2c2c30; --head:#1f1f23;
          --accent:#7aa7ff; --row:#1b1b1e; }
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
        (a["activity_name"] or "", "name"),
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
    body = "".join(
        "<tr>" + "".join(f'<td class="{c}">{esc(v)}</td>' for v, c in _cells(a)) + "</tr>"
        for a in result.rows
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


def make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            url = urlparse(self.path)
            if url.path != "/":
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
                types = activity_types(con)
                result = query_activities(con, parse_query(q, types))
            finally:
                con.close()
            self._send(200, render_page(result, types))

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
