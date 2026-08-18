"""Small dependency-free SVG chart renderers.

Petrol Khata runs fully offline, so charts are generated as plain server-side
SVG (no Chart.js / CDN) and returned as HTML-safe strings for templates
to render with `|safe`. All interpolated values are numbers or
datetime-formatted strings - never raw user input - so this is safe to
mark trusted.

Phase 2 adds optional hover/keyboard interactivity (tooltip + crosshair) to
line_chart/stacked_bar_chart/bar_chart, gated behind an `interactive=False`
parameter that defaults OFF. Existing callers (reports_trends() in app.py)
call these with their original positional arguments and never pass
`interactive`, so their output is byte-for-byte the same as before this
phase - the new markup (hit-target rects, crosshair line, data-* readout
attributes) only appears when a caller opts in. static/dashboard.js is the
one shared script that binds to any `.chart-interactive` element; charts.py
itself stays framework-free and knows nothing about that script.
"""

import math

PAD_LEFT = 46
PAD_RIGHT = 16
PAD_TOP = 16
PAD_BOTTOM = 34


def _x_positions(n, width):
    plot_w = width - PAD_LEFT - PAD_RIGHT
    if n <= 1:
        return [PAD_LEFT + plot_w / 2] if n == 1 else []
    step = plot_w / (n - 1)
    return [PAD_LEFT + i * step for i in range(n)]


def _y_scale(values, height, y_max_override=None):
    plot_h = height - PAD_TOP - PAD_BOTTOM
    lo = 0
    hi = y_max_override if y_max_override is not None else (max(values) if values else 1)
    if hi <= 0:
        hi = 1

    def to_y(v):
        return PAD_TOP + plot_h - (v / hi) * plot_h

    return to_y, lo, hi


def _label_indices(n, max_labels=7):
    if n <= max_labels:
        return list(range(n))
    step = max(1, round(n / max_labels))
    return list(range(0, n, step))


def _grid_and_axis(width, height, hi, x_positions, labels):
    plot_h = height - PAD_TOP - PAD_BOTTOM
    parts = []
    for frac in (0, 0.5, 1):
        y = PAD_TOP + plot_h - frac * plot_h
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{y:.1f}" x2="{width - PAD_RIGHT}" y2="{y:.1f}" '
            f'stroke="var(--border)" stroke-width="1" />'
        )
        parts.append(
            f'<text x="{PAD_LEFT - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'class="chart-axis-label">{hi * frac:,.0f}</text>'
        )
    for i in _label_indices(len(labels)):
        parts.append(
            f'<text x="{x_positions[i]:.1f}" y="{height - 10}" text-anchor="middle" '
            f'class="chart-axis-label">{labels[i]}</text>'
        )
    return "".join(parts)


def _hit_targets_and_crosshair(labels, x_pos, per_index_values, width, height):
    """Shared interactivity markup for line_chart/stacked_bar_chart: one
    invisible full-height hit rect per x-index (so hovering anywhere in
    that column works, not just exactly on a line/bar - see the module
    docstring), plus a single crosshair line JS repositions on hover/
    focus/keyboard-move rather than one crosshair per index.

    per_index_values: list (len == len(labels)) of already-formatted
    "1234.5|678.9"-style strings, one per x-index, matching the series
    order the caller's legend was built in - so the shared JS tooltip
    module never needs its own copy of what each series means, only the
    legend's existing name/colour and this rect's own values.

    Hit rects are emitted LAST (after everything else, including the
    crosshair line) so they paint on top and reliably receive pointer
    events despite being fully transparent."""
    plot_h = height - PAD_TOP - PAD_BOTTOM
    n = len(labels)
    col_w = (width - PAD_LEFT - PAD_RIGHT) / max(n, 1)
    parts = [
        f'<line class="chart-crosshair" x1="{PAD_LEFT}" y1="{PAD_TOP}" '
        f'x2="{PAD_LEFT}" y2="{PAD_TOP + plot_h}" />'
    ]
    for i in range(n):
        x = x_pos[i] - col_w / 2 if n > 1 else PAD_LEFT
        parts.append(
            f'<rect class="chart-hit" data-index="{i}" data-label="{labels[i]}" '
            f'data-values="{per_index_values[i]}" data-x="{x_pos[i]:.1f}" '
            f'x="{x:.1f}" y="{PAD_TOP}" width="{col_w:.1f}" height="{plot_h:.1f}" '
            f'fill="transparent" />'
        )
    return "".join(parts)


