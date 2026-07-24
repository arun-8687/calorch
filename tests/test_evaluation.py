"""Tests for the report-quality evaluation harness.

No network, no real LLM: every EventAnalysis is hand-built and every
judge_invoke is a plain Python fake.
"""
from __future__ import annotations

import json

import pytest

from calorch.analysis import EventAnalysis
from calorch.evaluation import (
    RUBRIC,
    aggregate,
    deterministic_checks,
    evaluate_analysis,
    render_analysis_text,
    score_report,
)
from calorch.state import EventType


# ---------------------------------------------------------------------------
# Fixtures: hand-built EventAnalysis objects
# ---------------------------------------------------------------------------
def _clean_analysis() -> EventAnalysis:
    return EventAnalysis(
        event_id="ev-1",
        event_type=EventType.EARNINGS_CALL,
        title="EARNINGS PREP PACK",
        sections=[
            ("Executive Snapshot", ["Revenue grew 8% YoY per the Q2 10-Q."]),
            ("Quarterly Trend", ["__TABLE__"]),
        ],
        tables=[
            {
                "title": "Quarterly Trend",
                "headers": ["Metric", "Q2 FY26"],
                "rows": [["Revenue", "$95.4B"], ["EPS", "$1.65"]],
                "source_note": "Source: SEC quarterly filings (XBRL)",
            }
        ],
        tickers=["AAPL"],
        source_attribution="Source: sec-edgar-xbrl @ 2026-06-09",
        confidence=0.9,
        data_sources=[{"source_name": "SEC EDGAR", "status": "active", "detail": "XBRL companyfacts"}],
    )


def _checks_by_name(checks: list[dict]) -> dict[str, dict]:
    return {c["check"]: c for c in checks}


def _failed_names(checks: list[dict]) -> list[str]:
    return sorted(c["check"] for c in checks if not c["passed"])


# ---------------------------------------------------------------------------
# deterministic_checks
# ---------------------------------------------------------------------------
def test_deterministic_checks_clean_analysis_all_pass():
    checks = deterministic_checks(_clean_analysis())
    assert len(checks) == 5
    assert all(c["passed"] for c in checks)
    assert all(c["detail"] == "ok" for c in checks)
    # every check has the locating-detail shape
    for c in checks:
        assert set(c) == {"check", "passed", "detail"}


def test_deterministic_checks_catches_bare_dash_row():
    a = _clean_analysis()
    a.tables[0]["rows"] = [*a.tables[0]["rows"], ["Net margin", "—"]]
    checks = deterministic_checks(a)
    assert _failed_names(checks) == ["no_bare_dash_cells"]
    by_name = _checks_by_name(checks)
    assert "row[2]" in by_name["no_bare_dash_cells"]["detail"]


def test_deterministic_checks_catches_bare_hyphen_cell():
    a = _clean_analysis()
    a.tables[0]["rows"][1][1] = " - "  # bare ASCII hyphen, padded
    checks = deterministic_checks(a)
    assert _failed_names(checks) == ["no_bare_dash_cells"]


def test_deterministic_checks_catches_unresolved_placeholder():
    a = _clean_analysis()
    a.sections[0] = (
        a.sections[0][0],
        ["Revenue grew {rev_yoy} YoY per the Q2 10-Q."],
    )
    checks = deterministic_checks(a)
    assert _failed_names(checks) == ["no_unresolved_placeholders"]
    by_name = _checks_by_name(checks)
    assert "{rev_yoy}" in by_name["no_unresolved_placeholders"]["detail"]


def test_deterministic_checks_catches_blocklisted_fabrication():
    a = _clean_analysis()
    a.sections[0] = (
        a.sections[0][0],
        ["Dr. Sarah Chen flagged margin pressure on the call."],
    )
    checks = deterministic_checks(a)
    assert _failed_names(checks) == ["no_blocklisted_fabrications"]
    by_name = _checks_by_name(checks)
    assert "Dr. Sarah Chen" in by_name["no_blocklisted_fabrications"]["detail"]


