"""Tests for `calorch.fin_metrics` — pure, None-safe derived analytics.

No network, no fixtures beyond hand-built dicts shaped like
`fundamentals_history()` / `latest_fundamentals()` output.
"""
from __future__ import annotations

from calorch import fin_metrics as fm

# ---------------------------------------------------------------------------
# Hand-built 5-quarter history fixture, newest first.
# Revenue grows steadily; margins/EPS/FCF present for all 5; Q4 2025 (index 4,
# the year-ago quarter for yoy/margin_delta) is fully populated so those
# functions have a base to compare against.
# ---------------------------------------------------------------------------
def _history(quarters: list[dict] | None = None) -> dict:
    if quarters is not None:
        return {"source": "sec-edgartools", "ticker": "AAPL", "cik": "0000320193", "quarterly": quarters}
    rows = [
        {"label": "Q2 2026", "revenue": 111.2e9, "gross_profit": 50.0e9, "operating_income": 30.0e9,
         "net_income": 25.0e9, "eps_diluted": 1.6, "gross_margin": 45.0, "operating_margin": 27.0,
         "net_margin": 22.5, "ocf": 35.0e9, "capex": 3.0e9, "fcf": 32.0e9, "buybacks": 20.0e9,
         "dividends_paid": 4.0e9},
        {"label": "Q1 2026", "revenue": 143.8e9, "gross_profit": 63.0e9, "operating_income": 42.0e9,
         "net_income": 36.0e9, "eps_diluted": 2.3, "gross_margin": 43.8, "operating_margin": 29.2,
         "net_margin": 25.0, "ocf": 50.0e9, "capex": 4.0e9, "fcf": 46.0e9, "buybacks": 22.0e9,
         "dividends_paid": 4.0e9},
        {"label": "Q4 2025", "revenue": 95.0e9, "gross_profit": 42.0e9, "operating_income": 26.0e9,
         "net_income": 21.0e9, "eps_diluted": 1.35, "gross_margin": 44.2, "operating_margin": 27.4,
         "net_margin": 22.1, "ocf": 28.0e9, "capex": 3.5e9, "fcf": 24.5e9, "buybacks": 18.0e9,
         "dividends_paid": 3.8e9},
        {"label": "Q3 2025", "revenue": 89.5e9, "gross_profit": 39.0e9, "operating_income": 24.0e9,
         "net_income": 19.0e9, "eps_diluted": 1.2, "gross_margin": 43.6, "operating_margin": 26.8,
         "net_margin": 21.2, "ocf": 26.0e9, "capex": 3.2e9, "fcf": 22.8e9, "buybacks": 17.0e9,
         "dividends_paid": 3.7e9},
        {"label": "Q2 2025", "revenue": 95.4e9, "gross_profit": 42.5e9, "operating_income": 27.0e9,
         "net_income": 22.0e9, "eps_diluted": 1.4, "gross_margin": 44.6, "operating_margin": 28.3,
         "net_margin": 23.1, "ocf": 30.0e9, "capex": 3.1e9, "fcf": 26.9e9, "buybacks": 19.0e9,
         "dividends_paid": 3.9e9},
    ]
    return {"source": "sec-edgartools", "ticker": "AAPL", "cik": "0000320193", "quarterly": rows}


FIXTURE = _history()


# ---------------------------------------------------------------------------
# yoy / qoq
# ---------------------------------------------------------------------------
def test_yoy_computes_percent_change_vs_4_back() -> None:
    result = fm.yoy(FIXTURE, "revenue")
    expected = round((111.2e9 - 95.4e9) / 95.4e9 * 100, 1)
    assert result == expected


def test_yoy_none_when_fewer_than_5_quarters() -> None:
    short = _history(FIXTURE["quarterly"][:4])
    assert fm.yoy(short, "revenue") is None


def test_yoy_none_when_base_missing() -> None:
    rows = [dict(r) for r in FIXTURE["quarterly"]]
    rows[4]["revenue"] = None
    assert fm.yoy(_history(rows), "revenue") is None


def test_yoy_none_when_base_non_positive() -> None:
    rows = [dict(r) for r in FIXTURE["quarterly"]]
    rows[4]["revenue"] = 0
    assert fm.yoy(_history(rows), "revenue") is None
    rows[4]["revenue"] = -5.0
    assert fm.yoy(_history(rows), "revenue") is None


def test_yoy_none_on_empty_history() -> None:
    assert fm.yoy({"quarterly": []}, "revenue") is None
    assert fm.yoy(None, "revenue") is None
    assert fm.yoy({}, "revenue") is None


