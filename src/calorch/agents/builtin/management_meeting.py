"""Management-meeting agent — executive 1:1 / town-hall preparation."""
from calorch.agents.base import AgentSpec, register
from calorch.renderers import _build_management_meeting
from calorch.state import EventType

register(
    AgentSpec(
        event_type=EventType.MANAGEMENT_MEETING,
        analysis_builder=_build_management_meeting,
        keywords=("ceo", "cfo", "cro", "cto", "1on1", "1:1", "town hall", "mgmt"),
    )
)
