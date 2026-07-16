"""Tests for the template engine (calorch.templates): dash-row suppression,
meta-table suppression, and source_note provenance passthrough.
"""
from __future__ import annotations

import pytest

from calorch.templates import TemplateEngine, _is_blank_value
from calorch.state import EventType


# ---------------------------------------------------------------------------
# _is_blank_value
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    ["—", "-", "— (—/—/—)", "$—", "—x", "—%", "  —  ", "{unresolved_key}", "— ({a}/{b})"],
)
def test_is_blank_value_true_cases(value: str) -> None:
    assert _is_blank_value(value) is True


@pytest.mark.parametrize(
    "value",
    ["5.2%", "-5.2%", "$1.23B", "1.5x", "N/A", "Low Risk", "AAPL", "0.00%", "$0.00"],
)
def test_is_blank_value_false_cases(value: str) -> None:
    assert _is_blank_value(value) is False


# ---------------------------------------------------------------------------
# _build_data_section — inline "rows" path
# ---------------------------------------------------------------------------
def _base_template(**overrides) -> dict:
    tpl = {
        "schema_version": "1.0",
        "event_type": "earnings_call",
        "report_header": {"title": "{primary_ticker} Brief"},
        "sections": [],
    }
    tpl.update(overrides)
    return tpl


def test_data_section_drops_blank_row_keeps_real_row() -> None:
    tpl = _base_template(
        sections=[
            {
                "id": "metrics",
                "title": "Metrics",
                "source": "data",
                "rows": [
                    {"label": "Revenue", "value": "{revenue}"},
                    {"label": "Price", "value": "{price}"},
                ],
            }
        ]
    )
    ctx = {"primary_ticker": "AAPL", "revenue": "$90.00B", "price": "—"}
    a = TemplateEngine(tpl).build(context=ctx)
    assert a.sections == [("Metrics", ["__TABLE__"])]
    assert len(a.tables) == 1
    assert a.tables[0]["rows"] == [["Revenue", "$90.00B"]]


def test_data_section_all_blank_rows_omits_section_and_table() -> None:
    tpl = _base_template(
        sections=[
            {
                "id": "metrics",
                "title": "Metrics",
                "source": "data",
                "rows": [
                    {"label": "EPS Estimate", "value": "{eps_estimate}"},
                    {"label": "EPS Surprise", "value": "{eps_surprise}"},
                ],
            }
        ]
    )
    ctx = {"primary_ticker": "AAPL", "eps_estimate": "—", "eps_surprise": "— (—/—/—)"}
    a = TemplateEngine(tpl).build(context=ctx)
    assert a.sections == []
    assert a.tables == []


def test_data_section_drops_decorated_dash_cluster_row() -> None:
    tpl = _base_template(
        sections=[
            {
                "id": "metrics",
                "title": "Metrics",
                "source": "data",
                "rows": [
                    {"label": "Revenue Surprise", "value": "{rev_surprise}"},
                    {"label": "Revenue", "value": "{revenue}"},
                ],
            }
        ]
    )
    ctx = {"primary_ticker": "AAPL", "rev_surprise": "— (—/—/—)", "revenue": "$1.00B"}
    a = TemplateEngine(tpl).build(context=ctx)
    assert len(a.tables) == 1
    assert a.tables[0]["rows"] == [["Revenue", "$1.00B"]]


def test_data_section_drops_row_with_unresolved_placeholder() -> None:
    tpl = _base_template(
        sections=[
            {
                "id": "metrics",
                "title": "Metrics",
                "source": "data",
                "rows": [
                    {"label": "Custom", "value": "Delta: {never_provided}"},
                    {"label": "Revenue", "value": "{revenue}"},
                ],
            }
        ]
    )
    ctx = {"primary_ticker": "AAPL", "revenue": "$1.00B"}
    a = TemplateEngine(tpl).build(context=ctx)
    assert len(a.tables) == 1
    assert a.tables[0]["rows"] == [["Revenue", "$1.00B"]]


def test_blank_rows_note_grid_untouched() -> None:
    tpl = _base_template(
        sections=[
            {
                "id": "notes",
                "title": "Notes",
                "source": "data",
                "headers": ["Question", "Notes"],
                "blank_rows": 3,
            }
        ]
    )
    a = TemplateEngine(tpl).build(context={"primary_ticker": "AAPL"})
    assert a.sections == [("Notes", ["__TABLE__"])]
    assert len(a.tables) == 1
    assert a.tables[0]["rows"] == [["", ""], ["", ""], ["", ""]]