def test_deterministic_checks_custom_blocklist():
    a = _clean_analysis()
    a.sections[0] = (a.sections[0][0], ["Contains a totally custom bad phrase."])
    checks = deterministic_checks(a, blocklist=["totally custom bad phrase"])
    assert _failed_names(checks) == ["no_blocklisted_fabrications"]


def test_deterministic_checks_table_without_source_note_or_source_heading_fails():
    a = _clean_analysis()
    a.tables[0].pop("source_note")
    a.sections[1] = ("Numbers", ["__TABLE__"])  # heading doesn't name a source
    checks = deterministic_checks(a)
    assert _failed_names(checks) == ["tables_have_source_attribution"]


def test_deterministic_checks_table_source_via_heading_name_is_enough():
    a = _clean_analysis()
    a.tables[0].pop("source_note")
    a.sections[1] = ("Source: SEC XBRL", ["__TABLE__"])
    checks = deterministic_checks(a)
    assert all(c["passed"] for c in checks)


def test_deterministic_checks_empty_data_sources_fails():
    a = _clean_analysis()
    a.data_sources = []
    checks = deterministic_checks(a)
    assert _failed_names(checks) == ["data_sources_present"]


def test_deterministic_checks_non_numeric_table_needs_no_source_note():
    a = _clean_analysis()
    a.tables[0].pop("source_note")
    a.tables[0]["rows"] = [["Status", "On track"], ["Tone", "Confident"]]
    checks = deterministic_checks(a)
    assert all(c["passed"] for c in checks)


# ---------------------------------------------------------------------------
# render_analysis_text
# ---------------------------------------------------------------------------
def test_render_analysis_text_includes_table_rows():
    text = render_analysis_text(_clean_analysis())
    assert "EARNINGS PREP PACK" in text
    assert "Executive Snapshot" in text
    assert "Revenue grew 8% YoY" in text
    assert "Quarterly Trend" in text
    assert "Revenue" in text and "$95.4B" in text
    assert "EPS" in text and "$1.65" in text
    assert "SEC quarterly filings (XBRL)" in text
    assert "SEC EDGAR" in text  # from data_sources


def test_render_analysis_text_handles_no_tables_or_sources():
    a = EventAnalysis(
        event_id="ev-2",
        event_type=EventType.CONFERENCE,
        title="CONFERENCE PREP PACK",
        sections=[("Company Overview", ["Nothing notable."])],
    )
    text = render_analysis_text(a)
    assert "CONFERENCE PREP PACK" in text
    assert "Nothing notable." in text


# ---------------------------------------------------------------------------
# evaluate_analysis
# ---------------------------------------------------------------------------
def _well_formed_payload() -> dict:
    return {
        "scores": {c["key"]: 4 for c in RUBRIC},
        "justifications": {c["key"]: "looks fine" for c in RUBRIC},
        "overall": 4,
    }


def test_evaluate_analysis_parses_well_formed_json():
    payload = _well_formed_payload()

    def fake_judge(_prompt: str) -> str:
        return json.dumps(payload)

    result = evaluate_analysis(_clean_analysis(), fake_judge, ticker="AAPL")
    assert "error" not in result
    assert result["scores"] == {c["key"]: 4.0 for c in RUBRIC}
    assert result["overall"] == 4.0
    assert result["justifications"]["sourcing"] == "looks fine"


def test_evaluate_analysis_parses_fenced_json_with_surrounding_prose():
    payload = _well_formed_payload()
    raw = "Sure, here is my evaluation:\n```json\n" + json.dumps(payload) + "\n```\nHope that helps!"

    def fake_judge(_prompt: str) -> str:
        return raw

    result = evaluate_analysis(_clean_analysis(), fake_judge)
    assert "error" not in result
    assert result["scores"] == {c["key"]: 4.0 for c in RUBRIC}
    assert result["overall"] == 4.0


def test_evaluate_analysis_garbage_response_returns_error_not_raise():
    def fake_judge(_prompt: str) -> str:
        return "I refuse to answer in JSON, sorry about that."

    result = evaluate_analysis(_clean_analysis(), fake_judge)
    assert "error" in result
    assert "scores" not in result