def sparkline(values, width=120, height=30, color="var(--accent)"):
    """Tiny trend line for a stat card - no axes, no labels, no grid.

    A flat series (every value equal, including all-zero) would divide by
    zero scaling to the plot height - guarded by drawing a flat line
    across the vertical centre instead, same spirit as line_chart's own
    y_max fallback for an empty/zero series. A single-point series is
    drawn as a lone centred dot (n<=1 has no line to speak of).

    aria-hidden: the sparkline is purely a decorative echo of the number
    already printed as text right above it on the stat card, not a
    second source of information - a screen reader gets nothing new from
    it and shouldn't be made to read an SVG's fallback description."""
    n = len(values)
    if n == 0:
        return ""
    pad = 3.0
    plot_w = width - 2 * pad
    plot_h = height - 2 * pad

    if n == 1:
        xs = [width / 2]
    else:
        step = plot_w / (n - 1)
        xs = [pad + i * step for i in range(n)]

    lo, hi = min(values), max(values)
    rng = hi - lo
    if rng <= 0:
        ys = [height / 2] * n
    else:
        ys = [pad + plot_h - (v - lo) / rng * plot_h for v in values]

    last_x, last_y = xs[-1], ys[-1]
    svg = [
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'class="sparkline" preserveAspectRatio="none" aria-hidden="true" focusable="false">'
    ]
    if n > 1:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        svg.append(
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round" />'
        )
    svg.append(f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.5" fill="{color}" />')
    svg.append("</svg>")
    return "".join(svg)


def line_chart(series, labels, colors, names, width=640, height=240, interactive=False, ghost_indices=()):
    """series: list of value-lists (each same length as labels).

    ghost_indices: indices into `series` that render muted/dashed and
    HIDDEN BY DEFAULT (inline style, not a CSS class - see
    static/dashboard.js) - the Dashboard's compare-to-previous-period
    overlay on the profit chart. A checkbox toggles them; with no JS the
    checkbox is inert and the ghost series simply never appears, which is
    the documented no-JS fallback for that one control (Phase 2 spec
    section 8) - every OTHER series on every OTHER chart is unaffected
    and renders unconditionally as before."""
    all_values = [v for s in series for v in s]
    y_max = max(all_values) * 1.15 if all_values else 1
    x_pos = _x_positions(len(labels), width)
    to_y, _, hi = _y_scale(all_values, height, y_max)

    svg_class = "chart-svg chart-interactive" if interactive else "chart-svg"
    if interactive:
        svg_open = (
            f'<svg viewBox="0 0 {width} {height}" class="{svg_class}" preserveAspectRatio="none" '
            f'role="group" tabindex="0" aria-label="Interactive chart. Use arrow keys to move through data points.">'
        )
    else:
        svg_open = f'<svg viewBox="0 0 {width} {height}" class="{svg_class}" preserveAspectRatio="none" role="img">'
    svg = [svg_open]
    svg.append(_grid_and_axis(width, height, hi, x_pos, labels))

    for idx, (s, color) in enumerate(zip(series, colors)):
        if not s:
            continue
        is_ghost = idx in ghost_indices
        ghost_attrs = ' class="chart-line-ghost" style="display:none"' if is_ghost else ""
        dash = ' stroke-dasharray="6,4"' if is_ghost else ""
        pts = " ".join(f"{x_pos[i]:.1f},{to_y(v):.1f}" for i, v in enumerate(s))
        svg.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5"{dash} '
                    f'stroke-linejoin="round" stroke-linecap="round"{ghost_attrs} />')
        for i, v in enumerate(s):
            svg.append(f'<circle cx="{x_pos[i]:.1f}" cy="{to_y(v):.1f}" r="3" fill="{color}"{ghost_attrs} />')

    if interactive:
        per_index_values = [
            "|".join(f"{s[i]:.2f}" if i < len(s) else "" for s in series) for i in range(len(labels))
        ]
        svg.append(_hit_targets_and_crosshair(labels, x_pos, per_index_values, width, height))

    svg.append("</svg>")
    legend_items = []
    for i, (c, n) in enumerate(zip(colors, names)):
        is_ghost = i in ghost_indices
        ghost_attrs = ' style="display:none"' if is_ghost else ""
        cls = "chart-legend-item chart-legend-ghost" if is_ghost else "chart-legend-item"
        legend_items.append(
            f'<span class="{cls}" data-name="{n}" data-color="{c}"{ghost_attrs}>'
            f'<span class="dot" style="background:{c}"></span>{n}</span>'
        )
    legend = "".join(legend_items)
    return f'<div class="chart-legend">{legend}</div>' + "".join(svg)


