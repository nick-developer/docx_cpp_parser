"""Inline SVG charts, generated with the standard library only.

The report has to open from a file:// URL with no network, so a charting library
loaded from a CDN is not an option, and vendoring one would put ~200 KB of
third-party JavaScript into every wheel *and* every generated report.  The
timeline only needs stacked columns, which is a few dozen lines of arithmetic —
so the charts are emitted as plain ``<svg>`` markup at build time.

The result is a chart that is already drawn when the file opens: no runtime, no
layout thrash on a 5,000-comment report, and it survives being pasted into an
email or printed.

Design notes (these follow deliberate rules, not taste):

* Marks are capped at 24 px so a sparse chart keeps air around its columns.
* Stacked segments are separated by a 2 px *geometric* gap, so the page surface
  shows through and the boundary reads in both light and dark mode.  Nothing is
  outlined — a stroke would add ink that is not data.
* The topmost segment gets a 4 px rounded cap; the baseline stays square.
* Series colours are CSS custom properties, so the light/dark palettes swap in
  one place.  They are categorical slots 1 and 2, which clear the colour-vision
  separation gates in both modes (worst-pair ΔE 24.7 light / 26.8 dark against a
  floor of 8).  Text never wears a series colour — the legend swatch beside it
  carries identity instead.
* Only the tallest column is labelled directly.  A number over every column is
  noise; the axis, the legend and the hover tooltip carry the rest.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from .analytics import TimelineBucket

__all__ = ["column_chart", "escape_text"]

# ─── Geometry ────────────────────────────────────────────────────────────────
_WIDTH = 960
_HEIGHT = 260
_PAD_LEFT = 46
_PAD_RIGHT = 14
_PAD_TOP = 20
_PAD_BOTTOM = 38

_MAX_BAR = 24.0
"""Cap on mark thickness; the rest of the band stays as air."""

_SEGMENT_GAP = 2.0
"""Surface gap between stacked segments — geometry, never a stroke."""

_CAP_RADIUS = 4.0
_MAX_X_LABELS = 9
_TARGET_TICKS = 4


def escape_text(value: str) -> str:
    """Escape a string for use as SVG/HTML text content or an attribute value."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _nice_step(span: float, target: int = _TARGET_TICKS) -> float:
    """Pick a 1/2/5×10ⁿ tick step giving roughly ``target`` gridlines.

    Axis ticks carry every value that is not directly labelled, so they have to
    land on numbers a reader recognises — 0/50/100, never 0/37/74.
    """
    if span <= 0:
        return 1.0
    rough = span / max(target, 1)
    magnitude = 10.0 ** (len(str(int(rough))) - 1) if rough >= 1 else 1.0
    for multiple in (1.0, 2.0, 5.0, 10.0):
        step = multiple * magnitude
        if step >= rough:
            return step
    return 10.0 * magnitude


def _axis_max(peak: int) -> Tuple[float, float]:
    """Return ``(axis_maximum, tick_step)`` for a chart topping out at ``peak``."""
    if peak <= 0:
        return 1.0, 1.0
    step = _nice_step(float(peak))
    top = step
    while top < peak:
        top += step
    return top, step


def _label_indices(count: int, maximum: int = _MAX_X_LABELS) -> List[int]:
    """Choose which x positions get a label, so ticks never collide.

    Always keeps the first and last so the chart's span is readable, and spreads
    the rest evenly between them.
    """
    if count <= 0:
        return []
    if count <= maximum:
        return list(range(count))
    stride = (count - 1) / (maximum - 1)
    chosen = {int(round(i * stride)) for i in range(maximum)}
    chosen.add(0)
    chosen.add(count - 1)
    return sorted(chosen)


def _bar_path(x: float, y: float, width: float, height: float, radius: float) -> str:
    """A column with rounded top corners and a square baseline."""
    if height <= 0:
        return ""
    r = min(radius, width / 2.0, height)
    if r <= 0.5:
        return f"M{x:.2f} {y:.2f}h{width:.2f}v{height:.2f}h{-width:.2f}Z"
    return (
        f"M{x:.2f} {y + r:.2f}"
        f"a{r:.2f} {r:.2f} 0 0 1 {r:.2f} {-r:.2f}"
        f"h{width - 2 * r:.2f}"
        f"a{r:.2f} {r:.2f} 0 0 1 {r:.2f} {r:.2f}"
        f"v{height - r:.2f}"
        f"h{-width:.2f}Z"
    )


