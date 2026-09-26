"""Minimal local web view: a list of activities with basic metrics.

Standard library only (``http.server``). Read-only: every request opens a
short-lived read-only DuckDB connection, so the view never modifies data.
Intended for localhost; it has no authentication.
"""

from __future__ import annotations

import html
import logging
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

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


def list_activities(
    con: duckdb.DuckDBPyConnection,
    activity_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Any]]:
    """Activities matching the filters, newest first."""
    cur = con.execute(
        f"SELECT {_COLUMNS} FROM activity "
        "WHERE (? IS NULL OR activity_type = ?) "
        "AND (? IS NULL OR date >= ?) AND (? IS NULL OR date <= ?) "
        "ORDER BY start_time_local DESC NULLS LAST, activity_id DESC",
        [activity_type, activity_type, date_from, date_from, date_to, date_to],
    )
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


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
"""

_HEADERS = (
    ("Date", ""),
    ("Time", ""),
    ("Type", ""),
    ("Name", ""),
    ("Distance", "n"),
    ("Duration", "n"),
    ("Pace / speed", "n"),
    ("Avg HR", "n"),
    ("Max HR", "n"),
    ("Cadence", "n"),
    ("Elev +", "n"),
    ("TE aer/ana", "n"),
    ("Load", "n"),
    ("RPE", "n"),
    ("Feel", ""),
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


def render_page(
    activities: list[dict[str, Any]],
    types: list[str],
    selected_type: str | None,
    date_from: date | None,
    date_to: date | None,
) -> str:
    esc = html.escape
    total_km = sum(a["distance_m"] or 0 for a in activities) / 1000
    total_s = sum(a["duration_s"] or 0 for a in activities)
    count = f"{len(activities)} activit{'y' if len(activities) == 1 else 'ies'}"
    options = ['<option value="">all</option>'] + [
        f'<option value="{esc(t)}"{" selected" if t == selected_type else ""}>{esc(t)}</option>'
        for t in types
    ]
    head = "".join(f'<th class="{c}">{esc(h)}</th>' for h, c in _HEADERS)
    body = "".join(
        "<tr>" + "".join(f'<td class="{c}">{esc(v)}</td>' for v, c in _cells(a)) + "</tr>"
        for a in activities
    )
    table = (
        f'<div class="wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
        if activities
        else '<p class="empty">No activities match. Run <code>garmin-data sync</code> first?</p>'
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Training log</title><style>{_CSS}</style></head>
<body><main>
<h1>Training log</h1>
<p class="summary">{count} · {total_km:.1f} km · {fmt_duration(total_s)}</p>
<form method="get">
  <label>Type<select name="type">{"".join(options)}</select></label>
  <label>From<input type="date" name="from" value="{esc(str(date_from or ""))}"></label>
  <label>To<input type="date" name="to" value="{esc(str(date_to or ""))}"></label>
  <button type="submit">Filter</button> <a href="/">Reset</a>
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
                selected = (q.get("type") or [""])[0] or None
                selected = selected if selected in types else None
                d_from, d_to = _parse_date(q.get("from")), _parse_date(q.get("to"))
                rows = list_activities(con, selected, d_from, d_to)
            finally:
                con.close()
            self._send(200, render_page(rows, types, selected, d_from, d_to))

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