def stacked_bar_chart(series_a, series_b, labels, colors, names, width=640, height=240, interactive=False):
    totals = [a + b for a, b in zip(series_a, series_b)]
    y_max = max(totals) * 1.15 if totals else 1
    x_pos = _x_positions(len(labels), width)
    to_y, _, hi = _y_scale(totals, height, y_max)
    plot_h = height - PAD_TOP - PAD_BOTTOM

    n = len(labels)
    bar_w = max(4, min(34, (width - PAD_LEFT - PAD_RIGHT) / max(n, 1) * 0.55))

    svg_class = "chart-svg chart-interactive" if interactive else "chart-svg"
    if interactive:
        svg_open = (
            f'<svg viewBox="0 0 {width} {height}" class="{svg_class}" preserveAspectRatio="none" '
            f'role="group" tabindex="0" aria-label="Interactive chart. Use arrow keys to move through data points.">'
        )
    else:
        svg_open = f'<svg viewBox="0 0 {width} {height}" class="{svg_class}" preserveAspectRatio="none" role="img">'
    svg = [svg_open]
    svg.append(_grid_and_axis(width, height, hi, x_pos, labels))

    for i in range(n):
        x = x_pos[i] - bar_w / 2
        a_val, b_val = series_a[i], series_b[i]
        a_h = (a_val / hi) * plot_h if hi else 0
        b_h = (b_val / hi) * plot_h if hi else 0
        y_base = PAD_TOP + plot_h
        svg.append(f'<rect x="{x:.1f}" y="{y_base - a_h:.1f}" width="{bar_w:.1f}" height="{a_h:.1f}" '
                    f'fill="{colors[0]}" rx="2" />')
        svg.append(f'<rect x="{x:.1f}" y="{y_base - a_h - b_h:.1f}" width="{bar_w:.1f}" height="{b_h:.1f}" '
                    f'fill="{colors[1]}" rx="2" />')

    if interactive:
        per_index_values = [f"{series_a[i]:.2f}|{series_b[i]:.2f}" for i in range(n)]
        svg.append(_hit_targets_and_crosshair(labels, x_pos, per_index_values, width, height))

    svg.append("</svg>")
    legend = "".join(
        f'<span class="chart-legend-item" data-name="{n}" data-color="{c}">'
        f'<span class="dot" style="background:{c}"></span>{n}</span>'
        for c, n in zip(colors, names)
    )
    return f'<div class="chart-legend">{legend}</div>' + "".join(svg)


