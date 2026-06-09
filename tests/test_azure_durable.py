"""Tests for calorch.azure_durable — Azure Durable Functions orchestration.

Tests the ADF orchestrator + activities + state adapter without
actually invoking Azure Functions runtime.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from calorch.azure_durable.state import deserialize_state, serialize_state
from calorch.state import CalendarEvent, ClassificationResult, EventType


# ---------------------------------------------------------------------------
# State adapter
# ---------------------------------------------------------------------------
class TestStateAdapter:
    def test_serialize_datetime(self):
        dt = datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc)
        result = serialize_state(dt)
        assert result == "2026-03-02T10:00:00+00:00"

    def test_serialize_calendar_event(self):
        ev = CalendarEvent(
            id="ev-001",
            subject="AAPL Q2 FY2026 Earnings Call",
            start=datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 3, 2, 11, 0, 0, tzinfo=timezone.utc),
        )
        result = serialize_state(ev)
        assert result["id"] == "ev-001"
        assert result["subject"] == "AAPL Q2 FY2026 Earnings Call"
        assert result["start"] in ("2026-03-02T10:00:00+00:00", "2026-03-02T10:00:00Z")

    def test_serialize_nested_dict(self):
        data = {
            "event": CalendarEvent(
                id="ev-001",
                subject="test",
                start=datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc),
                end=datetime(2026, 3, 2, 11, 0, 0, tzinfo=timezone.utc),
            ),
            "count": 42,
        }
        result = serialize_state(data)
        assert result["event"]["id"] == "ev-001"
        assert result["count"] == 42

    def test_deserialize_datetime(self):
        result = deserialize_state("2026-03-02T10:00:00+00:00")
        assert isinstance(result, datetime)
        assert result.year == 2026

    def test_deserialize_calendar_event(self):
        data = {
            "id": "ev-001",
            "subject": "test",
            "start": "2026-03-02T10:00:00+00:00",
            "end": "2026-03-02T11:00:00+00:00",
        }
        result = deserialize_state(data, CalendarEvent)
        assert isinstance(result, CalendarEvent)
        assert result.id == "ev-001"
        assert result.start.year == 2026

    def test_deserialize_plain_dict(self):
        data = {"key": "value", "num": 42}
        result = deserialize_state(data)
        assert result["key"] == "value"
        assert result["num"] == 42

    def test_roundtrip(self):
        cls = ClassificationResult(
            event_id="ev-001",
            pass1_label=EventType.EARNINGS_CALL,
            pass1_keyword_hits=5,
            final_label=EventType.EARNINGS_CALL,
            confidence=0.95,
            rationale="SEC form",
            routed_node="agent_earnings_call",
        )
        serialized = serialize_state(cls)
        deserialized = deserialize_state(serialized, ClassificationResult)
        assert deserialized.event_id == "ev-001"
        assert deserialized.confidence == 0.95
        assert deserialized.final_label == EventType.EARNINGS_CALL


# ---------------------------------------------------------------------------
# Orchestrator logic (unit test without ADF runtime)
# ---------------------------------------------------------------------------
class TestOrchestratorLogic:
    def test_orchestrator_function_exists(self):
        """The orchestrator function should be registered."""
        from calorch.azure_durable.orchestrator import calorch_orchestrator
        assert calorch_orchestrator is not None

    def test_new_run_id_format(self):
        from calorch.azure_durable.orchestrator import _new_run_id
        run_id = _new_run_id()
        assert len(run_id) == 16
        assert "T" in run_id
        assert run_id.endswith("Z")


# ---------------------------------------------------------------------------
# Activity existence
# ---------------------------------------------------------------------------
class TestActivitiesExist:
    def test_activity_functions_exist(self):
        from calorch.azure_durable.activities import (
            activity_scan_calendar,
            activity_classify,
            activity_agent,
            activity_deliver,
            activity_aggregate_briefing,
        )
        # The functions are decorated with Blueprint, so they are FunctionBuilder instances
        assert activity_scan_calendar is not None
        assert activity_classify is not None
        assert activity_agent is not None
        assert activity_deliver is not None
        assert activity_aggregate_briefing is not None

    def test_blueprints_can_be_created(self):
        from calorch.azure_durable.orchestrator import get_blueprint as get_orchestrator_bp
        from calorch.azure_durable.activities import get_blueprint as get_activities_bp
        import azure.durable_functions as df
        
        orchestrator_bp = get_orchestrator_bp()
        activities_bp = get_activities_bp()
        assert isinstance(orchestrator_bp, df.Blueprint)
        assert isinstance(activities_bp, df.Blueprint)


# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------
class TestBlueprintRegistration:
    def test_blueprints_can_be_registered(self):
        import azure.functions as func
        from calorch.azure_durable import register_blueprints
        
        app = func.FunctionApp()
        # Should not raise
        register_blueprints(app)
