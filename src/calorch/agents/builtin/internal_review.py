"""Internal-review agent — retro / sprint-review preparation."""
from __future__ import annotations

from calorch.agents.base import AgentSpec, register
from calorch.analysis import EventAnalysis, build_with_template
from calorch.state import EventType


def build_internal_review(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    ctx = {
        "event_id": ev.id,
        "review_type": "Q2 Coverage Retro",
        "event_date": str(ev.start)[:10] if hasattr(ev, "start") else "",
        "event_time": "1:00 PM IST",
        "confidence": cls.confidence,
        "total_names": "47",
        "active_buys": "12",
        "active_holds": "18",
        "active_sells": "17",
        "coverage_ratio": "94%",
        "initiations": "12 in Q1",
        "updates": "7",
        "deep_dives": "4",
        "channel_checks": "6",
        "kol_calls": "3",
    }
    return build_with_template("internal_review", ctx, {}, llm_call, providers)


register(
    AgentSpec(
        event_type=EventType.INTERNAL_REVIEW,
        analysis_builder=build_internal_review,
        keywords=("internal", "retro", "postmortem", "sprint review", "team meeting"),
    )
)
