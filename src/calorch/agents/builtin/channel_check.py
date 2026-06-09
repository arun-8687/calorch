"""Channel-check agent — distributor / reseller diligence preparation."""
from calorch.agents.base import AgentSpec, register
from calorch.renderers import _build_channel_check
from calorch.state import EventType

register(
    AgentSpec(
        event_type=EventType.CHANNEL_CHECK,
        analysis_builder=_build_channel_check,
        keywords=("channel", "distributor", "reseller", "channel partner", "var"),
    )
)
