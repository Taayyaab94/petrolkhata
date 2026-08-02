"""Small dependency-free SVG chart renderers.

Munchi runs fully offline, so charts are generated as plain server-side
SVG (no Chart.js / CDN) and returned as HTML-safe strings for templates
to render with `|safe`. All interpolated values are numbers or
datetime-formatted strings - never raw user input - so this is safe to
mark trusted.
"""

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


def line_chart(series, labels, colors, names, width=640, height=240):
    """series: list of value-lists (each same length as labels)."""
    all_values = [v for s in series for v in s]
    y_max = max(all_values) * 1.15 if all_values else 1
    x_pos = _x_positions(len(labels), width)
    to_y, _, hi = _y_scale(all_values, height, y_max)

    svg = [f'<svg viewBox="0 0 {width} {height}" class="chart-svg" preserveAspectRatio="none" role="img">']
    svg.append(_grid_and_axis(width, height, hi, x_pos, labels))

    for s, color in zip(series, colors):
        if not s:
            continue
        pts = " ".join(f"{x_pos[i]:.1f},{to_y(v):.1f}" for i, v in enumerate(s))
        svg.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5" '
                    f'stroke-linejoin="round" stroke-linecap="round" />')
        for i, v in enumerate(s):
            svg.append(f'<circle cx="{x_pos[i]:.1f}" cy="{to_y(v):.1f}" r="3" fill="{color}" />')

    svg.append("</svg>")
    legend = "".join(
        f'<span class="chart-legend-item"><span class="dot" style="background:{c}"></span>{n}</span>'
        for c, n in zip(colors, names)
    )
    return f'<div class="chart-legend">{legend}</div>' + "".join(svg)


def stacked_bar_chart(series_a, series_b, labels, colors, names, width=640, height=240):
    totals = [a + b for a, b in zip(series_a, series_b)]
    y_max = max(totals) * 1.15 if totals else 1
    x_pos = _x_positions(len(labels), width)
    to_y, _, hi = _y_scale(totals, height, y_max)
    plot_h = height - PAD_TOP - PAD_BOTTOM

    n = len(labels)
    bar_w = max(4, min(34, (width - PAD_LEFT - PAD_RIGHT) / max(n, 1) * 0.55))

    svg = [f'<svg viewBox="0 0 {width} {height}" class="chart-svg" preserveAspectRatio="none" role="img">']
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

    svg.append("</svg>")
    legend = "".join(
        f'<span class="chart-legend-item"><span class="dot" style="background:{c}"></span>{n}</span>'
        for c, n in zip(colors, names)
    )
    return f'<div class="chart-legend">{legend}</div>' + "".join(svg)


def bar_chart(values, labels, color, width=640, height=200):
    y_max = max(values) * 1.15 if values else 1
    x_pos = _x_positions(len(labels), width)
    to_y, _, hi = _y_scale(values, height, y_max)
    plot_h = height - PAD_TOP - PAD_BOTTOM
    n = len(labels)
    bar_w = max(4, min(34, (width - PAD_LEFT - PAD_RIGHT) / max(n, 1) * 0.55))

    svg = [f'<svg viewBox="0 0 {width} {height}" class="chart-svg" preserveAspectRatio="none" role="img">']
    svg.append(_grid_and_axis(width, height, hi, x_pos, labels))
    y_base = PAD_TOP + plot_h
    for i, v in enumerate(values):
        h = (v / hi) * plot_h if hi else 0
        x = x_pos[i] - bar_w / 2
        svg.append(f'<rect x="{x:.1f}" y="{y_base - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                    f'fill="{color}" rx="2" />')
    svg.append("</svg>")
    return "".join(svg)
