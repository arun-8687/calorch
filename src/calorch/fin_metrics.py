"""Pure, None-safe derived financial-metric helpers.

Operates on two shapes produced elsewhere in `calorch`:

  * ``history`` — a dict shaped ``{"quarterly": [...]}`` (plus a few
    metadata keys) as returned by
    ``EdgarToolsClient.fundamentals_history`` /
    ``FundamentalsProvider.fundamentals_history``. ``quarterly`` is a list
    of per-quarter dicts, **newest quarter first**.
  * ``funds`` — a dict shaped like ``latest_fundamentals()`` output (single
    latest-quarter snapshot).

No imports beyond stdlib. Every function is None-safe: missing keys,
missing quarters, non-numeric values, or empty history never raise — they
degrade to ``None`` (or an empty/partial result for the list/dict-returning
functions).
"""
from __future__ import annotations

from typing import Any

# Approximate days in a fiscal quarter, used for day-sales/day-payables style
# working-capital ratios (dso/dio/dpo) below.
_DAYS_PER_QUARTER = 91


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _quarterly(history: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not history:
        return []
    q = history.get("quarterly")
    return q if isinstance(q, list) else []


def _num(value: Any) -> float | None:
    """Coerce to float, or None for missing/NaN/non-numeric values."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:
        return None
    return float(value)


def _yoy_at(full: list[dict[str, Any]], i: int, key: str) -> float | None:
    """% change of `full[i][key]` vs `full[i+4][key]` (None if either missing/base<=0)."""
    if i < 0 or i + 4 >= len(full):
        return None
    newest = _num(full[i].get(key))
    base = _num(full[i + 4].get(key))
    if newest is None or base is None or base <= 0:
        return None
    return round((newest - base) / base * 100, 1)


# ---------------------------------------------------------------------------
# Growth
# ---------------------------------------------------------------------------
def yoy(history: dict[str, Any] | None, key: str) -> float | None:
    """% change of the newest quarter vs the quarter 4 back (year-over-year)."""
    return _yoy_at(_quarterly(history), 0, key)


def qoq(history: dict[str, Any] | None, key: str) -> float | None:
    """% change of the newest quarter vs the prior quarter (quarter-over-quarter)."""
    q = _quarterly(history)
    if len(q) < 2:
        return None
    newest = _num(q[0].get(key))
    base = _num(q[1].get(key))
    if newest is None or base is None or base <= 0:
        return None
    return round((newest - base) / base * 100, 1)


def ttm(history: dict[str, Any] | None, key: str) -> float | None:
    """Sum of the newest 4 quarters. None unless all 4 are present."""
    q = _quarterly(history)
    if len(q) < 4:
        return None
    vals = [_num(q[i].get(key)) for i in range(4)]
    if any(v is None for v in vals):
        return None
    return sum(vals)  # type: ignore[return-value]


def margin_delta(history: dict[str, Any] | None, key: str) -> float | None:
    """Percentage-point change of a margin key vs the prior-year quarter."""
    q = _quarterly(history)
    if len(q) < 5:
        return None
    newest = _num(q[0].get(key))
    base = _num(q[4].get(key))
    if newest is None or base is None:
        return None
    return round(newest - base, 1)


# ---------------------------------------------------------------------------
# Working-capital / channel-check ratios (single latest-quarter snapshot)
# ---------------------------------------------------------------------------
def _cogs(funds: dict[str, Any] | None) -> float | None:
    """cost_of_revenue if present, else revenue - gross_profit."""
    if not funds:
        return None
    cogs = _num(funds.get("cost_of_revenue"))
    if cogs is not None:
        return cogs
    rev = _num(funds.get("revenue"))
    gp = _num(funds.get("gross_profit"))
    if rev is not None and gp is not None:
        return rev - gp
    return None


def dso(funds: dict[str, Any] | None) -> float | None:
    """Days sales outstanding: receivables / revenue * ~1 quarter."""
    if not funds:
        return None
    recv = _num(funds.get("receivables"))
    rev = _num(funds.get("revenue"))
    if recv is None or not rev:
        return None
    return round(recv / rev * _DAYS_PER_QUARTER, 1)


def dio(funds: dict[str, Any] | None) -> float | None:
    """Days inventory outstanding: inventory / cogs * ~1 quarter."""
    if not funds:
        return None
    inv = _num(funds.get("inventory"))
    cogs = _cogs(funds)
    if inv is None or not cogs:
        return None
    return round(inv / cogs * _DAYS_PER_QUARTER, 1)


def dpo(funds: dict[str, Any] | None) -> float | None:
    """Days payables outstanding: accounts_payable / cogs * ~1 quarter."""
    if not funds:
        return None
    ap = _num(funds.get("accounts_payable"))
    cogs = _cogs(funds)
    if ap is None or not cogs:
        return None
    return round(ap / cogs * _DAYS_PER_QUARTER, 1)


def ccc(funds: dict[str, Any] | None) -> float | None:
    """Cash conversion cycle: dso + dio - dpo. None unless all three resolve."""
    d_so = dso(funds)
    d_io = dio(funds)
    d_po = dpo(funds)
    if d_so is None or d_io is None or d_po is None:
        return None
    return round(d_so + d_io - d_po, 1)


# ---------------------------------------------------------------------------
# Capital returns
# ---------------------------------------------------------------------------
def capital_returns(history: dict[str, Any] | None) -> dict[str, float | None]:
    """Latest-quarter + TTM buybacks / dividends / FCF, all None-safe."""
    q = _quarterly(history)
    latest = q[0] if q else {}
    return {
        "buybacks_latest": _num(latest.get("buybacks")),
        "dividends_latest": _num(latest.get("dividends_paid")),
        "fcf_latest": _num(latest.get("fcf")),
        "buybacks_ttm": ttm(history, "buybacks"),
        "dividends_ttm": ttm(history, "dividends_paid"),
        "fcf_ttm": ttm(history, "fcf"),
    }


# ---------------------------------------------------------------------------
# Formatting helpers (used by trend_rows / trend_summary_strings)
# ---------------------------------------------------------------------------
def _fmt_billions(value: Any) -> str:
    v = _num(value)
    if v is None:
        return "—"
    return f"${v / 1e9:.1f}B"


def _fmt_pct(value: Any, *, signed: bool = False) -> str:
    v = _num(value)
    if v is None:
        return "—"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def _fmt_eps(value: Any) -> str:
    v = _num(value)
    if v is None:
        return "—"
    return f"${v:.2f}"


# ---------------------------------------------------------------------------
# Trend table / LLM-context summary
# ---------------------------------------------------------------------------
def trend_headers(history: dict[str, Any] | None, *, max_quarters: int = 5) -> list[str]:
    """["Metric", label0, label1, ...] for the newest `max_quarters` quarters."""
    cols = _quarterly(history)[:max_quarters]
    return ["Metric"] + [str(row.get("label", "—")) for row in cols]


def trend_rows(history: dict[str, Any] | None, *, max_quarters: int = 5) -> list[list[str]]:
    """List-of-rows table: [metric_label, v(q0), v(q1), ...] for the newest
    `max_quarters` quarters. Rows whose values are all "—" are omitted.
    """
    full = _quarterly(history)
    cols = full[:max_quarters]
    if not cols:
        return []

    candidate_rows: list[tuple[str, list[str]]] = [
        ("Revenue ($B)", [_fmt_billions(r.get("revenue")) for r in cols]),
        ("Revenue YoY %", [_fmt_pct(_yoy_at(full, i, "revenue"), signed=True) for i in range(len(cols))]),
        ("Gross margin %", [_fmt_pct(r.get("gross_margin")) for r in cols]),
        ("Operating margin %", [_fmt_pct(r.get("operating_margin")) for r in cols]),
        ("Net margin %", [_fmt_pct(r.get("net_margin")) for r in cols]),
        ("EPS ($)", [_fmt_eps(r.get("eps_diluted")) for r in cols]),
        ("FCF ($B)", [_fmt_billions(r.get("fcf")) for r in cols]),
    ]

    rows: list[list[str]] = []
    for label, values in candidate_rows:
        if all(v == "—" for v in values):
            continue
        rows.append([label, *values])
    return rows


def trend_summary_strings(history: dict[str, Any] | None, *, max_quarters: int = 5) -> dict[str, str]:
    """Compact one-line strings per metric family, for LLM context.

    Only quarters that have data for the given metric family are included;
    a family with no data anywhere in the window returns "—".
    """
    full = _quarterly(history)
    cols = full[:max_quarters]

    revenue_parts: list[str] = []
    for i, r in enumerate(cols):
        rev = _num(r.get("revenue"))
        if rev is None:
            continue
        label = r.get("label", "—")
        y = _yoy_at(full, i, "revenue")
        if y is not None:
            revenue_parts.append(f"{label}: {_fmt_billions(rev)} ({y:+.1f}% YoY)")
        else:
            revenue_parts.append(f"{label}: {_fmt_billions(rev)}")

    margin_parts: list[str] = []
    for r in cols:
        gm, om, nm = r.get("gross_margin"), r.get("operating_margin"), r.get("net_margin")
        if gm is None and om is None and nm is None:
            continue
        label = r.get("label", "—")
        margin_parts.append(f"{label}: GM {_fmt_pct(gm)} / OM {_fmt_pct(om)} / NM {_fmt_pct(nm)}")

    fcf_parts: list[str] = []
    for r in cols:
        fcf = _num(r.get("fcf"))
        if fcf is None:
            continue
        label = r.get("label", "—")
        fcf_parts.append(f"{label}: {_fmt_billions(fcf)}")

    return {
        "revenue_trend": " | ".join(revenue_parts) if revenue_parts else "—",
        "margin_trend": " | ".join(margin_parts) if margin_parts else "—",
        "fcf_trend": " | ".join(fcf_parts) if fcf_parts else "—",
    }