def column_chart(
    buckets: Sequence[TimelineBucket],
    chart_id: str,
    caption: str,
    empty_message: str = "No dated comments to plot.",
) -> str:
    """Render a stacked column chart of comment activity as inline SVG.

    Parameters
    ----------
    buckets:
        The timeline series, already gap-filled by
        :func:`~docx_comment_parser.reporting.analytics.build_report_data`.
    chart_id:
        Unique id; used to tie the SVG to its accessible description.
    caption:
        Accessible description of what the chart shows.
    """
    if not buckets:
        return f'<p class="viz-empty">{escape_text(empty_message)}</p>'

    peak = max(b.total for b in buckets)
    axis_top, step = _axis_max(peak)

    plot_width = _WIDTH - _PAD_LEFT - _PAD_RIGHT
    plot_height = _HEIGHT - _PAD_TOP - _PAD_BOTTOM
    band = plot_width / len(buckets)
    bar_width = min(_MAX_BAR, band * 0.72)

    def y_for(value: float) -> float:
        return _PAD_TOP + plot_height * (1.0 - value / axis_top)

    parts: List[str] = []

    # ── Gridlines and y ticks ────────────────────────────────────────────────
    # Hairline, solid, one step off the surface: present enough to read a value
    # against, quiet enough to stay behind the data.
    ticks: List[str] = []
    value = 0.0
    while value <= axis_top + 1e-9:
        y = y_for(value)
        ticks.append(
            f'<line class="viz-grid" x1="{_PAD_LEFT}" y1="{y:.2f}" '
            f'x2="{_WIDTH - _PAD_RIGHT}" y2="{y:.2f}"/>'
            f'<text class="viz-tick" x="{_PAD_LEFT - 8}" y="{y + 4:.2f}" '
            f'text-anchor="end">{int(value):,}</text>'
        )
        value += step
    parts.append("".join(ticks))

    # ── Columns ──────────────────────────────────────────────────────────────
    peak_index = max(range(len(buckets)), key=lambda i: buckets[i].total)
    baseline = y_for(0)
    columns: List[str] = []

    for index, bucket in enumerate(buckets):
        centre = _PAD_LEFT + band * (index + 0.5)
        x = centre - bar_width / 2.0

        resolved_h = plot_height * (bucket.resolved / axis_top)
        open_h = plot_height * (bucket.unresolved / axis_top)

        # The hit target spans the whole band and the full plot height, so the
        # tooltip is reachable even where a column is one pixel tall.
        hit = (
            f'<rect class="viz-hit" x="{_PAD_LEFT + band * index:.2f}" y="{_PAD_TOP}" '
            f'width="{band:.2f}" height="{plot_height}" '
            f'data-label="{escape_text(bucket.label)}" '
            f'data-total="{bucket.total}" data-resolved="{bucket.resolved}" '
            f'data-open="{bucket.unresolved}"/>'
        )

        shapes: List[str] = []
        if bucket.total > 0:
            has_open = open_h > 0
            has_resolved = resolved_h > 0

            if has_resolved:
                # Resolved sits at the baseline. When something is stacked on
                # top, it gives up 2px so the surface shows through, and its cap
                # stays square because it is not the top of the stack.
                height = resolved_h - (_SEGMENT_GAP if has_open else 0.0)
                if height > 0:
                    shapes.append(
                        f'<path class="viz-series-1" d="'
                        f'{_bar_path(x, baseline - height, bar_width, height, 0.0 if has_open else _CAP_RADIUS)}"/>'
                    )
            if has_open:
                top = baseline - resolved_h - open_h
                shapes.append(
                    f'<path class="viz-series-2" d="'
                    f'{_bar_path(x, top, bar_width, open_h, _CAP_RADIUS)}"/>'
                )

            # Label only the tallest column: a value on every cap goes unread.
            if index == peak_index:
                cap = baseline - resolved_h - open_h
                shapes.append(
                    f'<text class="viz-peak" x="{centre:.2f}" y="{cap - 7:.2f}" '
                    f'text-anchor="middle">{bucket.total:,}</text>'
                )

        columns.append(f'<g class="viz-col">{"".join(shapes)}{hit}</g>')

    parts.append("".join(columns))

    # ── Baseline and x labels ────────────────────────────────────────────────
    parts.append(
        f'<line class="viz-axis" x1="{_PAD_LEFT}" y1="{baseline:.2f}" '
        f'x2="{_WIDTH - _PAD_RIGHT}" y2="{baseline:.2f}"/>'
    )
    for index in _label_indices(len(buckets)):
        centre = _PAD_LEFT + band * (index + 0.5)
        parts.append(
            f'<text class="viz-tick" x="{centre:.2f}" y="{baseline + 18:.2f}" '
            f'text-anchor="middle">{escape_text(buckets[index].label)}</text>'
        )

    desc_id = f"{chart_id}-desc"
    return (
        f'<svg class="viz" viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" '
        f'preserveAspectRatio="xMidYMid meet" aria-labelledby="{desc_id}">'
        f'<desc id="{desc_id}">{escape_text(caption)}</desc>'
        f'{"".join(parts)}</svg>'
    )
