"""Conference agent — investor day / summit preparation."""
from calorch.agents.base import AgentSpec, register
from calorch.renderers import _build_conference
from calorch.state import EventType

register(
    AgentSpec(
        event_type=EventType.CONFERENCE,
        analysis_builder=_build_conference,
        keywords=("conference", "summit", "expo", "investor day", "cmd", "capital markets day"),
    )
)