def test_evaluate_analysis_valid_json_but_wrong_shape_returns_error():
    def fake_judge(_prompt: str) -> str:
        return json.dumps({"final_label": "earnings_call", "confidence": 0.4})

    result = evaluate_analysis(_clean_analysis(), fake_judge)
    assert "error" in result


def test_evaluate_analysis_judge_invoke_raising_returns_error_not_raise():
    def raising_judge(_prompt: str) -> str:
        raise RuntimeError("boom")

    result = evaluate_analysis(_clean_analysis(), raising_judge)
    assert "error" in result


def test_evaluate_analysis_missing_overall_falls_back_to_mean_of_scores():
    payload = {"scores": {"sourcing": 2, "trend_accuracy": 4}, "justifications": {}}

    def fake_judge(_prompt: str) -> str:
        return json.dumps(payload)

    result = evaluate_analysis(_clean_analysis(), fake_judge)
    assert result["overall"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# score_report
# ---------------------------------------------------------------------------
def test_score_report_combines_deterministic_and_rubric():
    def fake_judge(_prompt: str) -> str:
        return json.dumps(_well_formed_payload())

    report = score_report(_clean_analysis(), fake_judge, ticker="AAPL")
    assert report["deterministic_passed"] is True
    assert report["ticker"] == "AAPL"
    assert report["event_type"] == "earnings_call"
    assert report["rubric"]["overall"] == 4.0
    assert len(report["deterministic"]) == 5


def test_score_report_defaults_ticker_from_analysis():
    def fake_judge(_prompt: str) -> str:
        return json.dumps(_well_formed_payload())

    report = score_report(_clean_analysis(), fake_judge)
    assert report["ticker"] == "AAPL"


def test_score_report_deterministic_passed_false_on_violation():
    a = _clean_analysis()
    a.data_sources = []

    def fake_judge(_prompt: str) -> str:
        return json.dumps(_well_formed_payload())

    report = score_report(a, fake_judge)
    assert report["deterministic_passed"] is False


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------
def test_aggregate_computes_means_and_failure_counts():
    good_checks = deterministic_checks(_clean_analysis())
    bad_analysis = _clean_analysis()
    bad_analysis.data_sources = []
    bad_checks = deterministic_checks(bad_analysis)

    results = [
        {
            "deterministic": good_checks,
            "deterministic_passed": True,
            "rubric": {"scores": {"sourcing": 4, "completeness": 2}, "overall": 3},
            "ticker": "AAPL",
            "event_type": "earnings_call",
        },
        {
            "deterministic": bad_checks,
            "deterministic_passed": False,
            "rubric": {"scores": {"sourcing": 2, "completeness": 4}, "overall": 3},
            "ticker": "NVDA",
            "event_type": "management_meeting",
        },
        {
            "deterministic": good_checks,
            "deterministic_passed": True,
            "rubric": {"error": "could not parse"},
            "ticker": "MSFT",
            "event_type": "conference",
        },
    ]

    agg = aggregate(results)
    assert agg["n_reports"] == 3
    assert agg["mean_scores"] == {"sourcing": 3.0, "completeness": 3.0}
    assert agg["mean_overall"] == pytest.approx(3.0)
    assert agg["deterministic_failures"] == 1  # one failing check in bad_checks
    assert agg["reports_with_deterministic_failures"] == 1
    assert agg["rubric_errors"] == 1


def test_aggregate_empty_results():
    agg = aggregate([])
    assert agg == {
        "n_reports": 0,
        "mean_scores": {},
        "mean_overall": None,
        "deterministic_failures": 0,
        "reports_with_deterministic_failures": 0,
        "rubric_errors": 0,
    }


def test_aggregate_all_rubric_errors_gives_none_mean_overall():
    a = _clean_analysis()
    checks = deterministic_checks(a)
    results = [
        {"deterministic": checks, "deterministic_passed": True, "rubric": {"error": "x"}},
        {"deterministic": checks, "deterministic_passed": True, "rubric": {"error": "y"}},
    ]
    agg = aggregate(results)
    assert agg["mean_overall"] is None
    assert agg["mean_scores"] == {}
    assert agg["rubric_errors"] == 2