# ---------------------------------------------------------------------------
# _build_data_section — rows_from path
# ---------------------------------------------------------------------------
def test_rows_from_filters_blank_rows() -> None:
    tpl = _base_template(
        sections=[
            {
                "id": "segments",
                "title": "Segments",
                "source": "data",
                "rows_from": "segments",
            }
        ]
    )
    data_tables = {
        "segments": {
            "headers": ["Segment", "Revenue", "% of Total"],
            "rows": [
                ["iPhone", "$45.00B", "69.2%"],
                ["Other", "—", "—"],
            ],
        }
    }
    a = TemplateEngine(tpl).build(context={"primary_ticker": "AAPL"}, data_tables=data_tables)
    assert len(a.tables) == 1
    assert a.tables[0]["rows"] == [["iPhone", "$45.00B", "69.2%"]]


def test_rows_from_all_blank_rows_omits_section_and_table() -> None:
    tpl = _base_template(
        sections=[
            {
                "id": "segments",
                "title": "Segments",
                "source": "data",
                "rows_from": "segments",
            }
        ]
    )
    data_tables = {
        "segments": {
            "headers": ["Segment", "Revenue", "% of Total"],
            "rows": [["iPhone", "—", "—"], ["Mac", "—", "—"]],
        }
    }
    a = TemplateEngine(tpl).build(context={"primary_ticker": "AAPL"}, data_tables=data_tables)
    assert a.sections == []
    assert a.tables == []


def test_rows_from_row_kept_when_label_blank_but_value_present() -> None:
    """Row filter only inspects non-label cells — a blank label doesn't sink the row."""
    tpl = _base_template(
        sections=[{"id": "x", "title": "X", "source": "data", "rows_from": "geo"}]
    )
    data_tables = {"geo": {"headers": ["Region", "Revenue"], "rows": [["—", "$1.00B"]]}}
    a = TemplateEngine(tpl).build(context={"primary_ticker": "AAPL"}, data_tables=data_tables)
    assert len(a.tables) == 1
    assert a.tables[0]["rows"] == [["—", "$1.00B"]]


# ---------------------------------------------------------------------------
# metadata_table suppression
# ---------------------------------------------------------------------------
def test_meta_table_drops_blank_rows_keeps_real() -> None:
    tpl = _base_template(
        metadata_table={
            "rows": [
                {"label": "Price", "value": "{price}"},
                {"label": "CEO", "value": "{ceo_name}"},
            ]
        },
    )
    ctx = {"primary_ticker": "AAPL", "price": "—", "ceo_name": "Tim Cook"}
    a = TemplateEngine(tpl).build(context=ctx)
    assert len(a.tables) == 1
    assert a.tables[0]["rows"] == [["CEO", "Tim Cook"]]


def test_meta_table_all_blank_omits_table_entirely() -> None:
    tpl = _base_template(
        metadata_table={
            "rows": [
                {"label": "Price", "value": "{price}"},
                {"label": "CEO", "value": "{ceo_name}"},
            ]
        },
    )
    ctx = {"primary_ticker": "AAPL", "price": "—", "ceo_name": "—"}
    a = TemplateEngine(tpl).build(context=ctx)
    assert a.tables == []


# ---------------------------------------------------------------------------
# source_note passthrough
# ---------------------------------------------------------------------------
def test_rows_from_source_note_copied_onto_table() -> None:
    tpl = _base_template(
        sections=[{"id": "x", "title": "Financials", "source": "data", "rows_from": "financials"}]
    )
    data_tables = {
        "financials": {
            "headers": ["Metric", "Value"],
            "rows": [["Revenue", "$1.00B"]],
            "source_note": "Source: SEC 10-Q filed 2026-01-28, period ended 2025-12-27",
        }
    }
    a = TemplateEngine(tpl).build(context={"primary_ticker": "AAPL"}, data_tables=data_tables)
    assert a.tables[0]["source_note"] == "Source: SEC 10-Q filed 2026-01-28, period ended 2025-12-27"


def test_rows_from_no_source_note_key_omitted() -> None:
    tpl = _base_template(
        sections=[{"id": "x", "title": "Financials", "source": "data", "rows_from": "financials"}]
    )
    data_tables = {"financials": {"headers": ["Metric", "Value"], "rows": [["Revenue", "$1.00B"]]}}
    a = TemplateEngine(tpl).build(context={"primary_ticker": "AAPL"}, data_tables=data_tables)
    assert "source_note" not in a.tables[0]


def test_static_inline_rows_source_note_formatted_with_ctx() -> None:
    tpl = _base_template(
        sections=[
            {
                "id": "x",
                "title": "Metrics",
                "source": "data",
                "source_note": "Q4 derived as FY minus Q1–Q3 for {primary_ticker}",
                "rows": [{"label": "Revenue", "value": "{revenue}"}],
            }
        ]
    )
    a = TemplateEngine(tpl).build(context={"primary_ticker": "AAPL", "revenue": "$1.00B"})
    assert a.tables[0]["source_note"] == "Q4 derived as FY minus Q1–Q3 for AAPL"


def test_event_type_resolves_from_enum() -> None:
    tpl = _base_template()
    a = TemplateEngine(tpl).build(context={"primary_ticker": "AAPL"})
    assert a.event_type == EventType.EARNINGS_CALL
