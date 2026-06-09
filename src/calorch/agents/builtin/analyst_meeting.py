"""Analyst-meeting agent — sell-side / buy-side meeting preparation."""
from calorch.agents.base import AgentSpec, register
from calorch.renderers import _build_analyst_meeting
from calorch.state import EventType

register(
    AgentSpec(
        event_type=EventType.ANALYST_MEETING,
        analysis_builder=_build_analyst_meeting,
        keywords=("analyst", "broker", "sell-side", "buy-side"),
    )
)
