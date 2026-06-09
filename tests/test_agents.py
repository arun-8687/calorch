"""Tests for the calorch.agents registry — modular per-event-type agents."""
from __future__ import annotations

import pytest

from calorch.agents import (
    AgentSpec,
    agent_node_names,
    classification_keywords,
    get_agent,
    iter_agents,
    make_agent_subgraph,
    register,
    unregister,
)
from calorch.state import EventType


class TestBuiltinRegistration:
    def test_every_event_type_has_an_agent(self):
        registered = {spec.event_type for spec in iter_agents()}
        assert registered == set(EventType)

    def test_node_names_follow_convention(self):
        for ev_type, name in agent_node_names().items():
            assert name == f"agent_{ev_type.value}"

    def test_keywords_exposed_for_classification(self):
        kws = classification_keywords()
        assert "earnings" in kws[EventType.EARNINGS_CALL]
        assert "kol" in kws[EventType.KOL_MEETING]
        # UNKNOWN deliberately has no keywords
        assert EventType.UNKNOWN not in kws

    def test_duplicate_registration_rejected(self):
        spec = get_agent(EventType.CONFERENCE)
        with pytest.raises(ValueError, match="already registered"):
            register(spec)

    def test_all_subgraphs_compile(self):
        for ev_type in EventType:
            graph = make_agent_subgraph(ev_type)
            assert "prepare" in graph.nodes


class TestExtensibility:
    def test_override_with_custom_graph_factory(self):
        """A deployment can replace a built-in agent with a custom subgraph."""
        original = get_agent(EventType.CONFERENCE)
        sentinel = object()
        try:
            register(
                AgentSpec(
                    event_type=EventType.CONFERENCE,
                    analysis_builder=original.analysis_builder,
                    keywords=original.keywords,
                    graph_factory=lambda spec: sentinel,
                ),
                replace=True,
            )
            assert make_agent_subgraph(EventType.CONFERENCE) is sentinel
        finally:
            register(original, replace=True)

    def test_unregistered_type_falls_back_to_unknown(self):
        """If an event type loses its agent, the UNKNOWN agent handles it."""
        original = get_agent(EventType.CHANNEL_CHECK)
        try:
            unregister(EventType.CHANNEL_CHECK)
            fallback = get_agent(EventType.CHANNEL_CHECK)
            assert fallback.event_type == EventType.UNKNOWN
        finally:
            register(original, replace=True)

    def test_custom_node_name(self):
        spec = AgentSpec(
            event_type=EventType.UNKNOWN,
            analysis_builder=lambda *a, **k: None,
            node_name="agent_custom",
        )
        assert spec.node_name == "agent_custom"


class TestDispatchIntegration:
    def test_build_analysis_dispatches_through_registry(self):
        """renderers.build_analysis must use the registered builder."""
        from calorch.renderers import build_analysis

        original = get_agent(EventType.INTERNAL_REVIEW)
        calls = []

        def fake_builder(event, cls, ed, llm_call, *, providers=None, cik_lookup=None):
            calls.append(event)
            return original.analysis_builder(
                event, cls, ed, llm_call, providers=providers, cik_lookup=cik_lookup
            )

        try:
            register(
                AgentSpec(
                    event_type=EventType.INTERNAL_REVIEW,
                    analysis_builder=fake_builder,
                    keywords=original.keywords,
                ),
                replace=True,
            )
            from datetime import datetime, timezone

            from calorch.state import CalendarEvent, ClassificationResult

            ev = CalendarEvent(
                id="ev-ir",
                subject="Sprint retro",
                start=datetime(2026, 6, 10, tzinfo=timezone.utc),
                end=datetime(2026, 6, 10, 1, tzinfo=timezone.utc),
            )
            cls = ClassificationResult(event_id="ev-ir", final_label=EventType.INTERNAL_REVIEW)
            build_analysis(EventType.INTERNAL_REVIEW, ev, cls, {"snapshots": {}}, None)
            assert calls == [ev]
        finally:
            register(original, replace=True)

    def test_fan_out_uses_registry_node_names(self):
        from calorch.nodes import fan_out_prepare_events
        from datetime import datetime, timezone

        from calorch.state import CalendarEvent, ClassificationResult

        ev = CalendarEvent(
            id="ev-1",
            subject="AAPL Earnings",
            start=datetime(2026, 6, 10, tzinfo=timezone.utc),
            end=datetime(2026, 6, 10, 1, tzinfo=timezone.utc),
        )
        cls = ClassificationResult(event_id="ev-1", final_label=EventType.EARNINGS_CALL)
        sends = fan_out_prepare_events({"events": [ev], "classifications": {"ev-1": cls}})
        assert sends[0].node == "agent_earnings_call"
