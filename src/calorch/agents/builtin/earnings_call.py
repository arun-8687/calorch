"""Earnings-call agent — quarterly results preparation.

Everything specific to the ``earnings_call`` event type lives here:
classification keywords and the analysis builder (10-section earnings
brief with XBRL financials, consensus, segments and guidance).
"""
from calorch.agents.base import AgentSpec, register
from calorch.renderers import _build_earnings_call
from calorch.state import EventType

register(
    AgentSpec(
        event_type=EventType.EARNINGS_CALL,
        analysis_builder=_build_earnings_call,
        keywords=("earnings", "results", "guidance", "q1", "q2", "q3", "q4", "fy"),
    )
)
