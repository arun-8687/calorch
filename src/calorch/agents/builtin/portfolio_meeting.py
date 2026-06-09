"""Portfolio-meeting agent — investment-committee / holdings review preparation."""
from calorch.agents.base import AgentSpec, register
from calorch.renderers import _build_portfolio_meeting
from calorch.state import EventType

register(
    AgentSpec(
        event_type=EventType.PORTFOLIO_MEETING,
        analysis_builder=_build_portfolio_meeting,
        keywords=("portfolio", "ic ", "investment committee", "holdings"),
    )
)
