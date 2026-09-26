"""Server-rendered SVG charts for the web view (standard library only).

Charts are drawn in a fixed ``viewBox`` and scale with the page width. Colors
come from CSS custom properties defined by the page, so light/dark mode is
handled in CSS. Hover (crosshair + readout) is a small script on the page that
reads the ``data-*`` attributes set here.
"""

from __future__ import annotations

import html
import math
from collections.abc import Callable, Sequence

W = 1000  # viewBox width
PAD_L, PAD_R, PAD_T, PAD_B = 52, 12, 10, 24


def _nice_step(span: float, target: int) -> float:
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def time_ticks(x_max: float) -> list[float]:
    """Tick positions (seconds) at round minute intervals."""
    for step in (60, 120, 300, 600, 900, 1200, 1800, 3600, 7200):
        if x_max / step <= 8:
            break
    return [i * step for i in range(int(x_max // step) + 1)]


def fmt_clock(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def percentile(values: Sequence[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def line_chart(
    *,
    name: str,
    title: str,
    xs: Sequence[float],
    ys: Sequence[float | None],
    color: str,
    y_fmt: Callable[[float], str],
    y_domain: tuple[float, float],
    invert: bool = False,
    area: bool = False,
    bands: Sequence[tuple[float, float]] = (),
    y_step: float | None = None,
    height: int = 150,
) -> str:
    """A single-series line chart over time (x in seconds).

    ``None`` values break the line. Values outside ``y_domain`` are clamped.
    ``invert`` puts low values at the top (pace: faster is up). ``bands`` are
    shaded x ranges (e.g. work intervals). ``y_step`` overrides the tick interval.
    """
    x_max = max(xs[-1] if xs else 0.0, 1.0)
    lo, hi = y_domain
    if hi <= lo:
        lo, hi = lo - 1, hi + 1
    plot_w, plot_h = W - PAD_L - PAD_R, height - PAD_T - PAD_B

    def px(x: float) -> float:
        return PAD_L + x / x_max * plot_w

    def py(y: float) -> float:
        f = (min(max(y, lo), hi) - lo) / (hi - lo)
        return PAD_T + (f if invert else 1 - f) * plot_h

    parts: list[str] = []
    for b0, b1 in bands:
        x0, x1 = px(max(b0, 0)), px(min(b1, x_max))
        if x1 > x0:
            parts.append(
                f'<rect class="band" x="{x0:.1f}" y="{PAD_T}" width="{x1 - x0:.1f}" '
                f'height="{plot_h}"/>'
            )
    step = y_step or _nice_step(hi - lo, 3)
    tick = math.ceil(lo / step) * step
    while tick <= hi + 1e-9:
        y = py(tick)
        parts.append(
            f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">'
            f"{html.escape(y_fmt(tick))}</text>"
        )
        tick += step
    base = PAD_T + plot_h
    for t in time_ticks(x_max):
        parts.append(
            f'<text class="tick" x="{px(t):.1f}" y="{base + 16}" text-anchor="middle">'
            f"{fmt_clock(t)}</text>"
        )
    parts.append(f'<line class="axis" x1="{PAD_L}" x2="{W - PAD_R}" y1="{base}" y2="{base}"/>')

    segments: list[list[tuple[float, float]]] = [[]]
    for x, y in zip(xs, ys, strict=True):
        if y is None:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append((px(x), py(y)))
    for seg in segments:
        if len(seg) < 2:
            continue
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
        if area:
            parts.append(
                f'<polygon class="area" style="fill:var({color})" '
                f'points="{seg[0][0]:.1f},{base} {pts} {seg[-1][0]:.1f},{base}"/>'
            )
        parts.append(f'<polyline class="line" style="stroke:var({color})" points="{pts}"/>')

    parts.append(f'<line class="xh" x1="0" x2="0" y1="{PAD_T}" y2="{base}" visibility="hidden"/>')
    return (
        f'<figure class="chart"><figcaption>{html.escape(title)}</figcaption>'
        f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="{html.escape(title)}" '
        f'data-chart="{html.escape(name)}" data-xmax="{x_max}" data-padl="{PAD_L}" '
        f'data-padr="{PAD_R}">{"".join(parts)}</svg></figure>'
    )


def lap_bars(
    laps: Sequence[dict],
    *,
    x_key: str,
    value_key: str,
    label: Callable[[dict], str],
    highlight: Callable[[dict], bool],
    height: int = 120,
) -> str:
    """Bars per lap: width proportional to ``x_key`` (distance or time),
    height to ``value_key`` (speed), highlighted laps in the accent color."""
    usable = [lap for lap in laps if (lap.get(x_key) or 0) > 0]
    total = sum(lap[x_key] for lap in usable)
    values = [lap[value_key] for lap in usable if lap.get(value_key)]
    if not usable or not total or not values:
        return ""
    v_max = max(values)
    v_min = min(0.6 * min(values), v_max * 0.5)
    plot_w, plot_h = W - PAD_L - PAD_R, height - PAD_T - 6
    base = PAD_T + plot_h
    parts = [f'<line class="axis" x1="{PAD_L}" x2="{W - PAD_R}" y1="{base}" y2="{base}"/>']
    x = float(PAD_L)
    for lap in usable:
        w = lap[x_key] / total * plot_w
        v = lap.get(value_key)
        if v:
            h = max(2.0, (v - v_min) / (v_max - v_min or 1) * plot_h)
            cls = "bar hi" if highlight(lap) else "bar"
            bw = max(w - 2, 1.0)  # 2px surface gap between neighbours
            r = min(4.0, bw / 2)
            parts.append(
                f'<path class="{cls}" tabindex="0" d="M{x:.1f},{base} v{-(h - r):.1f} '
                f"q0,{-r:.1f} {r:.1f},{-r:.1f} h{bw - 2 * r:.1f} q{r:.1f},0 {r:.1f},{r:.1f} "
                f'v{h - r:.1f} z"><title>{html.escape(label(lap))}</title></path>'
            )
        x += w
    return (
        f'<svg class="laps" viewBox="0 0 {W} {height}" role="img" '
        f'aria-label="Lap pace">{"".join(parts)}</svg>'
    )
