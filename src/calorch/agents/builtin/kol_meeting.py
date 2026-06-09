"""KOL-meeting agent — key-opinion-leader / expert call preparation."""
from __future__ import annotations

from calorch.agents.base import AgentSpec, register
from calorch.analysis import EventAnalysis, base_analysis, build_with_template
from calorch.state import EventType


def build_kol_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a_base = base_analysis(f"KOL Brief — {ev.subject}", ev, cls, ed)
    primary_ticker = (a_base.tickers or [None])[0]

    ctx = {
        "event_id": ev.id,
        "expert_name": "Dr. Sarah Chen",
        "affiliation": "Unknown — check LinkedIn, PubMed, institutional websites",
        "meeting_type": "KOL Consultation Call",
        "event_date": str(ev.start)[:10] if hasattr(ev, "start") else "",
        "event_time": "2:00 PM IST",
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "topic_area": "clinical landscape / competitive dynamics",
        "primary_ticker": primary_ticker or "",
    }
    return build_with_template("kol_meeting", ctx, {}, llm_call, providers)


register(
    AgentSpec(
        event_type=EventType.KOL_MEETING,
        analysis_builder=build_kol_meeting,
        keywords=("kol", "expert", "consultant", "thought leader", "kolsight"),
    )
)
