"""Helpers for SEC iXBRL segment / geographic revenue tables (earnings brief)."""
from __future__ import annotations

from typing import Any


def _fmt_b(val: float | None) -> str:
    """Format a value in billions with $X.XXB."""
    if val is None:
        return "—"
    return f"${val / 1e9:,.2f}B"


def _total_revenue(segments: list[dict[str, Any]] | None) -> float | None:
    if not segments:
        return None
    total = 0.0
    for s in segments:
        v = s.get("value")
        if isinstance(v, (int, float)):
            total += v
    return total if total > 0 else None


def _build_segment_table_pct(segments: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not segments:
        return None
    # Exclude aggregate members (e.g. ProductMember which sums hardware)
    leafs = [s for s in segments if s.get("segment_member") != "ProductMember"]
    total = _total_revenue(leafs)
    if not total:
        return None
    rows = []
    for s in leafs[:6]:
        label = s.get("segment_label") or s.get("segment_member", "—")
        val = s.get("value")
        if isinstance(val, (int, float)) and total > 0:
            pct = (val / total) * 100
            rows.append([label, _fmt_b(val), f"{pct:.1f}%"])
    return {"headers": ["Segment", "Revenue", "% of Total"], "rows": rows}


def _build_geo_table_pct(segments: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not segments:
        return None
    total = _total_revenue(segments)
    if not total:
        return None
    rows = []
    for s in segments[:6]:
        label = s.get("segment_label") or s.get("segment_member", "—")
        val = s.get("value")
        if isinstance(val, (int, float)) and total > 0:
            pct = (val / total) * 100
            rows.append([label, _fmt_b(val), f"{pct:.1f}%"])
    return {"headers": ["Region", "Revenue", "% of Total"], "rows": rows}
