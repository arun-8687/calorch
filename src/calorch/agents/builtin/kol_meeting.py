"""KOL-meeting agent — key-opinion-leader / expert call preparation."""
from calorch.agents.base import AgentSpec, register
from calorch.renderers import _build_kol_meeting
from calorch.state import EventType

register(
    AgentSpec(
        event_type=EventType.KOL_MEETING,
        analysis_builder=_build_kol_meeting,
        keywords=("kol", "expert", "consultant", "thought leader", "kolsight"),
    )
)
