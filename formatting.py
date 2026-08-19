"""One shared number-display rule for the whole app: comma thousands
separators always; two decimal places only when the value actually has a
non-zero fractional part (after rounding to 2dp) - a whole number never
shows a trailing ".00". Used by the Jinja filter (templates), the PDF/Excel
exports, and the SVG chart labels, so every number on every page follows
the same rule from one place instead of each being formatted by hand.

Dependency-free on purpose (no Flask/app import) - exports.py and charts.py
already avoid depending on app.py, and this module has to be importable by
both of them plus app.py itself without a circular import.
"""


def format_number(value):
    """value can be None, an int, a float, or a numeric string. None and
    anything that can't be read as a number pass through as "-" for None,
    or as their original string otherwise (rather than crashing a page) -
    a handful of callers pass already-formatted strings through code paths
    shared with real numbers."""
    if value is None:
        return "-"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value

    rounded = round(num, 2)
    if rounded == int(rounded):
        return f"{int(rounded):,}"
    return f"{rounded:,.2f}"
