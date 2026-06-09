"""Internal-review agent — retro / sprint-review preparation."""
from calorch.agents.base import AgentSpec, register
from calorch.renderers import _build_internal_review
from calorch.state import EventType

register(
    AgentSpec(
        event_type=EventType.INTERNAL_REVIEW,
        analysis_builder=_build_internal_review,
        keywords=("internal", "retro", "postmortem", "sprint review", "team meeting"),
    )
)
