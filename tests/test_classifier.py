"""Tests for the deterministic Pass 1 classifier."""
from calorch.nodes import _keyword_score
from calorch.state import EventType


def test_earnings_call_detection():
    label, hits, counts = _keyword_score("AAPL Q1 earnings call 2026 guidance")
    assert label is EventType.EARNINGS_CALL
    assert hits >= 2


def test_management_meeting_role_inference():
    label, _, _ = _keyword_score("1:1 with CFO — capital allocation")
    assert label is EventType.MANAGEMENT_MEETING


def test_conference_detection():
    label, _, _ = _keyword_score("Morgan Stanley TMT conference — investor day panels")
    assert label is EventType.CONFERENCE


def test_kol_meeting_detection():
    label, _, _ = _keyword_score("KOL call with semiconductor expert consultant")
    assert label is EventType.KOL_MEETING


def test_channel_check_detection():
    label, _, _ = _keyword_score("Channel check with EMEA distributor")
    assert label is EventType.CHANNEL_CHECK


def test_portfolio_meeting_detection():
    label, _, _ = _keyword_score("Portfolio holdings review — IC")
    assert label is EventType.PORTFOLIO_MEETING


def test_internal_review_detection():
    label, _, _ = _keyword_score("Internal team review retro")
    assert label is EventType.INTERNAL_REVIEW


def test_analyst_meeting_detection():
    label, _, _ = _keyword_score("Sell-side analyst meeting — broker view")
    assert label is EventType.ANALYST_MEETING


def test_unknown_when_no_keywords():
    label, hits, _ = _keyword_score("Lunch with the team")
    assert label is EventType.UNKNOWN
    assert hits == 0
