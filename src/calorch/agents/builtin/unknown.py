"""Unknown-event agent — generic preparation and registry fallback.

This agent has no classification keywords (UNKNOWN is what's left when
nothing matches) and also serves as the fallback for event types that
have no registered agent.
"""
from __future__ import annotations

from calorch.agents.base import AgentSpec, register
from calorch.analysis import EventAnalysis, base_analysis
from calorch.state import EventType


def build_unknown(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a = base_analysis(f"Calendar Brief — {ev.subject}", ev, cls, ed)
    a.sections = [
        ("Summary", [ev.body_preview or "(no preview)"]),
    ]
    return a


register(
    AgentSpec(
        event_type=EventType.UNKNOWN,
        analysis_builder=build_unknown,
    )
)