def test_qoq_computes_percent_change_vs_1_back() -> None:
    result = fm.qoq(FIXTURE, "revenue")
    expected = round((111.2e9 - 143.8e9) / 143.8e9 * 100, 1)
    assert result == expected


def test_qoq_none_when_fewer_than_2_quarters() -> None:
    assert fm.qoq(_history(FIXTURE["quarterly"][:1]), "revenue") is None


def test_qoq_none_when_key_missing() -> None:
    assert fm.qoq(FIXTURE, "nonexistent_key") is None


# ---------------------------------------------------------------------------
# ttm — needs exactly 4 present quarters
# ---------------------------------------------------------------------------
def test_ttm_sums_newest_4_quarters() -> None:
    expected = 111.2e9 + 143.8e9 + 95.0e9 + 89.5e9
    assert fm.ttm(FIXTURE, "revenue") == expected


def test_ttm_none_when_fewer_than_4_quarters() -> None:
    assert fm.ttm(_history(FIXTURE["quarterly"][:3]), "revenue") is None


def test_ttm_none_when_any_of_4_missing() -> None:
    rows = [dict(r) for r in FIXTURE["quarterly"]]
    rows[2]["revenue"] = None
    assert fm.ttm(_history(rows), "revenue") is None


# ---------------------------------------------------------------------------
# margin_delta
# ---------------------------------------------------------------------------
def test_margin_delta_computes_pts_change_vs_prior_year() -> None:
    result = fm.margin_delta(FIXTURE, "gross_margin")
    assert result == round(45.0 - 44.6, 1)


def test_margin_delta_none_when_fewer_than_5_quarters() -> None:
    assert fm.margin_delta(_history(FIXTURE["quarterly"][:4]), "gross_margin") is None


def test_margin_delta_none_when_value_missing() -> None:
    rows = [dict(r) for r in FIXTURE["quarterly"]]
    rows[0]["gross_margin"] = None
    assert fm.margin_delta(_history(rows), "gross_margin") is None


# ---------------------------------------------------------------------------
# dso / dio / dpo / ccc — latest_fundamentals-shaped snapshot
# ---------------------------------------------------------------------------
FUNDS = {
    "revenue": 100_000.0,
    "gross_profit": 40_000.0,
    "cost_of_revenue": 60_000.0,
    "inventory": 10_000.0,
    "receivables": 12_000.0,
    "accounts_payable": 9_000.0,
}


def test_dso_dio_dpo_use_cost_of_revenue_when_present() -> None:
    assert fm.dso(FUNDS) == round(12_000.0 / 100_000.0 * 91, 1)
    assert fm.dio(FUNDS) == round(10_000.0 / 60_000.0 * 91, 1)
    assert fm.dpo(FUNDS) == round(9_000.0 / 60_000.0 * 91, 1)


def test_dio_dpo_fall_back_to_revenue_minus_gross_profit_when_cogs_absent() -> None:
    funds = {k: v for k, v in FUNDS.items() if k != "cost_of_revenue"}
    cogs = FUNDS["revenue"] - FUNDS["gross_profit"]
    assert fm.dio(funds) == round(FUNDS["inventory"] / cogs * 91, 1)
    assert fm.dpo(funds) == round(FUNDS["accounts_payable"] / cogs * 91, 1)


def test_ccc_sums_dso_plus_dio_minus_dpo() -> None:
    d_so, d_io, d_po = fm.dso(FUNDS), fm.dio(FUNDS), fm.dpo(FUNDS)
    assert fm.ccc(FUNDS) == round(d_so + d_io - d_po, 1)


def test_ccc_family_none_safe_on_missing_inputs() -> None:
    assert fm.dso(None) is None
    assert fm.dso({}) is None
    assert fm.dio({"inventory": 10.0}) is None  # no cogs source at all
    assert fm.dpo({"accounts_payable": 5.0}) is None
    assert fm.ccc({}) is None


def test_ccc_none_when_any_component_missing() -> None:
    funds = dict(FUNDS)
    del funds["inventory"]
    assert fm.dio(funds) is None
    assert fm.ccc(funds) is None


def test_dso_none_on_zero_revenue() -> None:
    funds = dict(FUNDS)
    funds["revenue"] = 0
    assert fm.dso(funds) is None


# ---------------------------------------------------------------------------
# capital_returns
# ---------------------------------------------------------------------------
def test_capital_returns_latest_and_ttm() -> None:
    result = fm.capital_returns(FIXTURE)
    assert result["buybacks_latest"] == 20.0e9
    assert result["dividends_latest"] == 4.0e9
    assert result["fcf_latest"] == 32.0e9
    assert result["buybacks_ttm"] == 20.0e9 + 22.0e9 + 18.0e9 + 17.0e9
    assert result["dividends_ttm"] == 4.0e9 + 4.0e9 + 3.8e9 + 3.7e9
    assert result["fcf_ttm"] == 32.0e9 + 46.0e9 + 24.5e9 + 22.8e9