def donut_chart(segments, width=240, height=240, thickness=34):
    """segments: list of {"label", "amount", "color"}, already sorted by
    the caller. Renders a ring built from stacked <circle> strokes
    (stroke-dasharray/stroke-dashoffset around a shared radius) rather
    than <path> arc commands - the simplest correct approach: no
    arc-angle trig, no rounding drift accumulating across segments, and a
    single 100% segment falls out of the same formula as any other split
    (dasharray "circumference 0" IS a full circle, no special case
    needed).

    The only genuine edge case is an all-zero input, where every segment
    would divide by a zero total - guarded explicitly below with a plain
    grey ring instead.

    Each segment gets a <title> child so hovering shows its label/amount
    natively without any script - the accessible fallback Phase 2's own
    richer tooltip will sit on top of, not replace."""
    cx, cy = width / 2, height / 2
    radius = (min(width, height) - thickness) / 2
    circumference = 2 * math.pi * radius

    total = sum(max(s["amount"], 0) for s in segments)

    svg = [f'<svg viewBox="0 0 {width} {height}" class="chart-svg donut-svg" role="img">']

    if total <= 0:
        svg.append(
            f'<circle cx="{cx}" cy="{cy}" r="{radius:.2f}" fill="none" '
            f'stroke="var(--border)" stroke-width="{thickness}" />'
        )
    else:
        offset = 0.0
        for seg in segments:
            amount = max(seg["amount"], 0)
            if amount <= 0:
                continue
            dash = (amount / total) * circumference
            gap = circumference - dash
            svg.append(
                f'<circle cx="{cx}" cy="{cy}" r="{radius:.2f}" fill="none" '
                f'stroke="{seg["color"]}" stroke-width="{thickness}" '
                f'stroke-dasharray="{dash:.3f} {gap:.3f}" '
                f'stroke-dashoffset="{-offset:.3f}" '
                f'transform="rotate(-90 {cx} {cy})">'
                f'<title>{seg["label"]}: Rs {amount:,.2f}</title>'
                f"</circle>"
            )
            offset += dash

    svg.append(f'<text x="{cx}" y="{cy - 6:.1f}" text-anchor="middle" class="donut-total-label">Total</text>')
    svg.append(f'<text x="{cx}" y="{cy + 16:.1f}" text-anchor="middle" class="donut-total-value">Rs {total:,.0f}</text>')
    svg.append("</svg>")

    legend = "".join(
        f'<span class="chart-legend-item"><span class="dot" style="background:{s["color"]}"></span>'
        f'{s["label"]} (Rs {s["amount"]:,.2f})</span>'
        for s in segments
    )
    return f'<div class="chart-legend">{legend}</div>' + "".join(svg)


def bar_chart(values, labels, color, width=640, height=200, interactive=False):
    y_max = max(values) * 1.15 if values else 1
    x_pos = _x_positions(len(labels), width)
    to_y, _, hi = _y_scale(values, height, y_max)
    plot_h = height - PAD_TOP - PAD_BOTTOM
    n = len(labels)
    bar_w = max(4, min(34, (width - PAD_LEFT - PAD_RIGHT) / max(n, 1) * 0.55))

    svg_class = "chart-svg chart-interactive" if interactive else "chart-svg"
    if interactive:
        svg_open = (
            f'<svg viewBox="0 0 {width} {height}" class="{svg_class}" preserveAspectRatio="none" '
            f'role="group" tabindex="0" aria-label="Interactive chart. Use arrow keys to move through data points.">'
        )
    else:
        svg_open = f'<svg viewBox="0 0 {width} {height}" class="{svg_class}" preserveAspectRatio="none" role="img">'
    svg = [svg_open]
    svg.append(_grid_and_axis(width, height, hi, x_pos, labels))
    y_base = PAD_TOP + plot_h
    for i, v in enumerate(values):
        h = (v / hi) * plot_h if hi else 0
        x = x_pos[i] - bar_w / 2
        svg.append(f'<rect x="{x:.1f}" y="{y_base - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                    f'fill="{color}" rx="2" />')

    if interactive:
        per_index_values = [f"{v:.2f}" for v in values]
        svg.append(_hit_targets_and_crosshair(labels, x_pos, per_index_values, width, height))

    svg.append("</svg>")
    return "".join(svg)
