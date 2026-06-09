"""Unknown-event agent — generic preparation and registry fallback.

This agent has no classification keywords (UNKNOWN is what's left when
nothing matches) and also serves as the fallback for event types that
have no registered agent.
"""
from calorch.agents.base import AgentSpec, register
from calorch.renderers import _build_unknown
from calorch.state import EventType

register(
    AgentSpec(
        event_type=EventType.UNKNOWN,
        analysis_builder=_build_unknown,
    )
)