def test_capital_returns_empty_history_returns_all_none() -> None:
    result = fm.capital_returns({"quarterly": []})
    assert all(v is None for v in result.values())
    result = fm.capital_returns(None)
    assert all(v is None for v in result.values())


# ---------------------------------------------------------------------------
# trend_rows / trend_headers
# ---------------------------------------------------------------------------
def test_trend_headers_returns_metric_plus_labels() -> None:
    headers = fm.trend_headers(FIXTURE)
    assert headers == ["Metric", "Q2 2026", "Q1 2026", "Q4 2025", "Q3 2025", "Q2 2025"]


def test_trend_headers_respects_max_quarters() -> None:
    headers = fm.trend_headers(FIXTURE, max_quarters=3)
    assert headers == ["Metric", "Q2 2026", "Q1 2026", "Q4 2025"]


def test_trend_rows_includes_expected_metrics() -> None:
    rows = fm.trend_rows(FIXTURE, max_quarters=3)
    labels = [row[0] for row in rows]
    assert "Revenue ($B)" in labels
    assert "Revenue YoY %" in labels
    assert "Gross margin %" in labels
    assert "Operating margin %" in labels
    assert "Net margin %" in labels
    assert "EPS ($)" in labels
    assert "FCF ($B)" in labels

    revenue_row = next(row for row in rows if row[0] == "Revenue ($B)")
    assert revenue_row[1] == "$111.2B"
    assert revenue_row[2] == "$143.8B"
    assert revenue_row[3] == "$95.0B"


def test_trend_rows_yoy_uses_full_history_not_sliced_window() -> None:
    """Q2 2026's YoY must compare against Q2 2025 (index 4), even when the
    displayed window (max_quarters) is smaller than 5 — the YoY comparison
    reaches past the slice into the full quarterly list.
    """
    rows = fm.trend_rows(FIXTURE, max_quarters=3)
    yoy_row = next(row for row in rows if row[0] == "Revenue YoY %")
    expected = round((111.2e9 - 95.4e9) / 95.4e9 * 100, 1)
    assert yoy_row[1] == f"{expected:+.1f}%"


def test_trend_rows_omits_rows_that_are_all_dashes() -> None:
    rows = [
        {"label": "Q2 2026", "revenue": 100.0e9},  # no margins, no EPS, no FCF
        {"label": "Q1 2026", "revenue": 90.0e9},
    ]
    result = fm.trend_rows(_history(rows), max_quarters=2)
    labels = [row[0] for row in result]
    assert "Revenue ($B)" in labels
    assert "Gross margin %" not in labels
    assert "EPS ($)" not in labels
    assert "FCF ($B)" not in labels


def test_trend_rows_empty_on_empty_history() -> None:
    assert fm.trend_rows({"quarterly": []}) == []
    assert fm.trend_rows(None) == []


# ---------------------------------------------------------------------------
# trend_summary_strings
# ---------------------------------------------------------------------------
def test_trend_summary_strings_shape() -> None:
    result = fm.trend_summary_strings(FIXTURE, max_quarters=2)
    assert set(result) == {"revenue_trend", "margin_trend", "fcf_trend"}
    assert "Q2 2026: $111.2B" in result["revenue_trend"]
    assert "Q1 2026: $143.8B" in result["revenue_trend"]
    assert "|" in result["revenue_trend"]
    assert "GM" in result["margin_trend"]
    assert "$32.0B" in result["fcf_trend"] or "$32." in result["fcf_trend"]


def test_trend_summary_strings_includes_yoy_when_available() -> None:
    result = fm.trend_summary_strings(FIXTURE, max_quarters=1)
    expected_yoy = round((111.2e9 - 95.4e9) / 95.4e9 * 100, 1)
    assert f"{expected_yoy:+.1f}% YoY" in result["revenue_trend"]


def test_trend_summary_strings_only_includes_quarters_with_data() -> None:
    rows = [
        {"label": "Q2 2026", "revenue": 100.0e9},
        {"label": "Q1 2026"},  # no revenue at all
    ]
    result = fm.trend_summary_strings(_history(rows), max_quarters=2)
    assert "Q2 2026" in result["revenue_trend"]
    assert "Q1 2026" not in result["revenue_trend"]


def test_trend_summary_strings_all_dash_on_empty_history() -> None:
    result = fm.trend_summary_strings({"quarterly": []})
    assert result == {"revenue_trend": "—", "margin_trend": "—", "fcf_trend": "—"}
